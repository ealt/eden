"""Artifact writer + bundle helpers (issue #120).

Each ideator / evaluator submission carries an ``artifacts_uri``
pointing at a file the operator (or, in scripted mode, the worker)
wrote into ``--artifacts-dir``. Two shapes are produced:

- **Single-file artifact.** Text-only ideator content, or a single
  uploaded file with no extra context, is stored directly as
  ``<id>.<ext>`` and the URI points at it.
- **Bundled artifact (``.tar.gz``).** Text + one-or-more uploads, or
  two-or-more uploads, are wrapped in a gzip-compressed tar with a
  top-level ``manifest.json`` enumerating each entry's path / size /
  content-type. The URI points at the tarball. The artifact viewer
  reads ``manifest.json`` and renders per-entry download links
  without auto-unpacking on disk (issue #120 option (1)).

Writes are atomic (write to ``<final>.tmp``, rename to ``<final>``)
so a crash mid-write doesn't leave a half-written artifact behind.
"""

from __future__ import annotations

import io
import json
import mimetypes
import os
import tarfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

# Top-level filename for the manifest inside a bundle. The artifact
# viewer treats this as a reserved name and excludes it from the
# user-visible entry list.
MANIFEST_NAME = "manifest.json"

# Manifest schema version stamped into each bundle. Bump when the
# manifest shape changes incompatibly.
MANIFEST_VERSION = 1


@dataclass(frozen=True)
class UploadedFile:
    """One uploaded file accepted by a route handler.

    ``data`` is the raw bytes; ``filename`` is the operator-supplied
    name (sanitized by the bundler before being used as an archive
    entry). ``content_type`` is the multipart-declared MIME, used as
    a hint when sniffing isn't available.
    """

    filename: str
    data: bytes
    content_type: str | None = None


def _safe_filename(name: str) -> str:
    """Reduce ``name`` to a basename safe to use as a tar entry path.

    Strips any directory components (``a/b/c.pdf`` → ``c.pdf``) so a
    crafted upload can't escape the archive root, and drops NUL
    bytes. Empty / dot results fall back to a generic ``upload``
    placeholder.
    """
    base = Path(name).name.replace("\x00", "")
    if not base or base in (".", ".."):
        return "upload"
    return base


def _detect_content_type(filename: str, declared: str | None) -> str | None:
    if declared:
        return declared
    guess, _ = mimetypes.guess_type(filename)
    return guess


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _sanitize_uploads(uploads: list[UploadedFile]) -> list[str]:
    return [_safe_filename(u.filename) for u in uploads]


def _validate_bundle_names(
    names: list[str], *, text_filename: str | None
) -> None:
    """Reject collisions in the set of archive entry names.

    A bundle has a (possibly-present) text entry at ``text_filename``
    plus each upload at its sanitized basename. If two of these
    resolve to the same name, the tarball would contain a duplicate
    entry — surface this as a ``ValueError`` so the caller can render
    a form error rather than silently shadowing one with another.
    Also rejects any upload sanitizing to the reserved
    ``manifest.json`` slot.
    """
    candidates: list[str] = []
    if text_filename is not None:
        candidates.append(text_filename)
    candidates.extend(names)
    seen: set[str] = set()
    for n in candidates:
        if n == MANIFEST_NAME:
            raise ValueError(
                f"upload filename {n!r} collides with reserved bundle slot"
            )
        if n in seen:
            raise ValueError(f"duplicate filename in bundle: {n!r}")
        seen.add(n)


def predict_artifact_uri(
    artifacts_dir: Path | str,
    artifact_id: str,
    *,
    has_text: bool,
    uploads: list[UploadedFile],
) -> str:
    """Predict the URI :func:`write_artifact_bundle` will return.

    Mirrors the single-file vs bundle branching so callers can
    construct domain objects (``Idea``, ``EvaluationSubmission``)
    with the final URI BEFORE writing anything to disk — keeping the
    validation barrier ahead of any side effect. Raises ``ValueError``
    on the empty / colliding-filename cases the writer would reject.
    """
    base = Path(artifacts_dir).resolve()
    n_uploads = len(uploads)
    if not has_text and n_uploads == 0:
        raise ValueError(
            "artifact requires text content or at least one uploaded file"
        )
    if has_text and n_uploads == 0:
        return (base / f"{artifact_id}.md").as_uri()
    if not has_text and n_uploads == 1:
        ext = Path(_safe_filename(uploads[0].filename)).suffix
        return (base / f"{artifact_id}{ext}").as_uri()
    # Bundle path — pre-validate filenames so prediction fails fast.
    sanitized = _sanitize_uploads(uploads)
    _validate_bundle_names(
        sanitized,
        text_filename="content.md" if has_text else None,
    )
    return (base / f"{artifact_id}.tar.gz").as_uri()


def write_artifact_bundle(
    artifacts_dir: Path | str,
    artifact_id: str,
    *,
    text_content: str | None,
    text_filename: str,
    uploads: list[UploadedFile],
) -> str:
    """Persist an artifact and return its ``file://`` URI.

    Branches:

    - text only → ``<id>.md`` (UTF-8)
    - one upload, no text → ``<id>.<ext>`` (raw upload bytes)
    - text + 1+ uploads OR 2+ uploads → ``<id>.tar.gz`` with
      ``manifest.json`` + each entry under its sanitized basename.

    The text entry (when present) lands at ``text_filename`` inside
    the archive — e.g. ``idea.md`` for ideator submissions,
    ``evaluation.md`` for evaluator submissions — so the viewer can
    surface a role-coherent headline.

    Raises ``ValueError`` if both ``text_content`` is empty and
    ``uploads`` is empty, or if upload filenames collide with each
    other / with ``text_filename`` / with the reserved
    ``manifest.json`` slot.
    """
    base = Path(artifacts_dir).resolve()
    base.mkdir(parents=True, exist_ok=True)

    stripped = text_content.strip() if text_content else None
    has_text = bool(stripped)
    n_uploads = len(uploads)

    if not has_text and n_uploads == 0:
        raise ValueError(
            "artifact requires text content or at least one uploaded file"
        )

    if has_text and n_uploads == 0:
        path = base / f"{artifact_id}.md"
        assert stripped is not None
        _atomic_write_bytes(path, stripped.encode("utf-8"))
        return path.as_uri()

    sanitized = _sanitize_uploads(uploads)

    if not has_text and n_uploads == 1:
        ext = Path(sanitized[0]).suffix
        path = base / f"{artifact_id}{ext}"
        _atomic_write_bytes(path, uploads[0].data)
        return path.as_uri()

    # Bundle path
    _validate_bundle_names(
        sanitized,
        text_filename=text_filename if has_text else None,
    )

    entries: list[dict[str, object]] = []
    payloads: list[tuple[str, bytes]] = []
    if has_text:
        assert stripped is not None
        text_bytes = stripped.encode("utf-8")
        payloads.append((text_filename, text_bytes))
        entries.append(
            {
                "path": text_filename,
                "size": len(text_bytes),
                "content_type": "text/markdown; charset=utf-8",
            }
        )
    for name, up in zip(sanitized, uploads, strict=True):
        ctype = _detect_content_type(name, up.content_type)
        payloads.append((name, up.data))
        entries.append(
            {
                "path": name,
                "size": len(up.data),
                "content_type": ctype,
            }
        )

    manifest = {"version": MANIFEST_VERSION, "entries": entries}
    manifest_bytes = json.dumps(manifest, indent=2).encode("utf-8")

    path = base / f"{artifact_id}.tar.gz"
    tmp = base / f"{artifact_id}.tar.gz.tmp"
    with tarfile.open(tmp, mode="w:gz") as tar:
        _add_tar_entry(tar, MANIFEST_NAME, manifest_bytes)
        for name, data in payloads:
            _add_tar_entry(tar, name, data)
    os.replace(tmp, path)
    return path.as_uri()


def _add_tar_entry(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    info.mode = 0o644
    info.mtime = 0  # reproducible across writes
    tar.addfile(info, io.BytesIO(data))


def write_idea_artifact(
    artifacts_dir: Path | str,
    idea_id: str,
    markdown: str,
) -> str:
    """Write a text-only idea artifact (back-compat shim).

    Kept so existing call sites that pre-date the multi-file bundle
    (e.g. the ideator route's text-only path under tests, and the
    chunked execution-task seeding helpers) keep their existing
    signature. New code routes through :func:`write_artifact_bundle`.
    """
    return write_artifact_bundle(
        artifacts_dir,
        idea_id,
        text_content=markdown,
        text_filename="content.md",
        uploads=[],
    )


# ------------------------------------------------------------------ reading


def is_bundle_uri(uri: str | None) -> bool:
    """``True`` iff ``uri`` looks like a tarball artifact this module wrote."""
    if not uri:
        return False
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return False
    return unquote(parsed.path).endswith(".tar.gz")


def read_bundle_manifest(path: Path) -> dict | None:
    """Read ``manifest.json`` from a bundle. ``None`` on any read error.

    The viewer renders manifests defensively — a missing or
    malformed manifest degrades to "bundle (no manifest)" rather
    than 500-ing the page.
    """
    if not path.is_file():
        return None
    try:
        with tarfile.open(path, mode="r:gz") as tar:
            try:
                member = tar.getmember(MANIFEST_NAME)
            except KeyError:
                return None
            handle = tar.extractfile(member)
            if handle is None:
                return None
            data = handle.read()
    except (tarfile.TarError, OSError):
        return None
    try:
        loaded = json.loads(data)
    except json.JSONDecodeError:
        return None
    if not isinstance(loaded, dict):
        return None
    return loaded


def read_bundle_entry(
    path: Path,
    entry_name: str,
    *,
    max_bytes: int | None = None,
) -> bytes | None:
    """Read one entry from a bundle, or ``None`` on any failure.

    ``max_bytes`` short-circuits before extraction so a giant entry
    can't OOM the viewer. The viewer's inline-render call passes a
    1 MiB cap; the per-entry download route omits the cap and
    streams via :func:`tarfile.extractfile`.
    """
    try:
        with tarfile.open(path, mode="r:gz") as tar:
            try:
                member = tar.getmember(entry_name)
            except KeyError:
                return None
            if not member.isfile():
                return None
            if max_bytes is not None and member.size > max_bytes:
                return None
            handle = tar.extractfile(member)
            if handle is None:
                return None
            return handle.read()
    except (tarfile.TarError, OSError):
        return None
