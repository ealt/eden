"""Local-file proposal-artifact writer.

Each proposal needs an ``artifacts_uri``. For Phase 9 chunk 1 we
write the planner's rationale markdown to ``<artifacts-dir>/<id>.md``
and emit a ``file://`` URI. Phase 10 swaps in real blob storage.

Writes are atomic (write to ``<id>.md.tmp``, rename to ``<id>.md``)
so a crash mid-write doesn't leave a half-written artifact behind.
"""

from __future__ import annotations

import os
from pathlib import Path


def write_proposal_artifact(
    artifacts_dir: Path | str,
    proposal_id: str,
    markdown: str,
) -> str:
    """Write ``markdown`` to ``artifacts_dir/<proposal_id>.md`` and return a file:// URI.

    The file is written atomically via tmp-and-rename. ``artifacts_dir``
    is created (with parents) if it doesn't exist.
    """
    base = Path(artifacts_dir).resolve()
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{proposal_id}.md"
    tmp = base / f"{proposal_id}.md.tmp"
    tmp.write_text(markdown, encoding="utf-8")
    os.replace(tmp, path)
    return path.as_uri()
