"""Hierarchical artifact substrate: path-builder + bundle writer (issue #168).

Every ideator / executor / evaluator submission carries an ``artifacts_uri``
pointing at a file the operator (or, in scripted mode, the worker) wrote into
``--artifacts-dir``. This module is the single home for **where** those bytes
land and **what** they're named, so the layout lives in exactly one place that
the web-UI routes, the ideator subprocess host, and (by hand-mirror) the
standalone ``eden-manual`` CLI all agree on.

**Layout (issue #168, entity-hierarchical).** Artifacts are grouped under
``artifacts/`` by the durable entity that owns them and the role that produced
them::

    artifacts/
      ideas/<idea_id>/                    # ideator-produced
        content.md                        #   text-only idea
        <sanitized-upload>                #   single uploaded file
        bundle.tar.gz                     #   text + uploads / multi-file
      variants/<variant_id>/
        executor/                         # executor-produced
          exec-<uuid>.{md,<ext>,tar.gz}
        evaluator/                        # evaluator-produced
          eval-<uuid>.{md,<ext>,tar.gz}

The top-level dirs use the **artifact noun** (``ideas`` / ``variants``); the
variant sub-dirs use the **producing-role noun** (``executor`` / ``evaluator``)
because a variant aggregates artifacts from two sources. See the plan's §5
naming map for the rationale.

**Two write shapes per submission:**

- **Single-file artifact.** Text-only content, or a single uploaded file with no
  extra context, is stored directly and the URI points at it.
- **Bundled artifact (``.tar.gz``).** Text + one-or-more uploads, or two-or-more
  uploads, are wrapped in a gzip-compressed tar with a top-level
  ``manifest.json`` enumerating each entry's path / size / content-type. The URI
  points at the tarball. The artifact viewer reads ``manifest.json`` and renders
  per-entry download links without auto-unpacking on disk (issue #120).

**Filename policy (``ArtifactNaming``).** The entity dir's leaf-file naming
turns on whether the dir is *write-once* (one idea per ``ideas/<idea_id>/``, so
clean fixed names) or *accumulates* across submissions (``evaluator/`` and
``executor/`` are keyed only by the stable ``variant_id``, so each submission
mints a fresh ``eval-<uuid>`` / ``exec-<uuid>`` stem to stay distinct).

**No-overwrite (spec §5.4).** Every current caller mints a fresh id per write
(``uuid4().hex`` for ideas, ``eval-<uuid>`` / ``exec-<uuid>`` for submissions),
so no target path is ever written twice. As belt-and-suspenders against a future
caller that reuses an id, the final materialization is an *exclusive* link that
raises ``FileExistsError`` rather than clobbering — making the guarantee
independent of caller id-discipline.
"""

from __future__ import annotations

import contextlib
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


@dataclass(frozen=True)
class ArtifactNaming:
    """How to name an entity dir's leaf file(s) and the bundle headline.

    - ``headline`` is the text entry's name *inside* a bundle — role-coherent
      (``content.md`` for ideas, ``evaluation.md`` for evaluations,
      ``variant.md`` for executor artifacts). The artifact viewer extracts this
      entry as the inline markdown headline.
    - ``text_only_name`` / ``bundle_name`` are the on-disk basenames for the
      text-only and bundle write branches within the entity dir.
    - ``single_stem`` controls the single-upload branch: ``None`` means use the
      upload's own sanitized basename (the idea policy — its dir is unique, so
      the operator's filename survives verbatim); a string means
      ``<single_stem><ext>`` (the evaluator / executor policy — a shared dir
      keyed by ``variant_id``, so a per-submission stem keeps writes distinct).

    Construct via :func:`idea_naming` / :func:`submission_naming` rather than
    directly so the two policies stay in one place.
    """

    headline: str
    text_only_name: str
    bundle_name: str
    single_stem: str | None

    def single_upload_name(self, upload: UploadedFile) -> str:
        """Basename for the single-upload branch under this policy."""
        safe = _safe_filename(upload.filename)
        if self.single_stem is None:
            return safe
        return f"{self.single_stem}{Path(safe).suffix}"


def idea_naming() -> ArtifactNaming:
    """Naming policy for an ideator submission (``ideas/<idea_id>/``).

    The dir is write-once (one idea per ``idea_id``), so leaf names are clean
    fixed names: ``content.md`` (matching the subprocess host + binding §2.3),
    the upload's own name for a lone upload, and ``bundle.tar.gz`` for a bundle.
    """
    return ArtifactNaming(
        headline="content.md",
        text_only_name="content.md",
        bundle_name="bundle.tar.gz",
        single_stem=None,
    )


def submission_naming(stem: str, *, headline: str) -> ArtifactNaming:
    """Naming policy for an accumulating variant sub-dir (evaluator / executor).

    ``stem`` is the per-submission unique stem (``eval-<uuid>`` /
    ``exec-<uuid>``); ``headline`` is the role-coherent bundle headline entry
    (``evaluation.md`` / ``variant.md``). The shared dir is keyed only by the
    stable ``variant_id``, so the stem keeps every submission's leaf file
    distinct.
    """
    return ArtifactNaming(
        headline=headline,
        text_only_name=f"{stem}.md",
        bundle_name=f"{stem}.tar.gz",
        single_stem=stem,
    )


def entity_artifact_dir(
    artifacts_dir: Path | str, *, producer: str, entity_id: str
) -> Path:
    """Resolve the directory under ``artifacts_dir`` for one producer's bytes.

    ``producer`` is ``"ideator"`` (entity = ``idea_id``), ``"executor"`` or
    ``"evaluator"`` (entity = ``variant_id``). Returns an absolute path; the
    base is resolved so the URIs the writer stamps are canonical.
    """
    base = Path(artifacts_dir).resolve()
    if producer == "ideator":
        return base / "ideas" / entity_id
    if producer == "executor":
        return base / "variants" / entity_id / "executor"
    if producer == "evaluator":
        return base / "variants" / entity_id / "evaluator"
    raise ValueError(f"unknown artifact producer: {producer!r}")


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


def _materialize_exclusive(tmp: Path, final: Path) -> None:
    """Atomically move ``tmp`` → ``final``, refusing to clobber an existing file.

    ``os.link`` is atomic and raises ``FileExistsError`` if ``final`` already
    exists, giving the spec §5.4 no-overwrite guarantee independent of caller
    id-discipline. The temp link is always dropped — on success ``final`` keeps
    its own hard link to the data; on failure the orphan temp is cleaned up.
    """
    try:
        os.link(tmp, final)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


def _atomic_write_bytes_exclusive(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    _materialize_exclusive(tmp, path)


def _sanitize_uploads(uploads: list[UploadedFile]) -> list[str]:
    return [_safe_filename(u.filename) for u in uploads]


def _validate_bundle_names(names: list[str], *, headline: str | None) -> None:
    """Reject collisions in the set of archive entry names.

    A bundle has a (possibly-present) text entry at ``headline`` plus each
    upload at its sanitized basename. If two of these resolve to the same name,
    the tarball would contain a duplicate entry — surface this as a
    ``ValueError`` so the caller can render a form error rather than silently
    shadowing one with another. Also rejects any upload sanitizing to the
    reserved ``manifest.json`` slot.
    """
    candidates: list[str] = []
    if headline is not None:
        candidates.append(headline)
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
    target_dir: Path | str,
    naming: ArtifactNaming,
    *,
    has_text: bool,
    uploads: list[UploadedFile],
) -> str:
    """Predict the URI :func:`write_artifact_bundle` will return.

    Mirrors the single-file vs bundle branching so callers can construct domain
    objects (``Idea``, ``EvaluationSubmission``) with the final URI BEFORE
    writing anything to disk — keeping the validation barrier ahead of any side
    effect. Raises ``ValueError`` on the empty / colliding-filename cases the
    writer would reject. ``target_dir`` is the resolved entity dir
    (:func:`entity_artifact_dir`); ``naming`` is the §D.2 filename policy.
    """
    base = Path(target_dir)
    n_uploads = len(uploads)
    if not has_text and n_uploads == 0:
        raise ValueError(
            "artifact requires text content or at least one uploaded file"
        )
    if has_text and n_uploads == 0:
        return (base / naming.text_only_name).as_uri()
    if not has_text and n_uploads == 1:
        return (base / naming.single_upload_name(uploads[0])).as_uri()
    # Bundle path — pre-validate filenames so prediction fails fast.
    sanitized = _sanitize_uploads(uploads)
    _validate_bundle_names(
        sanitized, headline=naming.headline if has_text else None
    )
    return (base / naming.bundle_name).as_uri()


def write_artifact_bundle(
    target_dir: Path | str,
    naming: ArtifactNaming,
    *,
    text_content: str | None,
    uploads: list[UploadedFile],
) -> str:
    """Persist an artifact under ``target_dir`` and return its ``file://`` URI.

    Branches (basenames per the ``naming`` policy):

    - text only → ``naming.text_only_name`` (UTF-8)
    - one upload, no text → ``naming.single_upload_name(upload)`` (raw bytes)
    - text + 1+ uploads OR 2+ uploads → ``naming.bundle_name`` (``.tar.gz``) with
      ``manifest.json`` + the text entry at ``naming.headline`` + each upload
      under its sanitized basename.

    Raises ``ValueError`` if both ``text_content`` is empty and ``uploads`` is
    empty, or if upload filenames collide with each other / with the headline /
    with the reserved ``manifest.json`` slot. Raises ``FileExistsError`` if the
    resolved target path already exists (no-overwrite; spec §5.4).
    """
    base = Path(target_dir)
    base.mkdir(parents=True, exist_ok=True)

    stripped = text_content.strip() if text_content else None
    has_text = bool(stripped)
    n_uploads = len(uploads)

    if not has_text and n_uploads == 0:
        raise ValueError(
            "artifact requires text content or at least one uploaded file"
        )

    if has_text and n_uploads == 0:
        path = base / naming.text_only_name
        assert stripped is not None
        _atomic_write_bytes_exclusive(path, stripped.encode("utf-8"))
        return path.as_uri()

    sanitized = _sanitize_uploads(uploads)

    if not has_text and n_uploads == 1:
        path = base / naming.single_upload_name(uploads[0])
        _atomic_write_bytes_exclusive(path, uploads[0].data)
        return path.as_uri()

    # Bundle path
    _validate_bundle_names(
        sanitized, headline=naming.headline if has_text else None
    )

    entries: list[dict[str, object]] = []
    payloads: list[tuple[str, bytes]] = []
    if has_text:
        assert stripped is not None
        text_bytes = stripped.encode("utf-8")
        payloads.append((naming.headline, text_bytes))
        entries.append(
            {
                "path": naming.headline,
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

    path = base / naming.bundle_name
    tmp = base / (naming.bundle_name + ".tmp")
    with tarfile.open(tmp, mode="w:gz") as tar:
        _add_tar_entry(tar, MANIFEST_NAME, manifest_bytes)
        for name, data in payloads:
            _add_tar_entry(tar, name, data)
    _materialize_exclusive(tmp, path)
    return path.as_uri()


def _add_tar_entry(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    info.mode = 0o644
    info.mtime = 0  # reproducible across writes
    tar.addfile(info, io.BytesIO(data))


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

    The viewer renders manifests defensively — a missing or malformed manifest
    degrades to "bundle (no manifest)" rather than 500-ing the page.
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

    ``max_bytes`` short-circuits before extraction so a giant entry can't OOM
    the viewer. The viewer's inline-render call passes a 1 MiB cap; the
    per-entry download route omits the cap and streams via
    :func:`tarfile.extractfile`.
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
