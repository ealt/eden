"""Artifact-metadata store operations mixin (issue #166).

The reference artifact store records a per-artifact metadata row
alongside the bytes (which live in a separate ``ArtifactBackend``). This
mixin owns the metadata half: ``create_artifact`` / ``read_artifact``.

Unlike every other store write, an artifact deposit carries **no event**:
the artifact store is a distinct store from the task store / event log
(``08-storage.md`` §5), the metadata row is not bound to any
task / idea / variant transition, and a deposit precedes the object that
references its URI (``07-wire-protocol.md`` §16.1). The ``05-event-protocol.md``
§2 transactional invariant therefore does not apply — there is no
protocol-owned state change observable via tasks / ideas / variants to
pair an event with.
"""

from __future__ import annotations

from eden_contracts import ArtifactMetadata

from .._base import _StoreCore, _Tx
from ..errors import AlreadyExists, NotFound
from ._helpers import _deep


class _ArtifactOpsMixin(_StoreCore):
    """Artifact-metadata create + read (``08-storage.md`` §5.5)."""

    def read_artifact(self, opaque_id: str) -> ArtifactMetadata:
        """Return the artifact metadata row, or raise ``NotFound``."""
        with self._atomic_operation():
            metadata = self._get_artifact(opaque_id)
            if metadata is None:
                raise NotFound(f"artifact {opaque_id!r}")
            return _deep(metadata)

    def create_artifact(
        self,
        *,
        opaque_id: str,
        created_by: str,
        size_bytes: int,
        content_type: str,
    ) -> None:
        """Record an artifact metadata row (``08-storage.md`` §5.5).

        ``created_by`` is the depositing principal (stamped from the
        authenticated bearer at the wire layer, ``07-wire-protocol.md``
        §13.3) and is the sole key for the §16.2 fetch ACL. A reuse of an
        existing ``opaque_id`` raises ``AlreadyExists`` — the server mints
        a fresh id per deposit, so this is a defensive backstop mirroring
        the backend's exclusive-create.
        """
        with self._atomic_operation():
            if self._get_artifact(opaque_id) is not None:
                raise AlreadyExists(f"artifact {opaque_id!r}")
            tx = _Tx()
            tx.artifacts[opaque_id] = ArtifactMetadata(
                opaque_id=opaque_id,
                created_by=created_by,
                size_bytes=size_bytes,
                content_type=content_type,
                created_at=self._ts(),
            )
            self._apply_commit(tx)
