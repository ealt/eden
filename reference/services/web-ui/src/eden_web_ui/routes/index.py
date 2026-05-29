"""Index route — landing page with task counts."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from ._helpers import get_session, resolve_active_context

router = APIRouter()


@router.get("/", response_class=HTMLResponse, response_model=None)
async def index(request: Request) -> HTMLResponse | RedirectResponse:
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    active = resolve_active_context(request)
    if isinstance(active, Response):
        return active
    store = active.store
    pending = {
        kind: len(store.list_tasks(kind=kind, state="pending"))
        for kind in ("ideation", "execution", "evaluation")
    }
    return request.app.state.templates.TemplateResponse(
        request,
        "index.html",
        {"session": session, "pending": pending},
    )
