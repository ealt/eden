"""Tests for the /admin/* admins-group gate middleware (issue #144).

The middleware enforces three outcomes on every ``/admin/*`` path:

- Missing / invalid session → 303 redirect to ``/signin``.
- Session worker not in ``admins`` group → 403 with a "Forbidden"
  HTML page (not a JSON body — admin pages are HTML).
- Session worker in ``admins`` → request proceeds to the handler.

The gate covers every admin sub-router: the main ``admin`` package
(observability + actions + work-refs), ``admin_workers``,
``admin_groups``, ``admin_artifacts``, and ``admin_experiments``.

A representative GET per sub-router is exercised below; the
middleware lives in one place (``eden_web_ui.middleware``) so
per-route exhaustiveness is not needed once each sub-router's
prefix is shown to be covered.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import (
    EXPERIMENT_ID,
    SESSION_SECRET,
    _config,
    _now,
    _one_experiment_factory,
    web_ui_worker_id,
)
from eden_storage import InMemoryStore
from eden_web_ui import make_app
from fastapi import FastAPI
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------
# Fixtures for the non-admin posture
# ---------------------------------------------------------------------


@pytest.fixture
def store_non_admin(artifacts_dir: Path) -> InMemoryStore:
    """A store where the session worker is registered but NOT in admins.

    The default ``store`` fixture in ``conftest.py`` adds every
    registered test worker to ``admins`` so existing tests pass the
    gate. Tests for the gate-rejection path need the opposite: an
    ``admins`` group that does NOT contain the session worker (#128).
    """
    cfg = _config()
    s = InMemoryStore(
        experiment_id=EXPERIMENT_ID,
        evaluation_schema=cfg.evaluation_schema,
    )
    session_worker, _ = s.register_worker("ui-w")
    # Register admins with a different worker so the group exists
    # (mirrors a real deployment) but the signed-in user isn't in it.
    admin_worker, _ = s.register_worker("some-admin-worker")
    s.register_group(
        "admins", members=[admin_worker.worker_id], allow_reserved=True
    )
    s._session_worker_id = session_worker.worker_id  # type: ignore[attr-defined]
    return s


@pytest.fixture
def app_non_admin(
    store_non_admin: InMemoryStore, artifacts_dir: Path
) -> FastAPI:
    return make_app(
        store_factory=_one_experiment_factory(store_non_admin, admin_store=store_non_admin),
        experiment_id=EXPERIMENT_ID,
        experiment_config=_config(),
        worker_id=store_non_admin._session_worker_id,  # type: ignore[attr-defined]
        session_secret=SESSION_SECRET,
        claim_ttl_seconds=3600,
        artifacts_dir=artifacts_dir,
        secure_cookies=False,
        now=_now,
    )


@pytest.fixture
def signed_in_non_admin(app_non_admin: FastAPI) -> Iterator[TestClient]:
    with TestClient(app_non_admin) as c:
        resp = c.post("/signin", follow_redirects=False)
        assert resp.status_code == 303
        yield c


# ---------------------------------------------------------------------
# Routes exercised across sub-routers
# ---------------------------------------------------------------------

# Each entry: (description, path). Every admin sub-router is
# represented so the gate's path-prefix coverage is asserted
# directly, not by transitive trust.
_ADMIN_GET_ROUTES: tuple[tuple[str, str], ...] = (
    ("admin-index", "/admin/"),
    ("admin-tasks", "/admin/tasks/"),
    ("admin-variants", "/admin/variants/"),
    ("admin-events", "/admin/events/"),
    ("admin-ideas", "/admin/ideas/"),
    ("admin-work-refs", "/admin/work-refs/"),
    ("admin-workers", "/admin/workers/"),
    ("admin-groups", "/admin/groups/"),
    ("admin-artifacts", "/admin/artifacts/"),
)


# ---------------------------------------------------------------------
# Tests — unauthenticated → 303 /signin
# ---------------------------------------------------------------------


class TestUnauthenticatedRedirects:
    """No session cookie → middleware returns 303 to /signin on every admin path."""

    @pytest.mark.parametrize(("name", "path"), _ADMIN_GET_ROUTES)
    def test_get_unauthenticated_redirects_signin(
        self, client: TestClient, name: str, path: str
    ) -> None:
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 303, name
        assert resp.headers["location"] == "/signin", name


# ---------------------------------------------------------------------
# Tests — signed-in non-admin → 403
# ---------------------------------------------------------------------


class TestNonAdminForbidden:
    """Signed-in but not in admins → middleware returns 403 HTML, not 200."""

    @pytest.mark.parametrize(("name", "path"), _ADMIN_GET_ROUTES)
    def test_get_signed_in_non_admin_forbidden(
        self, signed_in_non_admin: TestClient, name: str, path: str
    ) -> None:
        resp = signed_in_non_admin.get(path, follow_redirects=False)
        assert resp.status_code == 403, name
        assert "admins" in resp.text.lower(), name
        # The body is HTML (the _error.html template), not JSON.
        assert "<html" in resp.text.lower(), name

    def test_post_signed_in_non_admin_forbidden(
        self, signed_in_non_admin: TestClient
    ) -> None:
        """Mutating POSTs on /admin/* also fail the gate before CSRF."""
        resp = signed_in_non_admin.post(
            "/admin/tasks/some-task/reclaim",
            data={"csrf_token": "x"},
            follow_redirects=False,
        )
        assert resp.status_code == 403
        # The middleware reaches its decision before the route's CSRF
        # check, so the body is the gate's forbidden page, not the
        # CSRF-failure response.
        assert "admins" in resp.text.lower()


# ---------------------------------------------------------------------
# Tests — signed-in admin → 200 (positive control)
# ---------------------------------------------------------------------


class TestAdminAllowed:
    """Signed-in worker in admins → request proceeds to the handler."""

    @pytest.mark.parametrize(("name", "path"), _ADMIN_GET_ROUTES)
    def test_get_signed_in_admin_200(
        self, signed_in_client: TestClient, name: str, path: str
    ) -> None:
        resp = signed_in_client.get(path, follow_redirects=False)
        assert resp.status_code == 200, name


# ---------------------------------------------------------------------
# Tests — non-admin paths are unaffected
# ---------------------------------------------------------------------


class TestNonAdminPathsUnaffected:
    """The gate must not interfere with non-/admin paths."""

    def test_signin_get_loads_for_unauthenticated(
        self, client: TestClient
    ) -> None:
        resp = client.get("/signin")
        assert resp.status_code == 200

    def test_root_loads_for_non_admin_session(
        self, signed_in_non_admin: TestClient
    ) -> None:
        resp = signed_in_non_admin.get("/")
        assert resp.status_code == 200

    def test_healthz_loads_unauthenticated(self, client: TestClient) -> None:
        resp = client.get("/healthz")
        assert resp.status_code == 200


# ---------------------------------------------------------------------
# Tests — transport / store failure during membership check
# ---------------------------------------------------------------------


class TestMembershipCheckFailure:
    """A raise from ``resolve_worker_in_group`` surfaces as a 502 error page."""

    def test_resolve_raises_returns_502_error_page(
        self, store: InMemoryStore, artifacts_dir: Path
    ) -> None:
        def _boom(_worker_id: str, _group_id: str) -> bool:
            raise RuntimeError("simulated transport failure")

        store.resolve_worker_in_group = _boom  # type: ignore[method-assign]
        app = make_app(
            store_factory=_one_experiment_factory(store, admin_store=store),
            experiment_id=EXPERIMENT_ID,
            experiment_config=_config(),
            worker_id=web_ui_worker_id(store),
            session_secret=SESSION_SECRET,
            claim_ttl_seconds=3600,
            artifacts_dir=artifacts_dir,
            secure_cookies=False,
            now=_now,
        )
        with TestClient(app) as c:
            resp = c.post("/signin", follow_redirects=False)
            assert resp.status_code == 303
            resp = c.get("/admin/", follow_redirects=False)
            assert resp.status_code == 502
            assert "transport" in resp.text.lower()
