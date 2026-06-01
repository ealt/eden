"""Unit tests for active-experiment resolution (issue #145 §3.1)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from conftest import EXPERIMENT_ID, SESSION_SECRET, WORKER_ID, _config
from eden_storage.errors import NotFound
from eden_web_ui import make_app
from eden_web_ui.routes._helpers import (
    ControlPlaneUnreachable,
    StaleSelection,
    resolve_active_context,
    resolve_active_experiment,
)
from eden_web_ui.sessions import SESSION_COOKIE_NAME, Session, SessionCodec
from eden_web_ui.store_factory import StaticStoreFactory
from fastapi import FastAPI
from starlette.requests import Request

OTHER_ID = "exp-other"


class _FakeControlPlane:
    """Stand-in control-plane client for resolution tests."""

    def __init__(self, *, known: set[str] | None = None, unreachable: bool = False) -> None:
        self._known = known if known is not None else {EXPERIMENT_ID, OTHER_ID}
        self._unreachable = unreachable

    def read_experiment_metadata(self, experiment_id: str) -> dict[str, str]:
        if self._unreachable:
            raise httpx.ConnectError("control plane down")
        if experiment_id not in self._known:
            raise NotFound(f"experiment {experiment_id!r} not registered")
        return {"experiment_id": experiment_id}

    def close(self) -> None:  # pragma: no cover - lifespan shutdown
        pass


class _FakeStore:
    def __init__(self, *, seeded: bool = True) -> None:
        self._seeded = seeded

    def read_experiment_state(self) -> str:
        if not self._seeded:
            raise NotFound("experiment not seeded on the task-store-server")
        return "running"


class _FakeFactory:
    def __init__(self, *, seeded: bool = True) -> None:
        self._store = _FakeStore(seeded=seeded)
        self.admin_enabled = True

    def for_experiment(self, experiment_id: str, *, role: str = "worker") -> Any:
        if role == "admin":
            return self._store
        return self._store

    def close(self) -> None:  # pragma: no cover - lifespan shutdown
        pass


def _build_app(
    *, control_plane: Any, store_factory: Any | None = None, tmp_path: Any
) -> FastAPI:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(exist_ok=True)
    default_factory = StaticStoreFactory(
        experiment_id=EXPERIMENT_ID,
        store=_FakeStore(),  # type: ignore[arg-type]
    )
    app = make_app(
        store_factory=default_factory if store_factory is None else store_factory,
        experiment_id=EXPERIMENT_ID,
        experiment_config=_config(),
        worker_id=WORKER_ID,
        session_secret=SESSION_SECRET,
        claim_ttl_seconds=3600,
        artifacts_dir=artifacts,
        secure_cookies=False,
        control_plane=control_plane,
    )
    # Override with the test doubles (make_app wires real types).
    app.state.control_plane = control_plane
    if store_factory is not None:
        app.state.store_factory = store_factory
    return app


def _request(app: FastAPI, *, selected: str | None = None) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if selected is not None:
        codec = SessionCodec(SESSION_SECRET)
        cookie = codec.encode(
            Session(worker_id="w", csrf="c", selected_experiment_id=selected)
        )
        headers.append((b"cookie", f"{SESSION_COOKIE_NAME}={cookie}".encode()))
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": headers,
        "app": app,
        "state": {},
    }
    return Request(scope)


# ---------------------------------------------------------------------------
# resolve_active_experiment
# ---------------------------------------------------------------------------


def test_no_control_plane_returns_default(tmp_path: Any) -> None:
    app = _build_app(control_plane=None, tmp_path=tmp_path)
    resolved = resolve_active_experiment(_request(app, selected=OTHER_ID))
    # control_plane is None → always the deployment default, no validation.
    assert resolved.experiment_id == EXPERIMENT_ID
    assert resolved.unseeded is False


def test_no_selection_returns_default(tmp_path: Any) -> None:
    app = _build_app(control_plane=_FakeControlPlane(), tmp_path=tmp_path)
    resolved = resolve_active_experiment(_request(app, selected=None))
    assert resolved.experiment_id == EXPERIMENT_ID


def test_selected_default_is_fast_path(tmp_path: Any) -> None:
    app = _build_app(control_plane=_FakeControlPlane(), tmp_path=tmp_path)
    resolved = resolve_active_experiment(_request(app, selected=EXPERIMENT_ID))
    assert resolved.experiment_id == EXPERIMENT_ID
    assert resolved.unseeded is False


def test_selected_seeded_experiment(tmp_path: Any) -> None:
    app = _build_app(
        control_plane=_FakeControlPlane(),
        store_factory=_FakeFactory(seeded=True),
        tmp_path=tmp_path,
    )
    resolved = resolve_active_experiment(_request(app, selected=OTHER_ID))
    assert resolved.experiment_id == OTHER_ID
    assert resolved.unseeded is False


def test_selected_unseeded_experiment(tmp_path: Any) -> None:
    app = _build_app(
        control_plane=_FakeControlPlane(),
        store_factory=_FakeFactory(seeded=False),
        tmp_path=tmp_path,
    )
    resolved = resolve_active_experiment(_request(app, selected=OTHER_ID))
    assert resolved.experiment_id == OTHER_ID
    assert resolved.unseeded is True


def test_stale_selection_raises(tmp_path: Any) -> None:
    app = _build_app(
        control_plane=_FakeControlPlane(known={EXPERIMENT_ID}), tmp_path=tmp_path
    )
    with pytest.raises(StaleSelection):
        resolve_active_experiment(_request(app, selected=OTHER_ID))


def test_control_plane_unreachable_raises(tmp_path: Any) -> None:
    app = _build_app(
        control_plane=_FakeControlPlane(unreachable=True), tmp_path=tmp_path
    )
    with pytest.raises(ControlPlaneUnreachable):
        resolve_active_experiment(_request(app, selected=OTHER_ID))


# ---------------------------------------------------------------------------
# resolve_active_context redirects
# ---------------------------------------------------------------------------


def test_context_stale_redirects_and_clears(tmp_path: Any) -> None:
    app = _build_app(
        control_plane=_FakeControlPlane(known={EXPERIMENT_ID}), tmp_path=tmp_path
    )
    from fastapi.responses import RedirectResponse

    result = resolve_active_context(_request(app, selected=OTHER_ID))
    assert isinstance(result, RedirectResponse)
    assert result.status_code == 303
    assert "stale-selection" in result.headers["location"]
    # The stale session field is cleared via a Set-Cookie on the redirect.
    assert SESSION_COOKIE_NAME in result.headers.get("set-cookie", "")


def test_context_unseeded_returns_page(tmp_path: Any) -> None:
    app = _build_app(
        control_plane=_FakeControlPlane(),
        store_factory=_FakeFactory(seeded=False),
        tmp_path=tmp_path,
    )
    result = resolve_active_context(_request(app, selected=OTHER_ID))
    from starlette.responses import Response as StarletteResponse

    assert isinstance(result, StarletteResponse)
    assert result.status_code == 409


def test_context_happy_path_returns_context(tmp_path: Any) -> None:
    factory = _FakeFactory(seeded=True)
    app = _build_app(
        control_plane=_FakeControlPlane(), store_factory=factory, tmp_path=tmp_path
    )
    result = resolve_active_context(_request(app, selected=OTHER_ID), need_config=True)
    from eden_web_ui.routes._helpers import ActiveContext

    assert isinstance(result, ActiveContext)
    assert result.experiment_id == OTHER_ID
    assert result.store is factory.for_experiment(OTHER_ID)
    assert result.config is not None
