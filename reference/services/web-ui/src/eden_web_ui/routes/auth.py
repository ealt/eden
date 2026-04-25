"""Sign-in / sign-out routes.

The Phase-9-chunk-1 sign-in is a no-questions form: any GET to
``/signin`` shows a button labeled "Continue as <worker-id>", a
POST issues a fresh session cookie, and ``/signout`` clears it.
Real per-user auth lives in Milestone 3.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..sessions import SESSION_COOKIE_NAME, Session, new_csrf_token
from ._helpers import write_session_cookie

router = APIRouter()


@router.get("/signin", response_class=HTMLResponse)
async def signin_form(request: Request) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request,
        "signin.html",
        {"worker_id": request.app.state.worker_id},
    )


@router.post("/signin", response_model=None)
async def signin_submit(request: Request) -> RedirectResponse:
    session = Session(
        worker_id=request.app.state.worker_id,
        csrf=new_csrf_token(),
    )
    encoded = request.app.state.session_codec.encode(session)
    response = RedirectResponse(url="/", status_code=303)
    write_session_cookie(
        response,
        encoded=encoded,
        secure=request.app.state.secure_cookies,
    )
    return response


@router.post("/signout", response_model=None)
async def signout(request: Request) -> RedirectResponse:
    response = RedirectResponse(url="/signin", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response
