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

EXPERIMENT_ID = "exp-groups"
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


def test_register_group_minimal(admin_client: StoreClient) -> None:
    group = admin_client.register_group("humans")
    assert group.group_id == "humans"
    assert group.members == []


def test_register_group_with_members(admin_client: StoreClient) -> None:
    group = admin_client.register_group("team-a", members=["eric", "alice"])
    assert group.members == ["eric", "alice"]


def test_add_remove_to_group(admin_client: StoreClient) -> None:
    admin_client.register_group("humans")
    admin_client.add_to_group("humans", "eric")
    admin_client.add_to_group("humans", "alice")
    assert admin_client.read_group("humans").members == ["eric", "alice"]
    admin_client.remove_from_group("humans", "eric")
    assert admin_client.read_group("humans").members == ["alice"]


def test_delete_group(admin_client: StoreClient) -> None:
    admin_client.register_group("humans")
    admin_client.delete_group("humans")
    with pytest.raises(NotFound):
        admin_client.read_group("humans")


def test_list_groups(admin_client: StoreClient) -> None:
    for gid in ["zoo", "agents", "humans"]:
        admin_client.register_group(gid)
    groups = admin_client.list_groups()
    assert [g.group_id for g in groups] == ["agents", "humans", "zoo"]


def test_register_group_cycle_rejected(admin_client: StoreClient) -> None:
    admin_client.register_group("a", members=["b"])
    with pytest.raises(CycleDetected):
        admin_client.register_group("b", members=["a"])


def test_add_to_group_indirect_cycle(admin_client: StoreClient) -> None:
    admin_client.register_group("a", members=["b"])
    admin_client.register_group("b", members=["c"])
    admin_client.register_group("c")
    with pytest.raises(CycleDetected):
        admin_client.add_to_group("c", "a")
