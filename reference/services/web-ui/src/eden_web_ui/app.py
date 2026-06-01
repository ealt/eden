"""FastAPI application factory for the reference Web UI service.

``make_app`` constructs the ASGI app from the configured store +
experiment context. The CLI layer (``cli.py``) builds the deps and
hands them in; tests construct the app directly with an in-memory
store.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eden_contracts import ExperimentConfig
from eden_control_plane import ControlPlaneClient
from eden_git import GitRepo
from eden_storage.errors import NotFound as StorageNotFound
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .middleware import AdminGateMiddleware
from .routes import admin as admin_routes
from .routes import admin_artifacts as admin_artifacts_routes
from .routes import admin_experiments as admin_experiments_routes
from .routes import admin_groups as admin_groups_routes
from .routes import admin_workers as admin_workers_routes
from .routes import artifacts as artifacts_routes
from .routes import auth as auth_routes
from .routes import evaluator as evaluator_routes
from .routes import executor as executor_routes
from .routes import ideator as ideator_routes
from .routes import index as index_routes
from .routes.admin import control as admin_control_routes
from .sessions import SessionCodec
from .store_factory import StaticStoreFactory, StoreFactory

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"


def _now_factory() -> Callable[[], datetime]:
    def _now() -> datetime:
        return datetime.now(UTC)

    return _now


def _experiment_context(request: Request) -> dict[str, Any]:
    """Template context processor: per-request active ``experiment_id``.

    Issue #145 moves ``experiment_id`` from a render-time Jinja global
    (one value for the process lifetime) to a per-request value, so
    every template reflects the experiment the operator selected. The
    active id is stashed on ``request.state.active_experiment_id`` by
    ``resolve_active_context``; absent that (unauthenticated pages,
    handlers that don't resolve), it falls back to the deployment
    default.
    """
    active = getattr(request.state, "active_experiment_id", None)
    default = request.app.state.experiment_id
    return {
        "experiment_id": active or default,
        "active_experiment_id": active,
        "default_experiment_id": default,
    }


def _register_routers(
    app: FastAPI,
    *,
    control_plane: ControlPlaneClient | None,
    repo: GitRepo | None,
) -> None:
    app.include_router(index_routes.router)
    app.include_router(auth_routes.router)
    app.include_router(ideator_routes.router)
    app.include_router(evaluator_routes.router)
    app.include_router(admin_routes.router)
    app.include_router(admin_workers_routes.router)
    app.include_router(admin_groups_routes.router)
    app.include_router(admin_artifacts_routes.router)
    app.include_router(artifacts_routes.router)
    if control_plane is not None:
        app.include_router(admin_experiments_routes.router)
        app.include_router(admin_control_routes.router)
    if repo is not None:
        app.include_router(executor_routes.router)


def make_app(
    *,
    store_factory: StoreFactory | StaticStoreFactory,
    experiment_id: str,
    experiment_config: ExperimentConfig,
    worker_id: str,
    session_secret: str,
    claim_ttl_seconds: int,
    artifacts_dir: Path,
    secure_cookies: bool = False,
    now: Callable[[], datetime] | None = None,
    repo: GitRepo | None = None,
    clone_url: str | None = None,
    base_commit_sha: str | None = None,
    control_plane: ControlPlaneClient | None = None,
    experiment_config_dir: Path | None = None,
) -> FastAPI:
    """Construct the FastAPI app.

    ``now`` is injected so tests can pin time deterministically.
    ``repo`` gates the executor module (None → routes unregistered).

    Issue #145: per-experiment routing goes through ``store_factory``.
    The CLI builds a live :class:`StoreFactory`; tests build a
    :class:`StaticStoreFactory` (one pre-built store for the single
    deployment experiment). When the factory's admin view is ``None``
    (no admin token configured), mutation controls render disabled and
    admin POSTs 303 to ``?error=admin-disabled`` (plan §D.3 four-posture
    matrix). ``experiment_config`` is the deployment-default config (and
    the single-experiment config source); ``experiment_config_dir``
    enables per-experiment config loading in control-plane mode (plan
    Decision 6).
    """

    @asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            store_factory.close()

    app = FastAPI(
        title="EDEN reference Web UI", version="0.0.1", lifespan=_lifespan
    )
    app.mount(
        "/static",
        StaticFiles(directory=str(_STATIC_DIR)),
        name="static",
    )
    templates = Jinja2Templates(
        directory=str(_TEMPLATES_DIR),
        context_processors=[_experiment_context],
    )
    templates.env.globals["executor_enabled"] = repo is not None
    templates.env.globals["admin_enabled"] = store_factory.admin_enabled
    templates.env.globals["control_plane_enabled"] = control_plane is not None

    app.state.store_factory = store_factory
    app.state.experiment_id = experiment_id
    app.state.experiment_config = experiment_config
    app.state.experiment_config_dir = experiment_config_dir
    app.state.experiment_config_cache = {}
    app.state.worker_id = worker_id
    app.state.session_codec = SessionCodec(session_secret)
    app.state.claim_ttl_seconds = claim_ttl_seconds
    app.state.artifacts_dir = Path(artifacts_dir)
    app.state.secure_cookies = secure_cookies
    app.state.now = now or _now_factory()
    app.state.templates = templates
    app.state.repo = repo
    app.state.clone_url = clone_url
    app.state.base_commit_sha = base_commit_sha
    app.state.control_plane = control_plane

    app.add_middleware(AdminGateMiddleware)
    _register_routers(app, control_plane=control_plane, repo=repo)

    @app.get("/healthz", include_in_schema=False)
    async def _healthz() -> dict[str, str]:
        # Unauthenticated by design — Compose's healthcheck must
        # work before any user signs in. Reveals only "the process
        # is up"; carries no secrets.
        return {"status": "ok"}

    @app.exception_handler(404)
    async def _not_found(request: Request, _exc: object) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "_error.html",
            {"title": "Not found", "message": "The page you requested does not exist."},
            status_code=404,
        )

    @app.exception_handler(StorageNotFound)
    async def _storage_not_found(request: Request, exc: StorageNotFound) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "_error.html",
            {"title": "Not found", "message": str(exc)},
            status_code=404,
        )

    return app
