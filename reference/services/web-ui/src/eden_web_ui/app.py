"""FastAPI application factory for the reference Web UI service.

``make_app`` constructs the ASGI app from the configured store +
experiment context. The CLI layer (``cli.py``) builds the deps and
hands them in; tests construct the app directly with an in-memory
store.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from eden_contracts import ExperimentConfig
from eden_storage import Store
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .routes import auth as auth_routes
from .routes import index as index_routes
from .routes import planner as planner_routes
from .sessions import SessionCodec

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"


def _now_factory() -> Callable[[], datetime]:
    def _now() -> datetime:
        return datetime.now(UTC)

    return _now


def make_app(
    *,
    store: Store,
    experiment_id: str,
    experiment_config: ExperimentConfig,
    worker_id: str,
    session_secret: str,
    claim_ttl_seconds: int,
    artifacts_dir: Path,
    secure_cookies: bool = False,
    now: Callable[[], datetime] | None = None,
) -> FastAPI:
    """Construct the FastAPI app.

    ``now`` is injected so tests can pin time deterministically; the
    CLI passes a real wall-clock factory.
    """
    app = FastAPI(title="EDEN reference Web UI", version="0.0.1")
    app.mount(
        "/static",
        StaticFiles(directory=str(_STATIC_DIR)),
        name="static",
    )
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    templates.env.globals["experiment_id"] = experiment_id

    app.state.store = store
    app.state.experiment_id = experiment_id
    app.state.experiment_config = experiment_config
    app.state.worker_id = worker_id
    app.state.session_codec = SessionCodec(session_secret)
    app.state.claim_ttl_seconds = claim_ttl_seconds
    app.state.artifacts_dir = Path(artifacts_dir)
    app.state.secure_cookies = secure_cookies
    app.state.now = now or _now_factory()
    app.state.templates = templates

    app.include_router(index_routes.router)
    app.include_router(auth_routes.router)
    app.include_router(planner_routes.router)

    @app.exception_handler(404)
    async def _not_found(request: Request, _exc: object) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "_error.html",
            {"title": "Not found", "message": "The page you requested does not exist."},
            status_code=404,
        )

    return app
