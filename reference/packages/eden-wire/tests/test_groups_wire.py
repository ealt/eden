"""Wire tests for group-registry endpoints (chapter 7 §7).

Round-trip register / mutate / delete / list / resolve via
``StoreClient`` + ``InMemoryStore`` behind FastAPI.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from eden_storage import CycleDetected, InMemoryStore, NotFound
from eden_wire import StoreClient, make_app
from fastapi.testclient import TestClient

EXPERIMENT_ID = "exp_eksmrv69a1hywbdbz4hq8xp082"
ADMIN_TOKEN = "groups-admin"


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore(experiment_id=EXPERIMENT_ID)


@pytest.fixture
def app(store: InMemoryStore) -> Any:
    return make_app(store, admin_token=ADMIN_TOKEN)


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
def admin_client(app: Any) -> StoreClient:
    transport = _proxy(TestClient(app))
    http = httpx.Client(transport=transport, base_url="http://unused")
    return StoreClient(
        "http://unused",
        experiment_id=EXPERIMENT_ID,
        bearer=f"admin:{ADMIN_TOKEN}",
        client=http,
    )


def _mint_worker(admin_client: StoreClient, name: str) -> str:
    """Register a worker by name and return its minted opaque ``wkr_*`` id."""
    worker, _ = admin_client.register_worker(name=name)
    return worker.worker_id


def test_register_group_minimal(admin_client: StoreClient) -> None:
    # The server mints the opaque group_id; the caller supplies a name.
    group = admin_client.register_group(name="humans")
    assert group.group_id.startswith("grp_")
    assert group.name == "humans"
    assert group.members == []


def test_register_group_with_members(admin_client: StoreClient) -> None:
    eric = _mint_worker(admin_client, "eric")
    alice = _mint_worker(admin_client, "alice")
    group = admin_client.register_group(name="team-a", members=[eric, alice])
    assert group.members == [eric, alice]


def test_add_remove_to_group(admin_client: StoreClient) -> None:
    eric = _mint_worker(admin_client, "eric")
    alice = _mint_worker(admin_client, "alice")
    group = admin_client.register_group(name="humans")
    gid = group.group_id
    admin_client.add_to_group(gid, eric)
    admin_client.add_to_group(gid, alice)
    assert admin_client.read_group(gid).members == [eric, alice]
    admin_client.remove_from_group(gid, eric)
    assert admin_client.read_group(gid).members == [alice]


def test_delete_group(admin_client: StoreClient) -> None:
    group = admin_client.register_group(name="humans")
    gid = group.group_id
    admin_client.delete_group(gid)
    with pytest.raises(NotFound):
        admin_client.read_group(gid)


def test_list_groups_filter_by_name(admin_client: StoreClient) -> None:
    minted: dict[str, str] = {}
    for nm in ["zoo", "agents", "humans"]:
        group = admin_client.register_group(name=nm)
        minted[nm] = group.group_id
    assert {g.group_id for g in admin_client.list_groups()} == set(
        minted.values()
    )
    agents = admin_client.list_groups(name="agents")
    assert [g.group_id for g in agents] == [minted["agents"]]
    assert admin_client.list_groups(name="nobody") == []


def test_register_group_cycle_rejected(admin_client: StoreClient) -> None:
    # Register A empty, then B with A as a member; closing the loop by
    # adding B to A is the cycle.
    group_a = admin_client.register_group(name="a")
    group_b = admin_client.register_group(name="b", members=[group_a.group_id])
    with pytest.raises(CycleDetected):
        admin_client.add_to_group(group_a.group_id, group_b.group_id)


def test_add_to_group_indirect_cycle(admin_client: StoreClient) -> None:
    group_c = admin_client.register_group(name="c")
    group_b = admin_client.register_group(name="b", members=[group_c.group_id])
    group_a = admin_client.register_group(name="a", members=[group_b.group_id])
    with pytest.raises(CycleDetected):
        admin_client.add_to_group(group_c.group_id, group_a.group_id)
