"""Artifact-serving route.

Browsers refuse to navigate to ``file://`` URIs from an ``http://``
page (the security model that produces ``about:blank#blocked`` on
right-click → open). The reference implementation stores artifact
files (idea rationales, evaluator notes) under the configured
``--artifacts-dir`` and references them via ``file://`` URIs in the
data model. To make those clickable from the UI, we expose a thin
HTTP read endpoint that serves any file confined to the artifacts
dir.

Trust boundary mirrors :func:`eden_web_ui.routes._helpers._read_inline_artifact`:

- Only ``file://`` URIs are eligible.
- The resolved path MUST be contained within ``artifacts_dir.resolve()``.
- Non-file inodes return 404.
- Authenticated callers only (302 redirect to /signin otherwise).

Content-type is best-effort via ``mimetypes.guess_type``; falls back
to ``text/plain; charset=utf-8`` so unknown extensions render in the
browser rather than triggering a download dialog.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from urllib.parse import unquote, urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse

from ._helpers import get_session

router = APIRouter()


@router.get("/artifacts", response_model=None)
async def serve_artifact(
    request: Request,
    uri: str = "",
) -> FileResponse | RedirectResponse:
    """Serve an artifact file referenced by a ``file://`` URI."""
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)

    if not uri:
        raise HTTPException(status_code=400, detail="uri query param required")

    parsed = urlparse(uri)
    if parsed.scheme != "file":
        raise HTTPException(
            status_code=400,
            detail="only file:// URIs are served by this endpoint",
        )

    raw_path = unquote(parsed.path)
    if not raw_path:
        raise HTTPException(status_code=400, detail="empty path in URI")

    candidate = Path(raw_path)
    try:
        resolved = candidate.resolve()
    except OSError as exc:  # pragma: no cover — defensive
        raise HTTPException(
            status_code=404, detail="artifact not found"
        ) from exc

    base = Path(request.app.state.artifacts_dir).resolve()
    if not resolved.is_relative_to(base):
        # Treat path-traversal / out-of-jail attempts as 404 (rather
        # than 403) so we don't leak information about the artifacts
        # directory layout.
        raise HTTPException(status_code=404, detail="artifact not found")

    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="artifact not found")

    content_type, _ = mimetypes.guess_type(str(resolved))
    if content_type is None:
        content_type = "text/plain; charset=utf-8"
    elif content_type.startswith("text/") and "charset" not in content_type:
        content_type = f"{content_type}; charset=utf-8"

    return FileResponse(str(resolved), media_type=content_type)
