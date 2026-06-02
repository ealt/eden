"""Wave 5 — cross-experiment admin routes.

Exercise the `/admin/experiments/` views against an in-memory
control plane (no real HTTP). The fixture wires the web-ui's
`control_plane` injection point to a `ControlPlaneClient` that
talks to an in-process `InMemoryControlPlaneStore` via an
`httpx.MockTransport` carrying the `eden_control_plane_server`
FastAPI app.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

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

EXPERIMENT_ID = "exp-1"
WORKER_ID = "auto-orchestrator-1"
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


@pytest.fixture
def cp_store() -> InMemoryControlPlaneStore:
    return InMemoryControlPlaneStore()


class _StoreBackedClient:
    """Thin ControlPlaneClient-shaped adapter that delegates directly to the store.

    The wire-layer tests in `reference/services/control-plane/tests/`
    cover the HTTP surface. The web-ui tests only need a callable
    matching the methods the admin_experiments routes use, so this
    adapter keeps the test focused on web-ui behavior without booting
    the control-plane app or wrestling with sync vs. async transports.
    """

    def __init__(self, store: InMemoryControlPlaneStore) -> None:
        self._store = store

    def list_experiments(self):  # noqa: ANN201
        return self._store.list_experiments()

    def read_experiment_metadata(self, experiment_id: str):  # noqa: ANN201
        return self._store.read_experiment_metadata(experiment_id)

    def register_experiment(self, experiment_id: str, config_uri: str):  # noqa: ANN201
        # ControlPlaneClient returns just `RegisteredExperiment`; the
        # store-side Protocol now returns `(entry, created)` to support
        # the 201/200 atomic decision. Adapt by unpacking the tuple.
        entry, _created = self._store.register_experiment(
            experiment_id, config_uri
        )
        return entry

    def unregister_experiment(self, experiment_id: str) -> None:
        self._store.unregister_experiment(experiment_id)


@pytest.fixture
def cp_client(cp_store: InMemoryControlPlaneStore) -> ControlPlaneClient:
    """A store-backed adapter; ducktypes as ControlPlaneClient for routes."""
    return _StoreBackedClient(cp_store)  # type: ignore[return-value]


@pytest.fixture
def store() -> InMemoryStore:
    s = InMemoryStore(experiment_id=EXPERIMENT_ID)
    # Issue #144: the /admin/* middleware gates on admins-group
    # membership; signed_in_client posts /signin as WORKER_ID so
    # WORKER_ID must be a member of the `admins` group.
    s.register_worker(WORKER_ID)
    s.register_group("admins", members=[WORKER_ID])
    return s


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
        worker_id=WORKER_ID,
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
    """Pull the CSRF token from the current session cookie."""
    from eden_web_ui.sessions import SessionCodec

    raw = client.cookies.get("eden_web_ui_session")
    assert raw is not None
    session = SessionCodec(SESSION_SECRET).decode(raw)
    assert session is not None
    return session.csrf


# ---------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------


def test_dashboard_renders_empty_registry(signed_in_client: TestClient) -> None:
    r = signed_in_client.get("/admin/experiments/")
    assert r.status_code == 200
    assert "No experiments registered" in r.text


def test_dashboard_lists_registered_experiments(
    signed_in_client: TestClient,
    cp_store: InMemoryControlPlaneStore,
) -> None:
    cp_store.register_experiment("exp-a", "file:///etc/a.yaml")
    cp_store.register_experiment("exp-b", "file:///etc/b.yaml")
    r = signed_in_client.get("/admin/experiments/")
    assert r.status_code == 200
    assert "exp-a" in r.text
    assert "exp-b" in r.text


def test_dashboard_redirects_unauthenticated(
    store: InMemoryStore,
    artifacts_dir: Path,
    cp_client: ControlPlaneClient,
) -> None:
    """No session cookie → /signin redirect."""
    app = make_web_ui_app(
        store_factory=_one_experiment_factory(store),
        experiment_id=EXPERIMENT_ID,
        experiment_config=_config(),
        worker_id=WORKER_ID,
        session_secret=SESSION_SECRET,
        claim_ttl_seconds=3600,
        artifacts_dir=artifacts_dir,
        control_plane=cp_client,
    )
    with TestClient(app) as client:
        r = client.get("/admin/experiments/", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/signin"


def test_dashboard_surfaces_lease_holder(
    signed_in_client: TestClient,
    cp_store: InMemoryControlPlaneStore,
) -> None:
    cp_store.register_experiment("exp-a", "file:///etc/a.yaml")
    cp_store.acquire_lease(
        "exp-a", "auto-orchestrator-x", "uuid-1", lease_duration_seconds=30
    )
    r = signed_in_client.get("/admin/experiments/")
    assert r.status_code == 200
    assert "auto-orchestrator-x" in r.text


# ---------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------


def test_register_experiment_round_trips(
    signed_in_client: TestClient,
    cp_store: InMemoryControlPlaneStore,
) -> None:
    csrf = _csrf_token(signed_in_client)
    r = signed_in_client.post(
        "/admin/experiments/register",
        data={
            "csrf_token": csrf,
            "experiment_id": "exp-new",
            "config_uri": "file:///etc/new.yaml",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/experiments/?registered=ok"
    # Verify it actually landed in the control plane.
    entries = cp_store.list_experiments()
    assert "exp-new" in [e.experiment_id for e in entries]


def test_register_experiment_rejects_bad_csrf(
    signed_in_client: TestClient,
    cp_store: InMemoryControlPlaneStore,
) -> None:
    r = signed_in_client.post(
        "/admin/experiments/register",
        data={
            "csrf_token": "wrong",
            "experiment_id": "exp-new",
            "config_uri": "file:///etc/new.yaml",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    # CSRF failure routes to the same outcome key as transport
    # failure — the banner says "refresh and verify".
    assert "transport" in r.headers["location"]
    # And the experiment was NOT registered.
    assert cp_store.list_experiments() == []


def test_register_experiment_409_already_exists(
    signed_in_client: TestClient,
    cp_store: InMemoryControlPlaneStore,
) -> None:
    cp_store.register_experiment("exp-a", "file:///etc/a.yaml")
    csrf = _csrf_token(signed_in_client)
    r = signed_in_client.post(
        "/admin/experiments/register",
        data={
            "csrf_token": csrf,
            "experiment_id": "exp-a",
            "config_uri": "file:///etc/DIFFERENT.yaml",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "already-exists" in r.headers["location"]


def test_unregister_blocked_while_running(
    signed_in_client: TestClient,
    cp_store: InMemoryControlPlaneStore,
) -> None:
    cp_store.register_experiment("exp-a", "file:///etc/a.yaml")
    csrf = _csrf_token(signed_in_client)
    r = signed_in_client.post(
        "/admin/experiments/exp-a/unregister",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "invalid-precondition" in r.headers["location"]


def test_unregister_succeeds_after_terminated(
    signed_in_client: TestClient,
    cp_store: InMemoryControlPlaneStore,
) -> None:
    cp_store.register_experiment("exp-a", "file:///etc/a.yaml")
    cp_store.update_last_known_state("exp-a", "terminated")
    csrf = _csrf_token(signed_in_client)
    r = signed_in_client.post(
        "/admin/experiments/exp-a/unregister",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "unregistered=ok" in r.headers["location"]
    assert cp_store.list_experiments() == []


def test_select_records_experiment_in_session(
    signed_in_client: TestClient,
    cp_store: InMemoryControlPlaneStore,
) -> None:
    cp_store.register_experiment("exp-a", "file:///etc/a.yaml")
    csrf = _csrf_token(signed_in_client)
    r = signed_in_client.post(
        "/admin/experiments/exp-a/select",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "selected=ok" in r.headers["location"]
    # Verify the new session cookie carries selected_experiment_id.
    raw = signed_in_client.cookies.get("eden_web_ui_session")
    assert raw is not None
    from eden_web_ui.sessions import SessionCodec

    session = SessionCodec(SESSION_SECRET).decode(raw)
    assert session is not None
    assert session.selected_experiment_id == "exp-a"


def test_force_release_route_is_404(
    signed_in_client: TestClient,
    cp_store: InMemoryControlPlaneStore,
) -> None:
    """Force-release was removed (chapter 11 §9 defers to a future amendment).

    The route MUST NOT be registered; a POST to the prior path
    returns 405 (no handler accepts POST) or 404. Either way, the
    operator cannot bypass natural lease expiration via the UI.
    """
    cp_store.register_experiment("exp-a", "file:///etc/a.yaml")
    lease = cp_store.acquire_lease(
        "exp-a", "auto-orchestrator-x", "uuid-1", lease_duration_seconds=30
    )
    csrf = _csrf_token(signed_in_client)
    r = signed_in_client.post(
        "/admin/experiments/exp-a/release-lease",
        data={
            "csrf_token": csrf,
            "lease_id": lease.lease_id,
            "holder_instance": "uuid-1",
        },
        follow_redirects=False,
    )
    assert r.status_code in {404, 405}
    # And the lease is STILL active in the store — UI cannot
    # bypass the natural expiration path.
    refreshed = cp_store.read_experiment_metadata("exp-a")
    assert refreshed.lease is not None


# ---------------------------------------------------------------------
# Wiring: routes are NOT registered without control_plane
# ---------------------------------------------------------------------


def test_routes_hidden_when_control_plane_unset(
    store: InMemoryStore,
    artifacts_dir: Path,
) -> None:
    """When `control_plane=None`, `/admin/experiments/` is 404."""
    app = make_web_ui_app(
        store_factory=_one_experiment_factory(store),
        experiment_id=EXPERIMENT_ID,
        experiment_config=_config(),
        worker_id=WORKER_ID,
        session_secret=SESSION_SECRET,
        claim_ttl_seconds=3600,
        artifacts_dir=artifacts_dir,
    )
    with TestClient(app) as client:
        client.post("/signin", follow_redirects=False)
        r = client.get("/admin/experiments/")
        assert r.status_code == 404


# ---------------------------------------------------------------------
# Top-nav experiment switcher + resolve-error banners (issue #145 W4)
# ---------------------------------------------------------------------


def test_switcher_widget_renders_registered_experiments(
    signed_in_client: TestClient,
    cp_store: InMemoryControlPlaneStore,
) -> None:
    cp_store.register_experiment("exp-a", "file:///etc/a.yaml")
    cp_store.register_experiment("exp-b", "file:///etc/b.yaml")
    r = signed_in_client.get("/admin/experiments/")
    assert r.status_code == 200
    assert 'class="switcher"' in r.text
    assert "/admin/experiments/exp-a/select" in r.text
    assert "/admin/experiments/exp-b/select" in r.text


def test_switcher_marks_the_selected_experiment_active(
    signed_in_client: TestClient,
    cp_store: InMemoryControlPlaneStore,
) -> None:
    cp_store.register_experiment("exp-a", "file:///etc/a.yaml")
    csrf = _csrf_token(signed_in_client)
    signed_in_client.post(
        "/admin/experiments/exp-a/select",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    r = signed_in_client.get("/admin/experiments/")
    assert "Active:" in r.text
    assert "exp-a" in r.text


def test_dashboard_renders_stale_selection_banner(
    signed_in_client: TestClient,
) -> None:
    r = signed_in_client.get("/admin/experiments/?error=stale-selection")
    assert r.status_code == 200
    assert "no longer registered" in r.text


def test_dashboard_renders_switched_mid_form_banner(
    signed_in_client: TestClient,
) -> None:
    r = signed_in_client.get(
        "/admin/experiments/?error=switched-mid-form&from=exp-x&to=exp-y"
    )
    assert r.status_code == 200
    assert "exp-x" in r.text
    assert "exp-y" in r.text


def test_switcher_absent_without_control_plane(
    store: InMemoryStore,
    artifacts_dir: Path,
) -> None:
    app = make_web_ui_app(
        store_factory=_one_experiment_factory(store, admin_store=store),
        experiment_id=EXPERIMENT_ID,
        experiment_config=_config(),
        worker_id=WORKER_ID,
        session_secret=SESSION_SECRET,
        claim_ttl_seconds=3600,
        artifacts_dir=artifacts_dir,
    )
    with TestClient(app) as client:
        client.post("/signin", follow_redirects=False)
        r = client.get("/")
        assert 'class="switcher"' not in r.text
        assert "experiment:" in r.text
