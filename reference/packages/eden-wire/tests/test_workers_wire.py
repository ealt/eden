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

EXPERIMENT_ID = "exp-workers"
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
    worker, token = admin_client.register_worker("eric")
    assert worker.worker_id == "eric"
    assert worker.experiment_id == EXPERIMENT_ID
    assert token is not None
    assert len(token) == 64  # 32 hex bytes


def test_register_worker_with_labels(admin_client: StoreClient) -> None:
    worker, _ = admin_client.register_worker(
        "agent-claude", labels={"role": "executor"}
    )
    assert worker.labels == {"role": "executor"}


def test_register_worker_idempotent_no_new_token(
    admin_client: StoreClient,
) -> None:
    _, first_token = admin_client.register_worker("eric")
    second, second_token = admin_client.register_worker("eric")
    assert second.worker_id == "eric"
    assert second_token is None
    assert first_token is not None


def test_read_worker_after_register(admin_client: StoreClient) -> None:
    admin_client.register_worker("eric")
    fresh = admin_client.read_worker("eric")
    assert fresh.worker_id == "eric"


def test_list_workers_sorted(admin_client: StoreClient) -> None:
    for wid in ["zoe", "alice", "bob"]:
        admin_client.register_worker(wid)
    workers = admin_client.list_workers()
    assert [w.worker_id for w in workers] == ["alice", "bob", "zoe"]


def test_reissue_credential_invalidates_prior(
    app: Any, admin_client: StoreClient
) -> None:
    _, first_token = admin_client.register_worker("eric")
    second_token = admin_client.reissue_credential("eric")
    assert first_token is not None
    assert first_token != second_token
    # The prior credential is invalid: hitting whoami with it should 401.
    test_client = TestClient(app)
    resp = test_client.get(
        f"/v0/experiments/{EXPERIMENT_ID}/whoami",
        headers={
            "X-Eden-Experiment-Id": EXPERIMENT_ID,
            "Authorization": f"Bearer eric:{first_token}",
        },
    )
    assert resp.status_code == 401
    # The new credential authenticates.
    resp = test_client.get(
        f"/v0/experiments/{EXPERIMENT_ID}/whoami",
        headers={
            "X-Eden-Experiment-Id": EXPERIMENT_ID,
            "Authorization": f"Bearer eric:{second_token}",
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"worker_id": "eric"}


def test_register_worker_reserved_returns_reserved_identifier(
    admin_client: StoreClient,
) -> None:
    """``admin`` / ``system`` / ``internal`` MUST be rejected."""
    from eden_storage import ReservedIdentifier

    for reserved in ["admin", "system", "internal"]:
        with pytest.raises(ReservedIdentifier):
            admin_client.register_worker(reserved)


def test_register_worker_grammar_violation_400(
    app: Any,
) -> None:
    """Grammar-violating ``worker_id`` returns 400 bad-request via Pydantic."""
    test_client = TestClient(app)
    resp = test_client.post(
        f"/v0/experiments/{EXPERIMENT_ID}/workers",
        headers={
            "X-Eden-Experiment-Id": EXPERIMENT_ID,
            "Authorization": f"Bearer admin:{ADMIN_TOKEN}",
        },
        json={"worker_id": "Eric"},
    )
    assert resp.status_code == 400
    assert resp.json()["type"] == "eden://error/bad-request"
