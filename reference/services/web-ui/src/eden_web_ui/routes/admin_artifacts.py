"""Read-only directory listing of the configured ``--artifacts-dir``.

Issue #107: the existing :mod:`eden_web_ui.routes.artifacts` route
serves an artifact file given a ``file://`` URI, but offers no way
to *discover* what artifacts exist. Operators wanting to verify
"did the executor write anything?" or "what's in this evaluation's
output?" have had to ``docker exec`` into the web-ui container or
read the host bind-mount directly. This module surfaces that
content in-UI as a peer of the other ``/admin/*`` inspection
surfaces (tasks, ideas, variants, events, workers, groups).

Trust boundary mirrors :func:`eden_web_ui.routes._helpers._read_inline_artifact`:

- Listing is rooted at ``app.state.artifacts_dir.resolve()`` and
  never escapes it. Each directory entry is ``resolve()``-ed and
  its containment re-checked, so symlinks pointing outside the jail
  are silently skipped.
- Authenticated callers only (302 redirect to ``/signin`` otherwise),
  matching the existing ``GET /artifacts`` posture.
- Read-only. Per the issue, "upload, delete, or any write operation
  ... is worker-write-only by design" — this module surfaces no
  mutation controls.

The empty listing is the *expected* state for scripted-mode
deployments (the URIs in submissions are fictional pointers; only
subprocess mode writes real bytes), so the template carries a
banner explaining that to head off "why is this blank?" friction.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ._helpers import get_session

router = APIRouter(prefix="/admin/artifacts")


@dataclass(frozen=True)
class _ArtifactEntry:
    rel_path: str
    size: int
    mtime: str
    serve_uri: str


def _format_mtime(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat(timespec="seconds")


def _walk_artifacts(base: Path) -> list[_ArtifactEntry]:
    """Recursively enumerate regular files under ``base``.

    Each candidate is ``resolve()``-ed and its containment within
    ``base`` re-checked: a symlink pointing outside the jail (or to
    a non-file inode) is silently skipped rather than surfaced. The
    artifacts dir is operator-curated (workers write into it; the UI
    only reads), so the design choice is to drop silently rather
    than surface "skipped N files for safety" — the listing is a
    convenience, not an audit log.
    """
    out: list[_ArtifactEntry] = []
    for dirpath, _dirnames, filenames in os.walk(base, followlinks=False):
        for name in filenames:
            candidate = Path(dirpath) / name
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if not resolved.is_relative_to(base):
                continue
            if not resolved.is_file():
                continue
            try:
                stat = resolved.stat()
            except OSError:
                continue
            rel = resolved.relative_to(base).as_posix()
            out.append(
                _ArtifactEntry(
                    rel_path=rel,
                    size=stat.st_size,
                    mtime=_format_mtime(stat.st_mtime),
                    serve_uri=f"file://{resolved}",
                )
            )
    out.sort(key=lambda e: e.rel_path)
    return out


@router.get("/", response_class=HTMLResponse, response_model=None)
async def artifacts_index(request: Request) -> HTMLResponse | RedirectResponse:
    """List files under the configured ``--artifacts-dir``."""
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)

    base = Path(request.app.state.artifacts_dir).resolve()
    entries = _walk_artifacts(base)

    return request.app.state.templates.TemplateResponse(
        request,
        "admin_artifacts.html",
        {
            "session": session,
            "artifacts_dir": str(base),
            "entries": entries,
        },
    )


__all__ = ["router"]
