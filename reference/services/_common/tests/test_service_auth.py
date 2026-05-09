"""Tests for the worker-host registration helper (12a-1 wave 4).

The helper :func:`bootstrap_worker_credential` walks the §D.1 startup
recovery flow: probe-via-whoami → register or reissue. These tests
drive it against a real ``InMemoryStore`` behind a FastAPI server
with §13 auth enabled, so the helper exercises every branch end-to-
end.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from eden_service_common.auth import (
    bootstrap_worker_credential,
    credential_path,
)
from eden_service_common.cli import (
    bearer_from_shared_token,
    resolve_admin_token,
    resolve_credentials_dir,
)
from eden_storage import InMemoryStore
from eden_wire import StoreClient, make_app
from fastapi.testclient import TestClient

EXPERIMENT_ID = "exp-svc-auth"
ADMIN_TOKEN = "svc-auth-admin"


def _proxy(test_client: TestClient) -> httpx.MockTransport:
    def _handler(request: httpx.Request) -> httpx.Response:
        response = test_client.request(
            request.method,
            request.url.raw_path.decode("ascii"),
            headers=dict(request.headers),
            content=request.content,
        )
        return httpx.Response(
            response.status_code,
            headers=dict(response.headers),
            content=response.content,
        )

    return httpx.MockTransport(_handler)


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore(experiment_id=EXPERIMENT_ID)


@pytest.fixture
def base_url(monkeypatch: pytest.MonkeyPatch, store: InMemoryStore) -> str:
    """Patch httpx to route ``http://unused`` through the in-process app.

    ``bootstrap_worker_credential`` constructs its own ``StoreClient``
    instances, so the patch targets ``httpx.Client`` at the class
    level — every client the helper builds picks up the mock
    transport.
    """
    app = make_app(store, admin_token=ADMIN_TOKEN)
    test_client = TestClient(app)
    transport = _proxy(test_client)
    real_init = httpx.Client.__init__

    def patched_init(self, *args, **kwargs):
        kwargs.setdefault("transport", transport)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched_init)
    return "http://unused"


def test_bootstrap_first_run_registers_and_persists(
    base_url: str, tmp_path: Path
) -> None:
    credentials_dir = tmp_path / "creds"
    cred = bootstrap_worker_credential(
        base_url=base_url,
        experiment_id=EXPERIMENT_ID,
        worker_id="eric",
        credentials_dir=credentials_dir,
        admin_token=ADMIN_TOKEN,
    )
    assert cred.worker_id == "eric"
    assert len(cred.token) == 64  # 32 hex bytes
    assert cred.bearer == f"eric:{cred.token}"
    persisted = credential_path(credentials_dir, "eric").read_text()
    assert persisted == cred.token


def test_bootstrap_idempotent_on_existing_persisted_token(
    base_url: str, tmp_path: Path
) -> None:
    """Restart with the same token: whoami succeeds, no rotation."""
    credentials_dir = tmp_path / "creds"
    first = bootstrap_worker_credential(
        base_url=base_url,
        experiment_id=EXPERIMENT_ID,
        worker_id="eric",
        credentials_dir=credentials_dir,
        admin_token=ADMIN_TOKEN,
    )
    second = bootstrap_worker_credential(
        base_url=base_url,
        experiment_id=EXPERIMENT_ID,
        worker_id="eric",
        credentials_dir=credentials_dir,
        admin_token=ADMIN_TOKEN,
    )
    assert first.token == second.token


def test_bootstrap_reissues_when_persisted_token_is_stale(
    base_url: str, tmp_path: Path, store: InMemoryStore
) -> None:
    """Admin rotated the credential externally; restart must escalate via reissue."""
    credentials_dir = tmp_path / "creds"
    bootstrap_worker_credential(
        base_url=base_url,
        experiment_id=EXPERIMENT_ID,
        worker_id="eric",
        credentials_dir=credentials_dir,
        admin_token=ADMIN_TOKEN,
    )
    # Out-of-band rotation invalidates the persisted token.
    new_token = store.reissue_credential("eric")
    rebooted = bootstrap_worker_credential(
        base_url=base_url,
        experiment_id=EXPERIMENT_ID,
        worker_id="eric",
        credentials_dir=credentials_dir,
        admin_token=ADMIN_TOKEN,
    )
    assert rebooted.token != new_token  # we rotated again on top
    persisted = credential_path(credentials_dir, "eric").read_text()
    assert persisted == rebooted.token
    # The token before our reissue is now invalid; the latest one we
    # persisted authenticates.
    with StoreClient(
        base_url, EXPERIMENT_ID, bearer=rebooted.bearer
    ) as client:
        assert client.whoami() == "eric"


def test_bootstrap_first_run_without_admin_token_raises(
    base_url: str, tmp_path: Path
) -> None:
    """First-run registration requires the admin token."""
    credentials_dir = tmp_path / "creds"
    with pytest.raises(RuntimeError, match="admin token"):
        bootstrap_worker_credential(
            base_url=base_url,
            experiment_id=EXPERIMENT_ID,
            worker_id="alice",
            credentials_dir=credentials_dir,
            admin_token=None,
        )


def test_bootstrap_persists_token_with_owner_only_perms(
    base_url: str, tmp_path: Path
) -> None:
    """§13.5 token-storage hygiene: file mode is 0600."""
    credentials_dir = tmp_path / "creds"
    bootstrap_worker_credential(
        base_url=base_url,
        experiment_id=EXPERIMENT_ID,
        worker_id="eric",
        credentials_dir=credentials_dir,
        admin_token=ADMIN_TOKEN,
    )
    path = credential_path(credentials_dir, "eric")
    assert (path.stat().st_mode & 0o777) == 0o600


def test_resolve_admin_token_prefers_cli_over_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import argparse

    monkeypatch.setenv("EDEN_ADMIN_TOKEN", "from-env")
    args = argparse.Namespace(admin_token="from-cli")
    assert resolve_admin_token(args) == "from-cli"
    args = argparse.Namespace(admin_token=None)
    assert resolve_admin_token(args) == "from-env"
    monkeypatch.delenv("EDEN_ADMIN_TOKEN")
    assert resolve_admin_token(args) is None


def test_resolve_credentials_dir_default_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import argparse

    monkeypatch.delenv("EDEN_WORKER_CREDENTIALS_DIR", raising=False)
    args = argparse.Namespace(credentials_dir=None)
    assert resolve_credentials_dir(args) == Path("/var/lib/eden/credentials")


def test_bearer_from_shared_token_admin_prefix() -> None:
    assert bearer_from_shared_token(None) is None
    assert bearer_from_shared_token("plain") == "admin:plain"
    assert bearer_from_shared_token("eric:secret") == "eric:secret"
