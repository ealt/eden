"""Artifact store conformance: metadata rows + blob backends (issue #166).

The artifact store is split in two — Store-side metadata
(``create_artifact`` / ``read_artifact``) and a bytes-only
``ArtifactBackend``. The metadata tests run against every reference
Store backend via the parametrized ``make_store`` fixture; the backend
tests cover both reference backends directly.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
from eden_storage import (
    AlreadyExists,
    ArtifactStore,
    FileArtifactBackend,
    InMemoryArtifactBackend,
    NotFound,
    Store,
)

_HEX = "0123456789abcdef0123456789abcdef"
_HEX2 = "fedcba9876543210fedcba9876543210"


# ---------------------------------------------------------------------
# Store-side metadata (parametrized across in-memory / sqlite / postgres)
# ---------------------------------------------------------------------


class TestArtifactMetadata:
    def test_create_then_read_roundtrips(
        self, make_store: Callable[..., Store]
    ) -> None:
        store = cast(ArtifactStore, make_store())
        store.create_artifact(
            opaque_id=_HEX,
            created_by="eric",
            size_bytes=2048,
            content_type="application/gzip",
        )
        meta = store.read_artifact(_HEX)
        assert meta.opaque_id == _HEX
        assert meta.created_by == "eric"
        assert meta.size_bytes == 2048
        assert meta.content_type == "application/gzip"
        assert meta.created_at.endswith("Z")

    def test_read_absent_raises_not_found(
        self, make_store: Callable[..., Store]
    ) -> None:
        store = cast(ArtifactStore, make_store())
        with pytest.raises(NotFound):
            store.read_artifact(_HEX)

    def test_duplicate_opaque_id_rejected(
        self, make_store: Callable[..., Store]
    ) -> None:
        store = cast(ArtifactStore, make_store())
        store.create_artifact(
            opaque_id=_HEX,
            created_by="eric",
            size_bytes=1,
            content_type="text/plain",
        )
        with pytest.raises(AlreadyExists):
            store.create_artifact(
                opaque_id=_HEX,
                created_by="eric",
                size_bytes=2,
                content_type="text/plain",
            )

    def test_admin_depositor_recorded(
        self, make_store: Callable[..., Store]
    ) -> None:
        store = cast(ArtifactStore, make_store())
        store.create_artifact(
            opaque_id=_HEX2,
            created_by="admin",
            size_bytes=0,
            content_type="text/markdown",
        )
        assert store.read_artifact(_HEX2).created_by == "admin"

    def test_no_event_emitted_for_artifact(
        self, make_store: Callable[..., Store]
    ) -> None:
        # The artifact store is distinct from the event log (08-storage.md
        # §5); a deposit carries no event.
        store = cast(ArtifactStore, make_store())
        before = len(store.events())
        store.create_artifact(
            opaque_id=_HEX,
            created_by="eric",
            size_bytes=1,
            content_type="text/plain",
        )
        assert len(store.events()) == before


# ---------------------------------------------------------------------
# Blob backends
# ---------------------------------------------------------------------


def _backends(tmp_path: Path) -> list[object]:
    return [InMemoryArtifactBackend(), FileArtifactBackend(tmp_path / "blobs")]


class TestArtifactBackend:
    def test_store_then_load_roundtrips(self, tmp_path: Path) -> None:
        payload = b"\x00\x01binary\xffbytes"
        for backend in _backends(tmp_path):
            backend.store(_HEX, payload)  # type: ignore[attr-defined]
            assert backend.load(_HEX) == payload  # type: ignore[attr-defined]

    def test_load_absent_raises_not_found(self, tmp_path: Path) -> None:
        for backend in _backends(tmp_path):
            with pytest.raises(NotFound):
                backend.load(_HEX)  # type: ignore[attr-defined]

    def test_store_is_exclusive_create(self, tmp_path: Path) -> None:
        # No-overwrite per 08-storage.md §5.4.
        for backend in _backends(tmp_path):
            backend.store(_HEX, b"first")  # type: ignore[attr-defined]
            with pytest.raises(FileExistsError):
                backend.store(_HEX, b"second")  # type: ignore[attr-defined]
            assert backend.load(_HEX) == b"first"  # type: ignore[attr-defined]

    def test_invalid_opaque_id_rejected(self, tmp_path: Path) -> None:
        for backend in _backends(tmp_path):
            with pytest.raises(ValueError, match="invalid opaque artifact id"):
                backend.store("../etc/passwd", b"x")  # type: ignore[attr-defined]
            with pytest.raises(ValueError, match="invalid opaque artifact id"):
                backend.load("not-hex")  # type: ignore[attr-defined]

    def test_file_backend_creates_root(self, tmp_path: Path) -> None:
        root = tmp_path / "nested" / "blobs"
        FileArtifactBackend(root)
        assert root.is_dir()

    def test_file_backend_rejects_symlink_leaf(self, tmp_path: Path) -> None:
        root = tmp_path / "blobs"
        backend = FileArtifactBackend(root)
        # Plant a symlink where the opaque-id file would resolve; O_NOFOLLOW
        # must refuse to follow it on load.
        target = tmp_path / "secret"
        target.write_bytes(b"secret")
        (root / _HEX).symlink_to(target)
        with pytest.raises((NotFound, OSError)):
            backend.load(_HEX)
