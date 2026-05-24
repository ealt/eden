"""Shared helpers for route handlers (session/CSRF lookup + cookie shape)."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlparse

from eden_contracts import Idea
from fastapi import Request, Response
from fastapi.responses import RedirectResponse

from ..sessions import SESSION_COOKIE_NAME, Session, SessionCodec, verify_csrf

_CONTENT_MAX_BYTES = 1 << 20  # 1 MiB


def is_htmx_request(request: Request) -> bool:
    """True iff the request was made by htmx (carries ``HX-Request: true``)."""
    return request.headers.get("hx-request", "").lower() == "true"


def htmx_aware_redirect(request: Request, url: str) -> Response:
    """Redirect appropriately for both htmx and no-JS clients.

    HTMX does not process 3xx responses — it follows the redirect
    transparently and swaps the redirected target's HTML into the
    configured target. For an ``add_row`` button targeted at
    ``#idea-rows`` that produces a full ``<html>`` document
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


def _read_inline_artifact(
    uri: str | None, artifacts_dir: Path
) -> str | None:
    """Return the artifact text iff ``uri`` resolves inside ``artifacts_dir``.

    Trust-boundary helper used by both the idea content
    rendering (chunk 9c §A.1) and the variant-side artifact rendering
    (chunk 9d §A.1):

    - Only ``file://`` URIs are eligible. Any other scheme returns
      ``None`` so the template renders the URI as a plain link.
    - The resolved path MUST be contained within
      ``artifacts_dir.resolve()``. ``..``-traversal and absolute
      escapes are rejected via ``Path.is_relative_to``.
    - Non-file inodes (directories, sockets) return ``None``.
    - Files larger than 1 MiB return ``None``.
    """
    if uri is None:
        return None
    parsed = urlparse(uri)
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
    if size > _CONTENT_MAX_BYTES:
        return None
    try:
        return resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def read_idea_content(
    idea: Idea, artifacts_dir: Path
) -> str | None:
    """Return the content text iff the artifact is safely confined."""
    return _read_inline_artifact(idea.artifacts_uri, artifacts_dir)


def read_variant_artifact(
    artifacts_uri: str | None, artifacts_dir: Path
) -> str | None:
    """Return the variant's inline artifact text iff safely confined.

    Sibling to :func:`read_idea_content` for the
    chunk-9d evaluator draft view; ``variant.artifacts_uri`` is
    optional and may be ``None``, which short-circuits to ``None``.
    """
    return _read_inline_artifact(artifacts_uri, artifacts_dir)


def validate_file_artifact_uri(
    artifacts_uri: str | None, artifacts_dir: Path
) -> str | None:
    """Validate that an operator-supplied ``file://`` URI is readable.

    Returns ``None`` if the URI is acceptable, or an operator-facing
    error string. Acceptance rules:

    - ``None`` / empty → accepted (the field is optional).
    - Non-``file://`` schemes (``http``, ``https``, etc.) → accepted
      without further check; remote schemes can't be probed server-side.
    - ``file://`` URIs MUST resolve to an existing regular file under
      ``artifacts_dir.resolve()`` (the substrate's artifact jail).

    Issue #167: previously the form silently accepted any string; an
    operator typing ``file:///eval.md`` (missing the artifacts-dir
    prefix) escaped the jail and the resulting submission later 404'd
    on read, with the URI locked in by the first-write-wins resubmit
    rule.
    """
    if artifacts_uri is None:
        return None
    if not artifacts_uri:
        return None
    parsed = urlparse(artifacts_uri)
    if parsed.scheme != "file":
        return None
    raw_path = unquote(parsed.path)
    if not raw_path:
        return "artifacts_uri path is empty"
    base = artifacts_dir.resolve()
    try:
        resolved = Path(raw_path).resolve()
    except OSError as exc:
        return f"artifacts_uri could not be resolved: {exc.strerror or exc}"
    if not resolved.is_relative_to(base):
        return (
            f"artifacts_uri must point to a file under {base} "
            f"(got {resolved})"
        )
    if not resolved.exists():
        return f"artifacts_uri does not exist at {resolved}"
    if not resolved.is_file():
        return f"artifacts_uri is not a regular file: {resolved}"
    try:
        with resolved.open("rb") as fh:
            fh.read(1)
    except OSError as exc:
        return f"artifacts_uri is not readable: {exc.strerror or exc}"
    return None
