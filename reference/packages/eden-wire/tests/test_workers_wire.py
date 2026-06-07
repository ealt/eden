"""Wire tests for the worker registry endpoints (chapter 7 §6).

Covers the round-trip register / read / list / reissue flow plus the
contract that ``registration_token`` is returned exactly once and that
re-registration of an existing ``worker_id`` is idempotent (no new
token).

Auth is intentionally exercised end-to-end via the §13 admin / worker
bearer scheme; ``test_auth.py`` covers the bearer parsing / dispatch
in isolation.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from eden_storage import InMemoryStore
from eden_wire import StoreClient, make_app
from fastapi.testclient import TestClient

EXPERIMENT_ID = "exp_dqa7yrxcfzrqwkh2qm4c9cpad3"
ADMIN_TOKEN = "wires-admin"


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore(experiment_id=EXPERIMENT_ID)


@pytest.fixture
def app(store: InMemoryStore) -> Any:
    return make_app(store, admin_token=ADMIN_TOKEN)


@pytest.fixture
def admin_client(app: Any) -> StoreClient:
    test_client = TestClient(app)
    transport = _proxy(test_client)
    http = httpx.Client(transport=transport, base_url="http://unused")
    return StoreClient(
        "http://unused",
        experiment_id=EXPERIMENT_ID,
        bearer=f"admin:{ADMIN_TOKEN}",
        client=http,
    )


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


def test_register_worker_returns_token_and_record(
    admin_client: StoreClient,
) -> None:
    # The server mints the opaque worker_id; the caller supplies a name.
    worker, token = admin_client.register_worker(name="eric")
    assert worker.worker_id.startswith("wkr_")
    assert worker.name == "eric"
    assert worker.experiment_id == EXPERIMENT_ID
    assert token is not None
    assert len(token) == 64  # 32 hex bytes


def test_register_worker_with_labels(admin_client: StoreClient) -> None:
    worker, _ = admin_client.register_worker(
        name="agent-claude", labels={"role": "executor"}
    )
    assert worker.labels == {"role": "executor"}


def test_register_worker_mints_fresh_id_each_call(
    admin_client: StoreClient,
) -> None:
    """Names MAY collide; each register mints a fresh worker + credential."""
    first, first_token = admin_client.register_worker(name="eric")
    second, second_token = admin_client.register_worker(name="eric")
    assert first.worker_id != second.worker_id
    assert first_token is not None
    assert second_token is not None
    assert first_token != second_token


def test_read_worker_after_register(admin_client: StoreClient) -> None:
    worker, _ = admin_client.register_worker(name="eric")
    fresh = admin_client.read_worker(worker.worker_id)
    assert fresh.worker_id == worker.worker_id
    assert fresh.name == "eric"


def test_list_workers_filter_by_name(admin_client: StoreClient) -> None:
    minted: dict[str, str] = {}
    for nm in ["zoe", "alice", "bob"]:
        worker, _ = admin_client.register_worker(name=nm)
        minted[nm] = worker.worker_id
    # Unfiltered list returns all three.
    assert {w.worker_id for w in admin_client.list_workers()} == set(
        minted.values()
    )
    # Exact name filter returns only the match.
    alice = admin_client.list_workers(name="alice")
    assert [w.worker_id for w in alice] == [minted["alice"]]
    # A name nobody holds returns the empty set.
    assert admin_client.list_workers(name="nobody") == []


def test_reissue_credential_invalidates_prior(
    app: Any, admin_client: StoreClient
) -> None:
    worker, first_token = admin_client.register_worker(name="eric")
    wid = worker.worker_id
    second_token = admin_client.reissue_credential(wid)
    assert first_token is not None
    assert first_token != second_token
    # The prior credential is invalid: hitting whoami with it should 401.
    test_client = TestClient(app)
    resp = test_client.get(
        f"/v0/experiments/{EXPERIMENT_ID}/whoami",
        headers={
            "X-Eden-Experiment-Id": EXPERIMENT_ID,
            "Authorization": f"Bearer {wid}:{first_token}",
        },
    )
    assert resp.status_code == 401
    # The new credential authenticates.
    resp = test_client.get(
        f"/v0/experiments/{EXPERIMENT_ID}/whoami",
        headers={
            "X-Eden-Experiment-Id": EXPERIMENT_ID,
            "Authorization": f"Bearer {wid}:{second_token}",
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"worker_id": wid, "name": "eric"}


def test_register_worker_reserved_returns_reserved_identifier(
    admin_client: StoreClient,
) -> None:
    """Reserved worker NAMES ``admin`` / ``system`` / ``internal`` MUST be rejected."""
    from eden_storage import ReservedIdentifier

    for reserved in ["admin", "system", "internal"]:
        with pytest.raises(ReservedIdentifier):
            admin_client.register_worker(name=reserved)


def test_register_worker_ill_formed_name_422(
    app: Any,
) -> None:
    """An ill-formed display ``name`` returns 422 invalid-name."""
    test_client = TestClient(app)
    resp = test_client.post(
        f"/v0/experiments/{EXPERIMENT_ID}/workers",
        headers={
            "X-Eden-Experiment-Id": EXPERIMENT_ID,
            "Authorization": f"Bearer admin:{ADMIN_TOKEN}",
        },
        # Leading whitespace violates the display-name grammar. The
        # request model's ``name`` is a plain string (#128), so the body
        # parses and the Store's ``_validate_display_name`` raises
        # ``InvalidName`` → 422 ``eden://error/invalid-name`` (07-wire
        # §6.1 / 02-data-model §1.7), NOT a 400 request-validation error.
        json={"name": " leading-space"},
    )
    assert resp.status_code == 422
    assert resp.json()["type"] == "eden://error/invalid-name"


def test_register_worker_legacy_worker_id_field_rejected(
    app: Any,
) -> None:
    """The caller no longer supplies a worker_id; the field is rejected (extra=forbid)."""
    test_client = TestClient(app)
    resp = test_client.post(
        f"/v0/experiments/{EXPERIMENT_ID}/workers",
        headers={
            "X-Eden-Experiment-Id": EXPERIMENT_ID,
            "Authorization": f"Bearer admin:{ADMIN_TOKEN}",
        },
        json={"worker_id": "wkr_0000000000000000000000000a"},
    )
    assert resp.status_code == 400
    assert resp.json()["type"] == "eden://error/bad-request"
