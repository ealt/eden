"""Blob backends for the reference artifact store (issue #166).

The artifact store is split in two: a **Store**-side metadata row
(``create_artifact`` / ``read_artifact`` — ACL + content-type) and a
**backend** that is a dumb bytes-in / bytes-out blob store. This module
owns the backend half.

The ``ArtifactBackend`` Protocol is deliberately metadata-free so a
future S3 / GCS backend (Phase 13d, the ``eden-blob`` package) can
satisfy it trivially — all attribution, sizing, and ACL state lives in
the Store, not here. Two reference backends ship today:

- :class:`FileArtifactBackend` — the Compose default; writes
  opaque-id-named blobs under a configured root via the exclusive-create
  / atomic-link idiom (no-overwrite per ``08-storage.md`` §5.4) and reads
  them back behind ``O_NOFOLLOW`` + regular-file guards. The opaque id is
  validated against its single-segment hex grammar first, so — unlike the
  retired ``_reference`` artifact route — there is no client-supplied path
  and therefore no traversal surface.
- :class:`InMemoryArtifactBackend` — a dict, for tests and in-process
  deployments.
"""

from __future__ import annotations

import contextlib
import os
import re
import stat
from pathlib import Path
from typing import Protocol, runtime_checkable

from .errors import NotFound

_OPAQUE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
"""Single-segment hex grammar for the server-minted opaque id.

Mirrors ``spec/v0/schemas/artifact-metadata.schema.json`` and
``eden_contracts.artifact.OPAQUE_ID_PATTERN``. Validating against this
before any filesystem call is what lets :class:`FileArtifactBackend`
skip the descriptor-walk traversal defense the client-path-bearing
``_reference`` route needed: a hex-only id can carry no ``/`` or ``..``.
"""

_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC


def _require_valid_opaque_id(opaque_id: str) -> None:
    if not _OPAQUE_ID_RE.fullmatch(opaque_id):
        raise ValueError(f"invalid opaque artifact id {opaque_id!r}")


@runtime_checkable
class ArtifactBackend(Protocol):
    """Bytes-in / bytes-out blob store keyed by opaque id.

    Metadata-free by design (``08-storage.md`` §5.5): the Store owns the
    attribution / size / content-type row. A backend only persists and
    returns the bytes.
    """

    def store(self, opaque_id: str, data: bytes) -> None:
        """Persist ``data`` under ``opaque_id``.

        MUST be exclusive-create: a reuse of an existing ``opaque_id``
        raises ``FileExistsError`` (the ``08-storage.md`` §5.4
        no-overwrite guarantee, independent of caller id-discipline).
        """
        ...

    def load(self, opaque_id: str) -> bytes:
        """Return the bytes previously stored under ``opaque_id``.

        Raises :class:`eden_storage.errors.NotFound` if absent.
        """
        ...


class InMemoryArtifactBackend:
    """Dict-backed blob store for tests / in-process deployments."""

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    def store(self, opaque_id: str, data: bytes) -> None:
        """Persist ``data``; ``FileExistsError`` on a reused id (§5.4)."""
        _require_valid_opaque_id(opaque_id)
        if opaque_id in self._blobs:
            raise FileExistsError(f"artifact {opaque_id!r} already exists")
        self._blobs[opaque_id] = bytes(data)

    def load(self, opaque_id: str) -> bytes:
        """Return the stored bytes; ``NotFound`` if absent."""
        _require_valid_opaque_id(opaque_id)
        try:
            return self._blobs[opaque_id]
        except KeyError as exc:
            raise NotFound(f"artifact {opaque_id!r}") from exc


class FileArtifactBackend:
    """Local-filesystem blob store: one opaque-id-named file under ``root``."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def store(self, opaque_id: str, data: bytes) -> None:
        """Persist ``data`` under ``root/opaque_id``; ``FileExistsError`` on reuse."""
        _require_valid_opaque_id(opaque_id)
        final = self._root / opaque_id
        tmp = self._root / f".{opaque_id}.tmp"
        tmp.write_bytes(data)
        # os.link is atomic and raises FileExistsError when `final`
        # already exists — the §5.4 no-overwrite guarantee. The temp link
        # is always dropped: on success `final` keeps its own hard link to
        # the data; on failure the orphan temp is cleaned up.
        try:
            os.link(tmp, final)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)

    def load(self, opaque_id: str) -> bytes:
        """Return the bytes under ``root/opaque_id``; ``NotFound`` if absent."""
        _require_valid_opaque_id(opaque_id)
        try:
            fd = os.open(self._root / opaque_id, _READ_FLAGS)
        except FileNotFoundError as exc:
            raise NotFound(f"artifact {opaque_id!r}") from exc
        try:
            st = os.fstat(fd)
            # Regular-file check on the open fd (not the path) closes the
            # TOCTOU window; O_NOFOLLOW already rejected a symlink at the
            # leaf. Defense-in-depth even though the opaque-id grammar means
            # the server never created anything but a plain file here.
            if not stat.S_ISREG(st.st_mode):
                raise NotFound(f"artifact {opaque_id!r}")
            return os.read(fd, st.st_size)
        finally:
            os.close(fd)
