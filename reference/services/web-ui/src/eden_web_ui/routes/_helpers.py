"""Shared helpers for route handlers (session/CSRF lookup + cookie shape)."""

from __future__ import annotations

from fastapi import Request, Response
from fastapi.responses import RedirectResponse

from ..sessions import SESSION_COOKIE_NAME, Session, SessionCodec, verify_csrf


def is_htmx_request(request: Request) -> bool:
    """True iff the request was made by htmx (carries ``HX-Request: true``)."""
    return request.headers.get("hx-request", "").lower() == "true"


def htmx_aware_redirect(request: Request, url: str) -> Response:
    """Redirect appropriately for both htmx and no-JS clients.

    HTMX does not process 3xx responses — it follows the redirect
    transparently and swaps the redirected target's HTML into the
    configured target. For an ``add_row`` button targeted at
    ``#proposal-rows`` that produces a full ``<html>`` document
    inside the rows container. The fix is to send back ``HX-Redirect``
    on a 200/204 instead; htmx intercepts that header and does a
    full client-side navigation.
    """
    if is_htmx_request(request):
        return Response(status_code=204, headers={"hx-redirect": url})
    return RedirectResponse(url=url, status_code=303)


def get_session(request: Request) -> Session | None:
    """Return the decoded session for ``request``, or ``None`` if missing/invalid."""
    raw = request.cookies.get(SESSION_COOKIE_NAME)
    if raw is None:
        return None
    codec: SessionCodec = request.app.state.session_codec
    return codec.decode(raw)


def write_session_cookie(
    response: Response,
    *,
    encoded: str,
    secure: bool,
) -> None:
    """Set the signed session cookie on ``response`` with pinned attributes."""
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=encoded,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


def csrf_ok(session: Session, presented: str | None) -> bool:
    """Constant-time CSRF token check, exposed for routes."""
    return verify_csrf(session, presented)
