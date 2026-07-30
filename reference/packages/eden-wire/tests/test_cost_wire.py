"""Wire coverage for the reference-only cost routes (issue #343).

``POST`` / ``GET /_reference/experiments/{E}/cost`` are the surface a
subprocess-mode worker host uses when its store lives across the wire,
so the round-trip that matters is ``StoreClient.record_cost`` →
``list_cost_entries`` returning the same figures a direct backend call
would. The auth cases are here because the auth middleware deliberately
skips ``/_reference/`` paths — these handlers gate themselves, and a
regression there would silently open an unauthenticated write.
"""

from __future__ import annotations

import pytest
from eden_storage import CostEntry, InMemoryStore
from eden_wire import StoreClient, make_app
from fastapi.testclient import TestClient

EXPERIMENT_ID = "exp_zp0q3v6xsnk0jf9hfb54m73626"
ADMIN_TOKEN = "admin-secret"  # noqa: S105 — test fixture


def _entry(entry_id: str = "e1", **overrides: object) -> CostEntry:
    fields: dict[str, object] = {
        "entry_id": entry_id,
        "experiment_id": EXPERIMENT_ID,
        "role": "executor",
        "source": "claude-code-stream-json",
        "task_id": "execution-1",
        "variant_id": "variant-1",
        "idea_id": "idea-1",
        "model": "claude-sonnet-4-6",
        "total_cost_usd": 0.1233009,
        "input_tokens": 6,
        "output_tokens": 637,
        "num_turns": 4,
        "duration_ms": 18118,
    }
    fields.update(overrides)
    return CostEntry.model_validate(fields)


def _cost_url() -> str:
    return f"/_reference/experiments/{EXPERIMENT_ID}/cost"


def _headers() -> dict[str, str]:
    return {"X-Eden-Experiment-Id": EXPERIMENT_ID}


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore(experiment_id=EXPERIMENT_ID)


@pytest.fixture
def client(store: InMemoryStore) -> TestClient:
    return TestClient(make_app(store), base_url="http://wire.test")


@pytest.fixture
def store_client(client: TestClient) -> StoreClient:
    return StoreClient("http://wire.test", EXPERIMENT_ID, client=client)


# ----------------------------------------------------------------------
# Round-trip through StoreClient
# ----------------------------------------------------------------------


def test_client_round_trip_preserves_every_figure(
    store_client: StoreClient,
) -> None:
    store_client.record_cost(_entry())

    (read,) = store_client.list_cost_entries()
    assert read.entry_id == "e1"
    assert read.role == "executor"
    assert read.source == "claude-code-stream-json"
    assert read.task_id == "execution-1"
    assert read.variant_id == "variant-1"
    assert read.idea_id == "idea-1"
    assert read.model == "claude-sonnet-4-6"
    assert read.total_cost_usd == pytest.approx(0.1233009)
    assert read.input_tokens == 6
    assert read.output_tokens == 637
    assert read.num_turns == 4
    assert read.duration_ms == 18118
    assert read.recorded_at is not None


def test_client_write_reaches_the_backing_store(
    store: InMemoryStore, store_client: StoreClient
) -> None:
    """The wire write lands in the server's ledger, not a client cache."""
    store_client.record_cost(_entry())
    assert [e.entry_id for e in store.list_cost_entries()] == ["e1"]


def test_client_filters_are_forwarded(store_client: StoreClient) -> None:
    store_client.record_cost(_entry("e1", role="executor", variant_id="v1"))
    store_client.record_cost(_entry("e2", role="evaluator", variant_id="v1"))
    store_client.record_cost(_entry("e3", role="executor", variant_id="v2"))

    assert {e.entry_id for e in store_client.list_cost_entries()} == {
        "e1",
        "e2",
        "e3",
    }
    assert {
        e.entry_id for e in store_client.list_cost_entries(role="executor")
    } == {"e1", "e3"}
    assert {
        e.entry_id for e in store_client.list_cost_entries(variant_id="v1")
    } == {"e1", "e2"}
    assert {
        e.entry_id
        for e in store_client.list_cost_entries(role="executor", variant_id="v1")
    } == {"e1"}


def test_client_repeat_record_is_idempotent(store_client: StoreClient) -> None:
    """A retry after a lost response must not double the reported spend."""
    store_client.record_cost(_entry("e1", total_cost_usd=0.5))
    store_client.record_cost(_entry("e1", total_cost_usd=0.5))

    entries = store_client.list_cost_entries()
    assert len(entries) == 1
    assert entries[0].total_cost_usd == pytest.approx(0.5)


def test_partial_entry_omits_absent_figures_on_the_wire(
    client: TestClient, store_client: StoreClient
) -> None:
    """Absent figures stay absent rather than serializing as null."""
    store_client.record_cost(
        CostEntry(
            entry_id="e1",
            experiment_id=EXPERIMENT_ID,
            role="ideator",
            source="worker-reported",
            task_id="ideation-1",
            input_tokens=100,
        )
    )
    body = client.get(_cost_url(), headers=_headers()).json()
    (row,) = body["entries"]
    assert "total_cost_usd" not in row
    assert "variant_id" not in row
    assert row["input_tokens"] == 100


# ----------------------------------------------------------------------
# Request validation
# ----------------------------------------------------------------------


def test_unknown_field_is_rejected(client: TestClient) -> None:
    """``extra="forbid"`` on the entry applies at the wire boundary too."""
    resp = client.post(
        _cost_url(),
        headers=_headers(),
        json={
            "entry_id": "e1",
            "experiment_id": EXPERIMENT_ID,
            "role": "executor",
            "source": "worker-reported",
            "task_id": "t1",
            "cost_usd": 0.5,
        },
    )
    assert resp.status_code == 400
    assert resp.json()["type"] == "eden://error/bad-request"


def test_unknown_role_is_rejected(client: TestClient) -> None:
    resp = client.post(
        _cost_url(),
        headers=_headers(),
        json={
            "entry_id": "e1",
            "experiment_id": EXPERIMENT_ID,
            "role": "integrator",
            "source": "worker-reported",
            "task_id": "t1",
            "total_cost_usd": 0.5,
        },
    )
    assert resp.status_code == 400
    assert resp.json()["type"] == "eden://error/bad-request"


def test_experiment_id_mismatch_is_rejected(client: TestClient) -> None:
    """The ledger belongs to one experiment; a foreign entry is refused."""
    resp = client.post(
        _cost_url(),
        headers=_headers(),
        json=_entry(experiment_id="exp_other").model_dump(
            mode="json", exclude_none=True
        ),
    )
    assert resp.status_code == 409
    assert resp.json()["type"] == "eden://error/invalid-precondition"


def test_missing_experiment_header_is_rejected(client: TestClient) -> None:
    resp = client.post(
        _cost_url(), json=_entry().model_dump(mode="json", exclude_none=True)
    )
    assert resp.status_code == 400
    assert resp.json()["type"] == "eden://error/experiment-id-mismatch"


# ----------------------------------------------------------------------
# Auth — the middleware skips /_reference/, so the handlers gate
# ----------------------------------------------------------------------


def _authed_client(store: InMemoryStore) -> TestClient:
    return TestClient(make_app(store, admin_token=ADMIN_TOKEN))


def _register_worker(client: TestClient) -> tuple[str, str]:
    resp = client.post(
        f"/v0/experiments/{EXPERIMENT_ID}/workers",
        headers={**_headers(), "Authorization": f"Bearer admin:{ADMIN_TOKEN}"},
        json={"name": "alice"},
    )
    assert resp.status_code == 200
    body = resp.json()
    return body["worker_id"], body["registration_token"]


def test_admin_bearer_can_write_and_read(store: InMemoryStore) -> None:
    client = _authed_client(store)
    auth = {**_headers(), "Authorization": f"Bearer admin:{ADMIN_TOKEN}"}
    assert (
        client.post(
            _cost_url(),
            headers=auth,
            json=_entry().model_dump(mode="json", exclude_none=True),
        ).status_code
        == 204
    )
    assert len(client.get(_cost_url(), headers=auth).json()["entries"]) == 1


def test_worker_bearer_can_write(store: InMemoryStore) -> None:
    """Workers spend the money, so workers may record it."""
    client = _authed_client(store)
    worker_id, token = _register_worker(client)
    resp = client.post(
        _cost_url(),
        headers={**_headers(), "Authorization": f"Bearer {worker_id}:{token}"},
        json=_entry().model_dump(mode="json", exclude_none=True),
    )
    assert resp.status_code == 204


@pytest.mark.parametrize(
    "header",
    [
        None,
        "Basic abc",
        "Bearer no-colon",
        "Bearer admin:wrong-token",
        "Bearer ghost:nonexistent",
    ],
)
def test_bad_bearer_cannot_write_or_read(
    store: InMemoryStore, header: str | None
) -> None:
    client = _authed_client(store)
    headers = dict(_headers())
    if header is not None:
        headers["Authorization"] = header

    post = client.post(
        _cost_url(),
        headers=headers,
        json=_entry().model_dump(mode="json", exclude_none=True),
    )
    assert post.status_code == 401
    assert client.get(_cost_url(), headers=headers).status_code == 401
    assert store.list_cost_entries() == []
