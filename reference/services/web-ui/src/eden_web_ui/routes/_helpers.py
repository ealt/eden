"""Shared helpers for route handlers (session/CSRF lookup + cookie shape)."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlparse

from eden_contracts import Idea
from fastapi import Request, Response
from fastapi.responses import RedirectResponse

from ..artifacts import (
    is_bundle_uri,
    read_bundle_entry,
    read_bundle_manifest,
)
from ..sessions import SESSION_COOKIE_NAME, Session, SessionCodec, verify_csrf

_CONTENT_MAX_BYTES = 1 << 20  # 1 MiB

# Bundle entry name that the viewer extracts as the inline "headline"
# when an idea-side bundle is rendered. Mirrors the convention written
# by :func:`eden_web_ui.artifacts.write_artifact_bundle` for ideator
# submissions.
IDEA_BUNDLE_HEADLINE = "idea.md"
EVALUATION_BUNDLE_HEADLINE = "evaluation.md"
VARIANT_BUNDLE_HEADLINE = "variant.md"


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


def _resolve_inside_jail(
    uri: str | None, artifacts_dir: Path
) -> Path | None:
    """Trust-boundary check: resolve ``uri`` if confined to ``artifacts_dir``.

    Returns the resolved path on success; ``None`` if the URI is not
    a ``file://`` URI, points outside the jail, or doesn't resolve
    to a regular file. Used by both the inline-artifact reader and
    the bundle manifest reader so they share one path-confinement
    check.
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
    return resolved


def _read_inline_artifact(
    uri: str | None,
    artifacts_dir: Path,
    *,
    bundle_headline: str | None = None,
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

    Issue #120: when the resolved file is a ``.tar.gz`` bundle and
    ``bundle_headline`` names an entry inside it (e.g. ``idea.md``),
    that entry's text is returned instead — so the operator's
    bundled markdown still renders inline as the headline of the
    submission. Returns ``None`` for bundles without that headline
    entry; the manifest table is rendered separately by the
    template.
    """
    resolved = _resolve_inside_jail(uri, artifacts_dir)
    if resolved is None:
        return None
    try:
        size = resolved.stat().st_size
    except OSError:
        return None
    if is_bundle_uri(uri):
        if bundle_headline is None:
            return None
        data = read_bundle_entry(
            resolved, bundle_headline, max_bytes=_CONTENT_MAX_BYTES
        )
        if data is None:
            return None
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if size > _CONTENT_MAX_BYTES:
        return None
    try:
        return resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _read_artifact_manifest(
    uri: str | None, artifacts_dir: Path
) -> dict | None:
    """Read the bundle manifest for ``uri`` iff it's an in-jail ``.tar.gz``."""
    if not is_bundle_uri(uri):
        return None
    resolved = _resolve_inside_jail(uri, artifacts_dir)
    if resolved is None:
        return None
    return read_bundle_manifest(resolved)


def read_idea_content(
    idea: Idea, artifacts_dir: Path
) -> str | None:
    """Return the content text iff the artifact is safely confined.

    For ``.tar.gz`` bundles, returns the ``idea.md`` headline entry
    if present; otherwise ``None`` (the manifest table still
    renders, supplied by :func:`read_idea_manifest`).
    """
    return _read_inline_artifact(
        idea.artifacts_uri,
        artifacts_dir,
        bundle_headline=IDEA_BUNDLE_HEADLINE,
    )


def read_idea_manifest(
    idea: Idea, artifacts_dir: Path
) -> dict | None:
    """Return the manifest dict iff the idea's artifact is a bundle."""
    return _read_artifact_manifest(idea.artifacts_uri, artifacts_dir)


def read_variant_artifact(
    artifacts_uri: str | None, artifacts_dir: Path
) -> str | None:
    """Return the variant's inline artifact text iff safely confined.

    Sibling to :func:`read_idea_content` for the
    chunk-9d evaluator draft view; ``variant.artifacts_uri`` is
    optional and may be ``None``, which short-circuits to ``None``.
    """
    return _read_inline_artifact(
        artifacts_uri,
        artifacts_dir,
        bundle_headline=VARIANT_BUNDLE_HEADLINE,
    )


def read_variant_artifact_manifest(
    artifacts_uri: str | None, artifacts_dir: Path
) -> dict | None:
    """Return the manifest dict iff the variant's artifact is a bundle."""
    return _read_artifact_manifest(artifacts_uri, artifacts_dir)


def read_evaluation_artifact(
    artifacts_uri: str | None, artifacts_dir: Path
) -> str | None:
    """Return the evaluator's inline artifact text iff safely confined."""
    return _read_inline_artifact(
        artifacts_uri,
        artifacts_dir,
        bundle_headline=EVALUATION_BUNDLE_HEADLINE,
    )


def read_evaluation_artifact_manifest(
    artifacts_uri: str | None, artifacts_dir: Path
) -> dict | None:
    """Return the manifest dict iff the evaluator artifact is a bundle."""
    return _read_artifact_manifest(artifacts_uri, artifacts_dir)
