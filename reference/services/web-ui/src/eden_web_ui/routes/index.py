"""Index route — landing page with task counts."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ._helpers import get_session

router = APIRouter()


@router.get("/", response_class=HTMLResponse, response_model=None)
async def index(request: Request) -> HTMLResponse | RedirectResponse:
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    store = request.app.state.store
    pending = {
        kind: len(store.list_tasks(kind=kind, state="pending"))
        for kind in ("plan", "implement", "evaluate")
    }
    return request.app.state.templates.TemplateResponse(
        request,
        "index.html",
        {"session": session, "pending": pending},
    )
