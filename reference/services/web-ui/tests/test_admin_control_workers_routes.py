"""Routes for `/admin/control/workers/` — deployment-scoped registry (#146).

Exercises list / register / detail / reissue flows against an
in-memory control-plane store, mirroring the
`test_admin_experiments_routes.py` adapter shape.
"""

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
    """Test adapter that ducktypes as ControlPlaneClient for routes.

    Delegates to an `InMemoryControlPlaneStore`. Translates the
    store's tuple/str returns into the client's dict-shaped returns
    so the routes' parsing paths exercise correctly.
    """

    def __init__(self, store: InMemoryControlPlaneStore) -> None:
        self._store = store

    # --- workers ---------------------------------------------------

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

    # --- groups ----------------------------------------------------

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
        r = client.get("/admin/control/workers/", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/signin"


def test_detail_redirects_unauthenticated(
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
        r = client.get("/admin/control/workers/alice/", follow_redirects=False)
        assert r.status_code == 303


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
            "/admin/control/workers/",
            data={"csrf_token": "bogus", "worker_id": "alice"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/signin"


# ---------------------------------------------------------------------
# List view
# ---------------------------------------------------------------------


def test_list_empty_renders(signed_in_client: TestClient) -> None:
    r = signed_in_client.get("/admin/control/workers/")
    assert r.status_code == 200
    assert "deployment-scoped workers" in r.text
    assert "no workers match" in r.text


def test_list_includes_registered_workers(
    signed_in_client: TestClient, cp_store: InMemoryControlPlaneStore
) -> None:
    cp_store.register_worker("alice")
    cp_store.register_worker("bob", labels={"role": "ideator"})
    r = signed_in_client.get("/admin/control/workers/")
    assert r.status_code == 200
    assert "alice" in r.text
    assert "bob" in r.text
    assert "role=ideator" in r.text


def test_list_filter_substring(
    signed_in_client: TestClient, cp_store: InMemoryControlPlaneStore
) -> None:
    cp_store.register_worker("alice-xyz")
    cp_store.register_worker("bob")
    r = signed_in_client.get("/admin/control/workers/?q=xyz")
    assert r.status_code == 200
    assert "alice-xyz" in r.text
    assert "bob" not in r.text


def test_list_shows_group_membership(
    signed_in_client: TestClient, cp_store: InMemoryControlPlaneStore
) -> None:
    worker, _ = cp_store.register_worker("alice")
    group = cp_store.register_group(
        "team-cp", members=[worker.worker_id]
    )
    r = signed_in_client.get("/admin/control/workers/")
    assert r.status_code == 200
    # The detail-page link for the membership group is present.
    assert f"/admin/control/groups/{group.group_id}/" in r.text


# ---------------------------------------------------------------------
# Register POST
# ---------------------------------------------------------------------


def test_register_round_trips_and_shows_token(
    signed_in_client: TestClient, cp_store: InMemoryControlPlaneStore
) -> None:
    csrf = _csrf_token(signed_in_client)
    r = signed_in_client.post(
        "/admin/control/workers/",
        data={
            "csrf_token": csrf,
            "name": "alice",
            "labels": "role=ideator",
        },
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert "registration token" in r.text
    assert "alice" in r.text
    # Worker is now in the registry (matched by name, minted id).
    assert len(cp_store.list_workers(name="alice")) == 1
    # Every register mints a fresh worker (no id-based idempotency).
    r2 = signed_in_client.post(
        "/admin/control/workers/",
        data={"csrf_token": csrf, "name": "alice", "labels": ""},
        follow_redirects=False,
    )
    assert r2.status_code == 200
    assert '<code class="token">' in r2.text
    assert len(cp_store.list_workers(name="alice")) == 2


def test_register_csrf_failure_returns_403(
    signed_in_client: TestClient,
) -> None:
    r = signed_in_client.post(
        "/admin/control/workers/",
        data={"csrf_token": "bogus", "name": "alice"},
        follow_redirects=False,
    )
    assert r.status_code == 403


def test_register_reserved_name_rejected(
    signed_in_client: TestClient,
) -> None:
    csrf = _csrf_token(signed_in_client)
    r = signed_in_client.post(
        "/admin/control/workers/",
        data={"csrf_token": csrf, "name": "admin", "labels": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "reserved-name" in r.headers["location"]


def test_register_invalid_name_rejected(
    signed_in_client: TestClient,
) -> None:
    csrf = _csrf_token(signed_in_client)
    r = signed_in_client.post(
        "/admin/control/workers/",
        data={"csrf_token": csrf, "name": " leading-space", "labels": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "invalid-name" in r.headers["location"]


def test_register_invalid_labels_surface_line_number(
    signed_in_client: TestClient,
) -> None:
    csrf = _csrf_token(signed_in_client)
    r = signed_in_client.post(
        "/admin/control/workers/",
        data={
            "csrf_token": csrf,
            "name": "alice",
            "labels": "role=ideator\nbogus-no-equals",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "invalid-labels-line" in r.headers["location"]
    assert "line=2" in r.headers["location"]


# ---------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------


def test_detail_renders_for_existing_worker(
    signed_in_client: TestClient, cp_store: InMemoryControlPlaneStore
) -> None:
    worker, _ = cp_store.register_worker("alice", labels={"role": "ideator"})
    r = signed_in_client.get(f"/admin/control/workers/{worker.worker_id}/")
    assert r.status_code == 200
    assert "alice" in r.text
    assert "role=ideator" in r.text
    assert "reissue credential" in r.text


def test_detail_404_for_missing_worker(
    signed_in_client: TestClient,
) -> None:
    r = signed_in_client.get(
        "/admin/control/workers/wkr_never000000000000000000000/"
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------
# Reissue
# ---------------------------------------------------------------------


def test_reissue_round_trips(
    signed_in_client: TestClient, cp_store: InMemoryControlPlaneStore
) -> None:
    worker, _ = cp_store.register_worker("alice")
    csrf = _csrf_token(signed_in_client)
    r = signed_in_client.post(
        f"/admin/control/workers/{worker.worker_id}/reissue-credential",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert "credential reissued" in r.text
    assert "registration token" in r.text


def test_reissue_csrf_failure_returns_403(
    signed_in_client: TestClient, cp_store: InMemoryControlPlaneStore
) -> None:
    worker, _ = cp_store.register_worker("alice")
    r = signed_in_client.post(
        f"/admin/control/workers/{worker.worker_id}/reissue-credential",
        data={"csrf_token": "bogus"},
        follow_redirects=False,
    )
    assert r.status_code == 403


def test_reissue_not_found_redirects(
    signed_in_client: TestClient,
) -> None:
    csrf = _csrf_token(signed_in_client)
    r = signed_in_client.post(
        "/admin/control/workers/wkr_never000000000000000000000/reissue-credential",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "not-found" in r.headers["location"]


# ---------------------------------------------------------------------
# Wiring: routes are NOT registered without control_plane
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
        r = client.get("/admin/control/workers/")
        assert r.status_code == 404
