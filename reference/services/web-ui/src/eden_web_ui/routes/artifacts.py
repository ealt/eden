"""Artifact-serving route.

Browsers refuse to navigate to ``file://`` URIs from an ``http://``
page (the security model that produces ``about:blank#blocked`` on
right-click → open). The reference implementation stores artifact
files (idea contents, evaluator notes, bundled multi-file artifacts
per issue #120) under the configured ``--artifacts-dir`` and
references them via ``file://`` URIs in the data model. To make
those clickable from the UI, we expose a thin HTTP read endpoint
that serves any file confined to the artifacts dir.

Two transports overlap on this route:

- ``GET /artifacts?uri=<file-uri>`` — serve the file as-is.
- ``GET /artifacts?uri=<file-uri>&entry=<entry>`` — when ``uri``
  points at a ``.tar.gz`` bundle (issue #120), stream the named
  entry from inside without unpacking it on disk.

Trust boundary mirrors :func:`eden_web_ui.routes._helpers._read_inline_artifact`:

- Only ``file://`` URIs are eligible.
- The resolved path MUST be contained within ``artifacts_dir.resolve()``.
- Non-file inodes return 404.
- Tarball entry names are validated to be a single safe basename
  (no slashes, no ``..``, no NUL) so a crafted ``?entry=`` can't
  reach outside the archive's logical root.
- Authenticated callers only (302 redirect to /signin otherwise).

Content-type is best-effort via ``mimetypes.guess_type``; falls back
to ``text/plain; charset=utf-8`` so unknown extensions render in the
browser rather than triggering a download dialog.
"""

from __future__ import annotations

import mimetypes
import tarfile
from pathlib import Path
from urllib.parse import unquote, urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response

from ..artifacts import MANIFEST_NAME
from ._helpers import get_session

router = APIRouter()


def _safe_entry_name(name: str) -> str | None:
    """Return ``name`` iff it is a single safe basename.

    Rejects any path component (``a/b``), traversal (``..``), NUL
    bytes, and the empty string. Returns ``None`` on rejection so
    the caller can 400.
    """
    if not name or "/" in name or "\\" in name or "\x00" in name:
        return None
    if name in (".", ".."):
        return None
    return name


def _content_type_for(path: Path) -> str:
    content_type, _ = mimetypes.guess_type(str(path))
    if content_type is None:
        return "text/plain; charset=utf-8"
    if content_type.startswith("text/") and "charset" not in content_type:
        return f"{content_type}; charset=utf-8"
    return content_type


def _serve_bundle_entry(
    bundle_path: Path, entry_name: str
) -> Response:
    """Stream one entry's bytes out of ``bundle_path``.

    Returns 404 on any extraction error (missing entry,
    non-regular-file member, tar corruption). Best-effort MIME
    detection from the entry's filename.
    """
    safe = _safe_entry_name(entry_name)
    if safe is None:
        raise HTTPException(
            status_code=400, detail="invalid entry name"
        )
    if safe == MANIFEST_NAME:
        # The manifest is metadata, not user-uploaded content; serve
        # it as application/json so operators can inspect it via the
        # same route shape rather than needing a separate endpoint.
        content_type = "application/json; charset=utf-8"
    else:
        content_type = _content_type_for(Path(safe))
    try:
        with tarfile.open(bundle_path, mode="r:gz") as tar:
            try:
                member = tar.getmember(safe)
            except KeyError as exc:
                raise HTTPException(
                    status_code=404, detail="entry not found"
                ) from exc
            if not member.isfile():
                raise HTTPException(
                    status_code=404, detail="entry not found"
                )
            handle = tar.extractfile(member)
            if handle is None:
                raise HTTPException(
                    status_code=404, detail="entry not found"
                )
            data = handle.read()
    except tarfile.TarError as exc:
        raise HTTPException(
            status_code=404, detail="bundle unreadable"
        ) from exc
    return Response(content=data, media_type=content_type)


@router.get("/artifacts", response_model=None)
async def serve_artifact(
    request: Request,
    uri: str = "",
    entry: str = "",
) -> FileResponse | RedirectResponse | Response:
    """Serve an artifact file referenced by a ``file://`` URI.

    When ``entry`` is non-empty AND the resolved file is a
    ``.tar.gz`` bundle, the route streams the named entry out of
    the archive instead of returning the archive itself.
    """
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

    if entry:
        if not resolved.name.endswith(".tar.gz"):
            raise HTTPException(
                status_code=400,
                detail="entry param only valid for .tar.gz bundles",
            )
        return _serve_bundle_entry(resolved, entry)

    return FileResponse(str(resolved), media_type=_content_type_for(resolved))
