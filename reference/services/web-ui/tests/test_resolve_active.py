"""Unit tests for active-experiment resolution (issue #145 §3.1)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from conftest import EXPERIMENT_ID, SESSION_SECRET, _config
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
from eden_wire import Unauthorized
from fastapi import FastAPI
from starlette.requests import Request

OTHER_ID = "exp-other"
# A session-principal worker id for the app under test; resolution tests
# call the resolver helpers directly (not via the admin gate), so this is
# just an opaque-shaped placeholder (#128).
WORKER_ID = "wkr_0123456789abcdefghjkmnpqrs"


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

    def list_experiments(self) -> list[Any]:
        if self._unreachable:
            raise httpx.ConnectError("control plane down")
        return [SimpleNamespace(experiment_id=e) for e in sorted(self._known)]

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
    *,
    control_plane: Any,
    store_factory: Any | None = None,
    tmp_path: Any,
    config_dir: Any | None = None,
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
        experiment_config_dir=config_dir,
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


def _config_dir_with(tmp_path: Any, experiment_id: str) -> Any:
    """A per-experiment config dir holding ``<experiment_id>.yaml``."""
    from conftest import _FIXTURE_CONFIG

    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir(exist_ok=True)
    (cfg_dir / f"{experiment_id}.yaml").write_text(_FIXTURE_CONFIG.read_text())
    return cfg_dir


def test_context_happy_path_returns_context(tmp_path: Any) -> None:
    factory = _FakeFactory(seeded=True)
    app = _build_app(
        control_plane=_FakeControlPlane(),
        store_factory=factory,
        tmp_path=tmp_path,
        config_dir=_config_dir_with(tmp_path, OTHER_ID),
    )
    result = resolve_active_context(_request(app, selected=OTHER_ID), need_config=True)
    from eden_web_ui.routes._helpers import ActiveContext

    assert isinstance(result, ActiveContext)
    assert result.experiment_id == OTHER_ID
    assert result.store is factory.for_experiment(OTHER_ID)
    assert result.config is not None


def test_context_non_default_without_config_dir_redirects(tmp_path: Any) -> None:
    """Non-default experiment + no config dir → config-missing redirect, not
    a silent fall back to the default config (codex round-0 Bug 1)."""
    from fastapi.responses import RedirectResponse

    factory = _FakeFactory(seeded=True)
    app = _build_app(
        control_plane=_FakeControlPlane(), store_factory=factory, tmp_path=tmp_path
    )
    result = resolve_active_context(_request(app, selected=OTHER_ID), need_config=True)
    assert isinstance(result, RedirectResponse)
    assert "config-missing" in result.headers["location"]


class _UnauthStore:
    def read_experiment_state(self) -> str:
        raise Unauthorized("no credential for this experiment")


class _UnauthFactory:
    """Factory whose worker store always 401s; records evict() calls."""

    def __init__(self) -> None:
        self.evicted: list[str] = []
        self.admin_enabled = True

    def for_experiment(self, experiment_id: str, *, role: str = "worker") -> Any:
        return _UnauthStore()

    def evict(self, experiment_id: str) -> None:
        self.evicted.append(experiment_id)

    def close(self) -> None:  # pragma: no cover - lifespan shutdown
        pass


def test_resolve_401_evicts_then_raises_missing_admin(tmp_path: Any) -> None:
    """Posture C/D: a 401 on the seed probe evicts the cached credential and
    re-bootstraps once; a persistent 401 → MissingAdminToken (the
    cannot-bootstrap-credential branch), NOT unseeded (codex round-0 Bug 2 /
    Risk 4)."""
    from eden_web_ui.store_factory import MissingAdminToken

    factory = _UnauthFactory()
    app = _build_app(
        control_plane=_FakeControlPlane(), store_factory=factory, tmp_path=tmp_path
    )
    with pytest.raises(MissingAdminToken):
        resolve_active_experiment(_request(app, selected=OTHER_ID))
    assert factory.evicted == [OTHER_ID]  # evicted once before the retry


def test_switcher_hidden_when_control_plane_unreadable(tmp_path: Any) -> None:
    """Posture D / control-plane outage with a cold cache → switcher hidden
    (None), not an empty dropdown (codex round-0 Risk 5)."""
    from eden_web_ui.routes._helpers import switcher_context

    app = _build_app(
        control_plane=_FakeControlPlane(unreachable=True), tmp_path=tmp_path
    )
    # A session must be present (signed in); the switcher's visibility is
    # independent of which experiment is selected.
    ctx = switcher_context(_request(app, selected=EXPERIMENT_ID))
    assert ctx["switcher_experiments"] is None


# ---------------------------------------------------------------------------
# form_experiment_guard (issue #145 §3.6)
# ---------------------------------------------------------------------------


def test_form_guard_matching_returns_none() -> None:
    from eden_web_ui.routes._helpers import form_experiment_guard

    assert form_experiment_guard({"form_experiment_id": "exp-1"}, "exp-1") is None


def test_form_guard_absent_field_returns_none() -> None:
    from eden_web_ui.routes._helpers import form_experiment_guard

    assert form_experiment_guard({}, "exp-1") is None


def test_form_guard_mismatch_redirects_with_from_and_to() -> None:
    from eden_web_ui.routes._helpers import form_experiment_guard
    from fastapi.responses import RedirectResponse

    result = form_experiment_guard({"form_experiment_id": "exp-x"}, "exp-y")
    assert isinstance(result, RedirectResponse)
    loc = result.headers["location"]
    assert "switched-mid-form" in loc
    assert "from=exp-x" in loc
    assert "to=exp-y" in loc


# ---------------------------------------------------------------------------
# Store follows the active selection (the core #145 behavior)
# ---------------------------------------------------------------------------


class _MultiFakeFactory:
    def __init__(self, stores: dict[str, _FakeStore]) -> None:
        self._stores = stores
        self.admin_enabled = True

    def for_experiment(self, experiment_id: str, *, role: str = "worker") -> Any:
        return self._stores[experiment_id]

    def close(self) -> None:  # pragma: no cover - lifespan shutdown
        pass


def test_context_store_follows_selection(tmp_path: Any) -> None:
    from eden_web_ui.routes._helpers import ActiveContext

    default_store, other_store = _FakeStore(), _FakeStore()
    factory = _MultiFakeFactory({EXPERIMENT_ID: default_store, OTHER_ID: other_store})
    app = _build_app(
        control_plane=_FakeControlPlane(), store_factory=factory, tmp_path=tmp_path
    )
    result = resolve_active_context(_request(app, selected=OTHER_ID))
    assert isinstance(result, ActiveContext)
    assert result.experiment_id == OTHER_ID
    assert result.store is other_store
    # No selection → the deployment default's store.
    result_default = resolve_active_context(_request(app, selected=None))
    assert isinstance(result_default, ActiveContext)
    assert result_default.store is default_store
