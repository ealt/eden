"""Shared helpers for route handlers (session/CSRF lookup + cookie shape)."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlparse

from eden_contracts import Proposal
from fastapi import Request, Response
from fastapi.responses import RedirectResponse

from ..sessions import SESSION_COOKIE_NAME, Session, SessionCodec, verify_csrf

_RATIONALE_MAX_BYTES = 1 << 20  # 1 MiB


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


def read_proposal_rationale(
    proposal: Proposal, artifacts_dir: Path
) -> str | None:
    """Return the rationale text iff the artifact is safely confined.

    Per the §A.1 trust boundary in
    ``docs/plans/eden-phase-9c-implementer-module.md``: a proposal's
    ``artifacts_uri`` may point anywhere; the UI must not render
    arbitrary local files. The rule is:

    - Only ``file://`` URIs are eligible. Any other scheme returns
      ``None`` so the template renders the URI as a plain link.
    - The resolved path MUST be contained within
      ``artifacts_dir.resolve()``. ``..``-traversal and absolute
      escapes are rejected via ``Path.is_relative_to``.
    - Files larger than 1 MiB return ``None`` (the template renders
      a "rationale too large" note + link).
    """
    parsed = urlparse(proposal.artifacts_uri)
    if parsed.scheme != "file":
        return None
    raw_path = unquote(parsed.path)
    if not raw_path:
        return None
    candidate = Path(raw_path)
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    base = artifacts_dir.resolve()
    if not resolved.is_relative_to(base):
        return None
    if not resolved.is_file():
        return None
    try:
        size = resolved.stat().st_size
    except OSError:
        return None
    if size > _RATIONALE_MAX_BYTES:
        return None
    try:
        return resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
