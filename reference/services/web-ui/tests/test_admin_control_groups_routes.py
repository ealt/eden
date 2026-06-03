"""Routes for `/admin/control/groups/` — deployment-scoped registry (#146)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import _one_experiment_factory
from eden_contracts import ExperimentConfig
from eden_control_plane import (
    ControlPlaneClient,
    InMemoryControlPlaneStore,
)
from eden_service_common import load_experiment_config
from eden_storage import InMemoryStore
from eden_web_ui import make_app as make_web_ui_app
from fastapi.testclient import TestClient

EXPERIMENT_ID = "exp_0123456789abcdefghjkmnpqrs"
WORKER_NAME = "auto-orchestrator-1"
SESSION_SECRET = "test-session-secret-padding-padding-padding"

_FIXTURE_CONFIG = (
    Path(__file__).resolve().parents[4]
    / "tests"
    / "fixtures"
    / "experiment"
    / ".eden"
    / "config.yaml"
)


def _config() -> ExperimentConfig:
    return load_experiment_config(str(_FIXTURE_CONFIG))


class _StoreBackedClient:
    """Same shape as in `test_admin_control_workers_routes.py`."""

    def __init__(self, store: InMemoryControlPlaneStore) -> None:
        self._store = store

    def list_workers(self, *, name: str | None = None):  # noqa: ANN201
        return self._store.list_workers(name=name)

    def read_worker(self, worker_id: str):  # noqa: ANN201
        return self._store.read_worker(worker_id)

    def register_worker(
        self, name: str | None = None, *, labels: dict[str, str] | None = None
    ) -> dict[str, Any]:
        worker, token = self._store.register_worker(name, labels=labels)
        out: dict[str, Any] = worker.model_dump(mode="json", exclude_none=True)
        if token is not None:
            out["registration_token"] = token
        return out

    def reissue_credential(self, worker_id: str) -> dict[str, Any]:
        token = self._store.reissue_credential(worker_id)
        worker = self._store.read_worker(worker_id)
        out: dict[str, Any] = worker.model_dump(mode="json", exclude_none=True)
        out["registration_token"] = token
        return out

    def list_groups(self, *, name: str | None = None):  # noqa: ANN201
        return self._store.list_groups(name=name)

    def read_group(self, group_id: str):  # noqa: ANN201
        return self._store.read_group(group_id)

    def register_group(self, name=None, *, members=None):  # noqa: ANN201, ANN001
        return self._store.register_group(name, members=members)

    def add_to_group(self, group_id: str, member_id: str):  # noqa: ANN201
        return self._store.add_to_group(group_id, member_id)

    def remove_from_group(self, group_id: str, member_id: str):  # noqa: ANN201
        return self._store.remove_from_group(group_id, member_id)

    def delete_group(self, group_id: str) -> None:
        self._store.delete_group(group_id)


@pytest.fixture
def cp_store() -> InMemoryControlPlaneStore:
    return InMemoryControlPlaneStore()


@pytest.fixture
def cp_client(cp_store: InMemoryControlPlaneStore) -> ControlPlaneClient:
    return _StoreBackedClient(cp_store)  # type: ignore[return-value]


@pytest.fixture
def store() -> InMemoryStore:
    s = InMemoryStore(experiment_id=EXPERIMENT_ID)
    # Issue #144: the /admin/* middleware gates on admins-group
    # membership; signed_in_client posts /signin as the minted session
    # worker id, which must be a member of the `admins` group (#128).
    worker, _ = s.register_worker(WORKER_NAME)
    s.register_group("admins", members=[worker.worker_id], allow_reserved=True)
    s._session_worker_id = worker.worker_id  # type: ignore[attr-defined]
    return s


def _session_worker_id(store: InMemoryStore) -> str:
    return store._session_worker_id  # type: ignore[attr-defined]


@pytest.fixture
def artifacts_dir(tmp_path: Path) -> Path:
    out = tmp_path / "artifacts"
    out.mkdir()
    return out


@pytest.fixture
def signed_in_client(
    store: InMemoryStore,
    artifacts_dir: Path,
    cp_client: ControlPlaneClient,
) -> Iterator[TestClient]:
    app = make_web_ui_app(
        store_factory=_one_experiment_factory(store, admin_store=store),
        experiment_id=EXPERIMENT_ID,
        experiment_config=_config(),
        worker_id=_session_worker_id(store),
        session_secret=SESSION_SECRET,
        claim_ttl_seconds=3600,
        artifacts_dir=artifacts_dir,
        control_plane=cp_client,
    )
    with TestClient(app) as client:
        signin = client.post("/signin", follow_redirects=False)
        assert signin.status_code == 303
        yield client


def _csrf_token(client: TestClient) -> str:
    from eden_web_ui.sessions import SessionCodec

    raw = client.cookies.get("eden_web_ui_session")
    assert raw is not None
    session = SessionCodec(SESSION_SECRET).decode(raw)
    assert session is not None
    return session.csrf


# ---------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------


def test_list_redirects_unauthenticated(
    store: InMemoryStore,
    artifacts_dir: Path,
    cp_client: ControlPlaneClient,
) -> None:
    app = make_web_ui_app(
        store_factory=_one_experiment_factory(store),
        experiment_id=EXPERIMENT_ID,
        experiment_config=_config(),
        worker_id=_session_worker_id(store),
        session_secret=SESSION_SECRET,
        claim_ttl_seconds=3600,
        artifacts_dir=artifacts_dir,
        control_plane=cp_client,
    )
    with TestClient(app) as client:
        r = client.get("/admin/control/groups/", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/signin"


def test_register_unauthenticated_redirects_before_csrf(
    store: InMemoryStore,
    artifacts_dir: Path,
    cp_client: ControlPlaneClient,
) -> None:
    app = make_web_ui_app(
        store_factory=_one_experiment_factory(store),
        experiment_id=EXPERIMENT_ID,
        experiment_config=_config(),
        worker_id=_session_worker_id(store),
        session_secret=SESSION_SECRET,
        claim_ttl_seconds=3600,
        artifacts_dir=artifacts_dir,
        control_plane=cp_client,
    )
    with TestClient(app) as client:
        r = client.post(
            "/admin/control/groups/",
            data={"csrf_token": "bogus", "group_id": "g1"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/signin"


# ---------------------------------------------------------------------
# List view
# ---------------------------------------------------------------------


def test_list_empty_renders(signed_in_client: TestClient) -> None:
    r = signed_in_client.get("/admin/control/groups/")
    assert r.status_code == 200
    assert "deployment-scoped groups" in r.text
    assert "no groups match" in r.text


def test_list_renders_registered_groups(
    signed_in_client: TestClient, cp_store: InMemoryControlPlaneStore
) -> None:
    worker, _ = cp_store.register_worker("alice")
    cp_store.register_group("team-list", members=[worker.worker_id])
    r = signed_in_client.get("/admin/control/groups/")
    assert r.status_code == 200
    assert "team-list" in r.text


def test_list_filter_substring(
    signed_in_client: TestClient, cp_store: InMemoryControlPlaneStore
) -> None:
    cp_store.register_group("team-admins")
    cp_store.register_group("evaluators")
    r = signed_in_client.get("/admin/control/groups/?q=admin")
    assert r.status_code == 200
    assert "team-admins" in r.text
    assert "evaluators" not in r.text


# ---------------------------------------------------------------------
# Register POST
# ---------------------------------------------------------------------


def test_register_round_trips(
    signed_in_client: TestClient, cp_store: InMemoryControlPlaneStore
) -> None:
    csrf = _csrf_token(signed_in_client)
    r = signed_in_client.post(
        "/admin/control/groups/",
        data={"csrf_token": csrf, "name": "team-reg", "members": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith("/admin/control/groups/grp_")
    assert loc.endswith("/?ok=registered")
    assert len(cp_store.list_groups(name="team-reg")) == 1


def test_register_with_initial_members(
    signed_in_client: TestClient, cp_store: InMemoryControlPlaneStore
) -> None:
    alice, _ = cp_store.register_worker("alice")
    bob, _ = cp_store.register_worker("bob")
    csrf = _csrf_token(signed_in_client)
    r = signed_in_client.post(
        "/admin/control/groups/",
        data={
            "csrf_token": csrf,
            "name": "team-init",
            "members": f"{alice.worker_id}\n{bob.worker_id}\n# comment\n",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    grp = cp_store.list_groups(name="team-init")[0]
    assert grp.members == [alice.worker_id, bob.worker_id]


def test_register_csrf_failure_returns_403(
    signed_in_client: TestClient,
) -> None:
    r = signed_in_client.post(
        "/admin/control/groups/",
        data={"csrf_token": "bogus", "name": "team-x", "members": ""},
        follow_redirects=False,
    )
    assert r.status_code == 403


def test_register_reserved_name_rejected(
    signed_in_client: TestClient,
) -> None:
    csrf = _csrf_token(signed_in_client)
    r = signed_in_client.post(
        "/admin/control/groups/",
        data={"csrf_token": csrf, "name": "admins", "members": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "reserved-name" in r.headers["location"]


def test_register_invalid_member_id(
    signed_in_client: TestClient,
) -> None:
    csrf = _csrf_token(signed_in_client)
    r = signed_in_client.post(
        "/admin/control/groups/",
        data={
            "csrf_token": csrf,
            "name": "team-bad",
            "members": "BadMember",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "invalid-members" in r.headers["location"]


# ---------------------------------------------------------------------
# Detail + mutations
# ---------------------------------------------------------------------


def test_detail_renders(
    signed_in_client: TestClient, cp_store: InMemoryControlPlaneStore
) -> None:
    alice, _ = cp_store.register_worker("alice")
    group = cp_store.register_group("team-d", members=[alice.worker_id])
    r = signed_in_client.get(f"/admin/control/groups/{group.group_id}/")
    assert r.status_code == 200
    assert "team-d" in r.text
    assert "alice" in r.text
    assert "transitive worker closure" in r.text


def test_detail_404_for_missing(
    signed_in_client: TestClient,
) -> None:
    r = signed_in_client.get(
        "/admin/control/groups/grp_never000000000000000000000/"
    )
    assert r.status_code == 404


def test_add_member_round_trips(
    signed_in_client: TestClient, cp_store: InMemoryControlPlaneStore
) -> None:
    alice, _ = cp_store.register_worker("alice")
    group = cp_store.register_group("team-add")
    csrf = _csrf_token(signed_in_client)
    r = signed_in_client.post(
        f"/admin/control/groups/{group.group_id}/members",
        data={"csrf_token": csrf, "member_id": alice.worker_id},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "ok=added" in r.headers["location"]
    assert cp_store.read_group(group.group_id).members == [alice.worker_id]


def test_add_member_csrf_failure_returns_403(
    signed_in_client: TestClient, cp_store: InMemoryControlPlaneStore
) -> None:
    group = cp_store.register_group("team-add-csrf")
    r = signed_in_client.post(
        f"/admin/control/groups/{group.group_id}/members",
        data={"csrf_token": "bogus", "member_id": "wkr_x"},
        follow_redirects=False,
    )
    assert r.status_code == 403


def test_add_member_invalid_member_id(
    signed_in_client: TestClient, cp_store: InMemoryControlPlaneStore
) -> None:
    group = cp_store.register_group("team-add-bad")
    csrf = _csrf_token(signed_in_client)
    r = signed_in_client.post(
        f"/admin/control/groups/{group.group_id}/members",
        data={"csrf_token": csrf, "member_id": "Bad"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "invalid-member-id" in r.headers["location"]


def test_remove_member_round_trips(
    signed_in_client: TestClient, cp_store: InMemoryControlPlaneStore
) -> None:
    alice, _ = cp_store.register_worker("alice")
    group = cp_store.register_group("team-rm", members=[alice.worker_id])
    csrf = _csrf_token(signed_in_client)
    r = signed_in_client.post(
        f"/admin/control/groups/{group.group_id}/members/{alice.worker_id}/remove",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "ok=removed" in r.headers["location"]
    assert cp_store.read_group(group.group_id).members == []


def test_delete_group_round_trips(
    signed_in_client: TestClient, cp_store: InMemoryControlPlaneStore
) -> None:
    group = cp_store.register_group("team-del")
    csrf = _csrf_token(signed_in_client)
    r = signed_in_client.post(
        f"/admin/control/groups/{group.group_id}/delete",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "ok=deleted" in r.headers["location"]
    assert group.group_id not in [g.group_id for g in cp_store.list_groups()]


def test_delete_group_not_found_redirects(
    signed_in_client: TestClient,
) -> None:
    csrf = _csrf_token(signed_in_client)
    r = signed_in_client.post(
        "/admin/control/groups/grp_never000000000000000000000/delete",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "group-not-found" in r.headers["location"]


# ---------------------------------------------------------------------
# Wiring: routes hidden without control_plane
# ---------------------------------------------------------------------


def test_routes_hidden_when_control_plane_unset(
    store: InMemoryStore, artifacts_dir: Path
) -> None:
    app = make_web_ui_app(
        store_factory=_one_experiment_factory(store),
        experiment_id=EXPERIMENT_ID,
        experiment_config=_config(),
        worker_id=_session_worker_id(store),
        session_secret=SESSION_SECRET,
        claim_ttl_seconds=3600,
        artifacts_dir=artifacts_dir,
    )
    with TestClient(app) as client:
        client.post("/signin", follow_redirects=False)
        r = client.get("/admin/control/groups/")
        assert r.status_code == 404
