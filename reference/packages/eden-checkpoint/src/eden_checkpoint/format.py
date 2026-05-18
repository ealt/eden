"""Tar envelope + JSONL + content-addressed artifact reader / writer.

The portable-checkpoint format (``spec/v0/10-checkpoints.md`` §3) is a
tar archive of a directory tree. This module implements both sides of
the format:

- :class:`CheckpointWriter` — streaming tar producer used by exporters.
  Append entries in any order; finalize on ``__exit__``.
- :class:`CheckpointReader` — reader rooted at an already-extracted
  directory; exposes the parsed manifest and per-file accessors.
- :func:`extract_checkpoint` — convenience that untars an archive stream
  into a destination directory and returns a reader. The reference Store
  uses this pattern per the §6 materialize-then-stream design.

Path safety: ``extract_checkpoint`` uses ``tarfile``'s ``data`` filter
(Python 3.12+), which rejects entries that would escape the destination
directory or carry hardlinks, symlinks, or unsafe permission bits.
"""

from __future__ import annotations

import io
import json
import tarfile
from collections.abc import Iterable, Iterator
from pathlib import Path
from types import TracebackType
from typing import Any, BinaryIO, Self

from ._hashing import is_valid_sha256_hex, sha256_hex
from .errors import CheckpointInvalid, UnsupportedCheckpointVersion
from .manifest import (
    ARTIFACT_URI_PREFIX,
    CHECKPOINT_FORMAT_VERSION,
    DEFAULT_FILES,
    CheckpointManifest,
    ManifestFiles,
)

_MANIFEST_FILENAME = "manifest.json"
"""The fixed filename of the manifest within the archive root."""

_ROOT_DIR_NAME = "checkpoint"
"""The top-level directory name inside the tar archive.

``spec/v0/10-checkpoints.md`` §4 leaves this name implementation-defined;
this binding uses the literal string ``"checkpoint"`` so a hand-extracted
archive has a predictable shape.
"""


def _serialize_jsonl_row(obj: Any) -> bytes:
    r"""Serialize one JSONL row.

    The serializer is compact (no extra whitespace) and uses ``ensure_ascii=False``
    so non-ASCII fields round-trip without escaping. Each row terminates
    with a single ``\n``.
    """
    encoded = json.dumps(obj, ensure_ascii=False, sort_keys=False, separators=(",", ":"))
    return (encoded + "\n").encode("utf-8")


class CheckpointWriter:
    """Streaming tar writer for a portable-checkpoint archive.

    Use as a context manager. Entries may be written in any order; the
    writer enforces dedup on artifacts (content-addressed by SHA-256)
    and tracks emitted files so :meth:`build_manifest` can populate
    ``counts`` accurately when the caller does not supply them.

    The tar envelope is finalized when the context manager exits. If an
    exception escapes the ``with`` body, the partial tar bytes already
    written remain in the stream — callers MUST treat the stream as
    invalid on exception.
    """

    def __init__(
        self,
        stream: BinaryIO,
        *,
        files: ManifestFiles = DEFAULT_FILES,
        root_dir_name: str = _ROOT_DIR_NAME,
    ) -> None:
        """Initialize the writer over ``stream``.

        Args:
            stream: A binary writable stream. The writer appends tar
                bytes to it; the stream remains open after the context
                exits — callers manage its lifecycle.
            files: Optional override of the per-component path layout.
                Defaults to ``DEFAULT_FILES`` (matches the canonical
                ``10-checkpoints.md`` §3 layout).
            root_dir_name: Optional override of the tar archive's
                top-level directory name; defaults to ``"checkpoint"``.
        """
        # SIM115: tarfile lifecycle is managed by close() / __exit__; a
        # context manager wrapper would defeat the writer's own context.
        self._tar: tarfile.TarFile = tarfile.open(  # noqa: SIM115
            fileobj=stream, mode="w|", format=tarfile.PAX_FORMAT
        )
        self._files: ManifestFiles = files
        self._root: str = root_dir_name
        self._closed: bool = False
        self._seen_artifact_hashes: set[str] = set()
        self._counts: dict[str, int] = {
            "tasks": 0,
            "ideas": 0,
            "variants": 0,
            "submissions": 0,
            "events": 0,
            "workers": 0,
            "groups": 0,
        }
        self._registered_files: set[str] = set()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Finalize the tar stream. Idempotent."""
        if self._closed:
            return
        self._tar.close()
        self._closed = True

    def write_text(self, archive_path: str, text: str) -> None:
        """Write a UTF-8 text entry at ``archive_path`` (relative to the archive root)."""
        self.write_bytes(archive_path, text.encode("utf-8"))

    def write_bytes(self, archive_path: str, data: bytes) -> None:
        """Write raw bytes at ``archive_path`` (relative to the archive root)."""
        if self._closed:
            raise RuntimeError("CheckpointWriter is closed")
        if archive_path in self._registered_files:
            raise RuntimeError(f"duplicate archive entry: {archive_path!r}")
        info = tarfile.TarInfo(name=f"{self._root}/{archive_path}")
        info.size = len(data)
        info.mode = 0o644
        self._tar.addfile(info, io.BytesIO(data))
        self._registered_files.add(archive_path)

    def write_jsonl(self, kind: str, rows: Iterable[Any]) -> int:
        """Serialize ``rows`` as JSONL into the file named by ``kind``.

        ``kind`` MUST be one of the keys on :class:`ManifestFiles` whose
        value is the on-archive filename (e.g., ``"tasks"`` maps to
        ``tasks.jsonl``). Returns the number of rows written; callers
        SHOULD pass this to :meth:`build_manifest` via the ``counts``
        argument or let the writer auto-populate it.
        """
        archive_path = self._jsonl_path_for(kind)
        buffer = bytearray()
        n = 0
        for row in rows:
            buffer.extend(_serialize_jsonl_row(row))
            n += 1
        self.write_bytes(archive_path, bytes(buffer))
        if kind in self._counts:
            self._counts[kind] = n
        return n

    def write_artifact(self, data: bytes) -> str:
        """Write an artifact and return its content-addressed URI.

        The artifact is hashed and written at
        ``artifacts/sha256/<hex>``. Duplicate writes (same byte content)
        are deduped — the second call returns the same URI without
        re-emitting bytes.

        Returns:
            A ``checkpoint:sha256:<hex>`` URI suitable for use as an
            ``artifacts_uri`` value in the JSONL rows.
        """
        digest = sha256_hex(data)
        uri = f"{ARTIFACT_URI_PREFIX}{digest}"
        if digest in self._seen_artifact_hashes:
            return uri
        archive_path = f"{self._files.artifacts_dir}/{digest}"
        self.write_bytes(archive_path, data)
        self._seen_artifact_hashes.add(digest)
        return uri

    def write_repo_bundle(self, bundle_bytes: bytes) -> None:
        """Write the git bundle at ``repo.bundle``."""
        self.write_bytes(self._files.repo_bundle, bundle_bytes)

    def write_experiment_config(self, content: str | bytes) -> None:
        """Write the experiment config (verbatim) at the manifest's ``experiment_config`` path."""
        if isinstance(content, str):
            self.write_text(self._files.experiment_config, content)
        else:
            self.write_bytes(self._files.experiment_config, content)

    def write_experiment(self, experiment_obj: Any) -> None:
        """Write the runtime experiment object as JSON at the manifest's ``experiment`` path."""
        data = json.dumps(experiment_obj, ensure_ascii=False, sort_keys=False).encode("utf-8")
        self.write_bytes(self._files.experiment, data)

    def write_manifest(self, manifest: CheckpointManifest) -> None:
        """Write the manifest JSON at ``manifest.json``.

        Implementations SHOULD call this last so the manifest's
        ``counts`` reflect the actually-written rows; callers using
        :meth:`build_manifest` get that automatically.
        """
        data = json.dumps(
            manifest.model_dump(mode="json"), ensure_ascii=False, indent=2
        ).encode("utf-8")
        if self._closed:
            raise RuntimeError("CheckpointWriter is closed")
        if _MANIFEST_FILENAME in self._registered_files:
            raise RuntimeError("manifest already written")
        info = tarfile.TarInfo(name=f"{self._root}/{_MANIFEST_FILENAME}")
        info.size = len(data)
        info.mode = 0o644
        self._tar.addfile(info, io.BytesIO(data))
        self._registered_files.add(_MANIFEST_FILENAME)

    @property
    def counts(self) -> dict[str, int]:
        """Snapshot of per-kind row counts written so far."""
        return dict(self._counts)

    @property
    def files(self) -> ManifestFiles:
        """The path layout this writer emits."""
        return self._files

    def _jsonl_path_for(self, kind: str) -> str:
        path_map: dict[str, str] = {
            "tasks": self._files.tasks,
            "ideas": self._files.ideas,
            "variants": self._files.variants,
            "submissions": self._files.submissions,
            "events": self._files.events,
            "workers": self._files.workers,
            "groups": self._files.groups,
        }
        try:
            return path_map[kind]
        except KeyError as exc:
            raise ValueError(f"unknown JSONL kind: {kind!r}") from exc


class CheckpointReader:
    """Reader rooted at an extracted checkpoint directory.

    The reader does NOT untar the archive itself — callers use
    :func:`extract_checkpoint` for that. Once a reader is constructed,
    :attr:`manifest` is the parsed manifest object and the
    ``iter_*`` / ``read_*`` methods walk the tree.

    The reader validates the format version on construction and
    raises :class:`UnsupportedCheckpointVersion` if the manifest's
    ``checkpoint_format_version`` does not match this binding's
    ``CHECKPOINT_FORMAT_VERSION``. Spec-version mismatches are caller-
    side concerns and raised by the importer, not here.
    """

    def __init__(self, root: Path) -> None:
        """Construct a reader at ``root`` (a directory containing ``manifest.json``)."""
        self._root: Path = root
        manifest_path = root / _MANIFEST_FILENAME
        if not manifest_path.is_file():
            raise CheckpointInvalid(f"missing {_MANIFEST_FILENAME} at archive root")
        try:
            manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CheckpointInvalid(f"unreadable {_MANIFEST_FILENAME}: {exc}") from exc
        try:
            self._manifest: CheckpointManifest = CheckpointManifest.model_validate(manifest_data)
        except Exception as exc:
            raise CheckpointInvalid(f"manifest schema validation failed: {exc}") from exc
        if self._manifest.checkpoint_format_version != CHECKPOINT_FORMAT_VERSION:
            raise UnsupportedCheckpointVersion(
                f"checkpoint_format_version={self._manifest.checkpoint_format_version!r}, "
                f"this binding emits {CHECKPOINT_FORMAT_VERSION!r}"
            )

    @property
    def manifest(self) -> CheckpointManifest:
        """The parsed manifest."""
        return self._manifest

    @property
    def root(self) -> Path:
        """The extracted-archive root directory."""
        return self._root

    def iter_jsonl(self, kind: str) -> Iterator[dict[str, Any]]:
        """Yield each row of the JSONL file named by ``kind``.

        ``kind`` MUST be one of ``tasks`` / ``ideas`` / ``variants`` /
        ``submissions`` / ``events`` / ``workers`` / ``groups``.
        """
        archive_path = self._jsonl_path_for(kind)
        full = self._root / archive_path
        if not full.is_file():
            raise CheckpointInvalid(f"missing JSONL file: {archive_path}")
        with full.open("r", encoding="utf-8") as fh:
            for line_no, raw in enumerate(fh, start=1):
                if not raw.strip():
                    raise CheckpointInvalid(
                        f"empty line at {archive_path}:{line_no}; "
                        "the format MUST NOT contain blank lines"
                    )
                try:
                    yield json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise CheckpointInvalid(
                        f"malformed JSON at {archive_path}:{line_no}: {exc}"
                    ) from exc

    def read_artifact(self, digest: str) -> bytes:
        """Return the bytes of the artifact named by ``digest``.

        ``digest`` is the lowercase-hex SHA-256 portion of a
        ``checkpoint:sha256:<hex>`` URI.
        """
        if not is_valid_sha256_hex(digest):
            raise CheckpointInvalid(f"invalid artifact digest: {digest!r}")
        artifact_path = self._root / self._manifest.files.artifacts_dir / digest
        if not artifact_path.is_file():
            raise CheckpointInvalid(f"missing artifact: sha256:{digest}")
        return artifact_path.read_bytes()

    def iter_artifact_digests(self) -> Iterator[str]:
        """Yield each artifact digest present under the artifacts directory."""
        artifacts_root = self._root / self._manifest.files.artifacts_dir
        if not artifacts_root.is_dir():
            return
        for entry in sorted(artifacts_root.iterdir()):
            if entry.is_file() and is_valid_sha256_hex(entry.name):
                yield entry.name

    def read_repo_bundle_path(self) -> Path:
        """Return the on-disk path of the git bundle.

        The bundle is presented as a path (rather than bytes) so the
        importer can pass it to ``git fetch`` without re-materializing
        the file.
        """
        bundle_path = self._root / self._manifest.files.repo_bundle
        if not bundle_path.is_file():
            raise CheckpointInvalid(f"missing repo bundle: {self._manifest.files.repo_bundle}")
        return bundle_path

    def read_experiment_config(self) -> str:
        """Return the experiment config bytes decoded as UTF-8."""
        path = self._root / self._manifest.files.experiment_config
        if not path.is_file():
            raise CheckpointInvalid(
                f"missing experiment config: {self._manifest.files.experiment_config}"
            )
        return path.read_text(encoding="utf-8")

    def read_experiment(self) -> dict[str, Any]:
        """Return the experiment runtime object as a parsed dict."""
        path = self._root / self._manifest.files.experiment
        if not path.is_file():
            raise CheckpointInvalid(f"missing experiment.json: {self._manifest.files.experiment}")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CheckpointInvalid(f"malformed experiment.json: {exc}") from exc

    def _jsonl_path_for(self, kind: str) -> str:
        files = self._manifest.files
        path_map: dict[str, str] = {
            "tasks": files.tasks,
            "ideas": files.ideas,
            "variants": files.variants,
            "submissions": files.submissions,
            "events": files.events,
            "workers": files.workers,
            "groups": files.groups,
        }
        try:
            return path_map[kind]
        except KeyError as exc:
            raise ValueError(f"unknown JSONL kind: {kind!r}") from exc


def extract_checkpoint(stream: BinaryIO, dest_dir: Path) -> CheckpointReader:
    """Untar a checkpoint archive into ``dest_dir`` and return a reader.

    ``dest_dir`` MUST already exist and be writable. The archive's
    top-level directory becomes a single child of ``dest_dir``; the
    returned reader is rooted at that child.

    Tar extraction uses the ``data`` filter (Python 3.12+) which rejects
    path-traversal, hardlinks, symlinks, special files, and unsafe
    permission bits. Malformed archives raise :class:`CheckpointInvalid`.
    """
    if not dest_dir.is_dir():
        raise ValueError(f"destination directory does not exist: {dest_dir}")

    # SIM115: closed in the finally block below; context-manager wrapping
    # would obscure the iteration loop.
    try:
        tar = tarfile.open(fileobj=stream, mode="r|")  # noqa: SIM115
    except tarfile.TarError as exc:
        raise CheckpointInvalid(f"unreadable tar archive: {exc}") from exc

    top_level_dirs: set[str] = set()
    try:
        for member in tar:
            # Track the top-level directory so we can return a reader rooted at it.
            parts = Path(member.name).parts
            if not parts:
                raise CheckpointInvalid("empty member name in archive")
            top_level_dirs.add(parts[0])
            tar.extract(member, path=dest_dir, filter="data")
    except (tarfile.TarError, OSError, ValueError) as exc:
        raise CheckpointInvalid(f"failed to extract tar archive: {exc}") from exc
    finally:
        tar.close()

    if len(top_level_dirs) != 1:
        raise CheckpointInvalid(
            f"archive MUST have a single top-level directory; found {sorted(top_level_dirs)}"
        )
    root = dest_dir / next(iter(top_level_dirs))
    if not root.is_dir():
        raise CheckpointInvalid(f"extracted archive root is not a directory: {root}")
    return CheckpointReader(root)
