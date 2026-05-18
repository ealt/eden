"""Tar envelope: writer produces a parseable archive, reader extracts safely."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest
from eden_checkpoint import (
    CHECKPOINT_FORMAT_VERSION,
    CHECKPOINT_SPEC_VERSION,
    DEFAULT_FILES,
    CheckpointInvalid,
    CheckpointManifest,
    CheckpointReader,
    CheckpointWriter,
    ManifestCounts,
    UnsupportedCheckpointVersion,
    extract_checkpoint,
)


def _minimal_manifest(**overrides: object) -> CheckpointManifest:
    data: dict[str, object] = {
        "checkpoint_format_version": CHECKPOINT_FORMAT_VERSION,
        "spec_version": CHECKPOINT_SPEC_VERSION,
        "experiment_id": "exp-1",
        "exported_at": "2026-05-06T15:00:00Z",
        "requires_credential_reissue": True,
        "counts": ManifestCounts(
            tasks=0, ideas=0, variants=0, submissions=0, events=0, workers=0, groups=0
        ).model_dump(),
        "files": DEFAULT_FILES.model_dump(),
    }
    data.update(overrides)
    return CheckpointManifest.model_validate(data)


def _write_minimal_checkpoint(stream: io.BytesIO) -> None:
    with CheckpointWriter(stream) as w:
        w.write_experiment_config("parallel_variants: 1\n")
        w.write_experiment(
            {"experiment_id": "exp-1", "state": "running", "created_at": "2026-01-01T00:00:00Z"}
        )
        w.write_jsonl("tasks", [])
        w.write_jsonl("ideas", [])
        w.write_jsonl("variants", [])
        w.write_jsonl("submissions", [])
        w.write_jsonl("events", [])
        w.write_jsonl("workers", [])
        w.write_jsonl("groups", [])
        w.write_repo_bundle(b"fake bundle bytes")
        w.write_manifest(_minimal_manifest())


def test_writer_produces_parseable_tar(tmp_path: Path) -> None:
    stream = io.BytesIO()
    _write_minimal_checkpoint(stream)
    stream.seek(0)

    with tarfile.open(fileobj=stream, mode="r|") as tar:
        names = [m.name for m in tar.getmembers()]

    # Every entry MUST live under a single top-level directory.
    roots = {n.split("/", 1)[0] for n in names}
    assert len(roots) == 1
    assert "checkpoint/manifest.json" in names
    assert "checkpoint/experiment.json" in names
    assert "checkpoint/repo.bundle" in names


def test_reader_round_trip(tmp_path: Path) -> None:
    stream = io.BytesIO()
    _write_minimal_checkpoint(stream)
    stream.seek(0)

    reader = extract_checkpoint(stream, tmp_path)
    assert reader.manifest.experiment_id == "exp-1"
    assert reader.manifest.checkpoint_format_version == CHECKPOINT_FORMAT_VERSION
    assert list(reader.iter_jsonl("tasks")) == []
    assert reader.read_repo_bundle_path().read_bytes() == b"fake bundle bytes"
    assert reader.read_experiment()["state"] == "running"


def test_reader_rejects_missing_manifest(tmp_path: Path) -> None:
    # Build a tar with only a stray file, no manifest.
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w|") as tar:
        data = b"not a manifest"
        info = tarfile.TarInfo(name="checkpoint/somefile.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    stream.seek(0)
    with pytest.raises(CheckpointInvalid, match="missing manifest"):
        extract_checkpoint(stream, tmp_path)


def test_reader_rejects_wrong_format_version(tmp_path: Path) -> None:
    stream = io.BytesIO()
    with CheckpointWriter(stream) as w:
        w.write_experiment_config("x")
        w.write_experiment(
            {"experiment_id": "exp", "state": "running", "created_at": "2026-01-01T00:00:00Z"}
        )
        for k in ("tasks", "ideas", "variants", "submissions", "events", "workers", "groups"):
            w.write_jsonl(k, [])
        w.write_repo_bundle(b"")
        w.write_manifest(_minimal_manifest(checkpoint_format_version="999"))
    stream.seek(0)
    with pytest.raises(UnsupportedCheckpointVersion):
        extract_checkpoint(stream, tmp_path)


def test_reader_rejects_multi_root_tar(tmp_path: Path) -> None:
    """An archive MUST have exactly one top-level directory."""
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w|") as tar:
        for name in ("a/file.txt", "b/file.txt"):
            data = b"x"
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    stream.seek(0)
    with pytest.raises(CheckpointInvalid, match="single top-level"):
        extract_checkpoint(stream, tmp_path)


def test_writer_rejects_duplicate_entries() -> None:
    stream = io.BytesIO()
    with CheckpointWriter(stream) as w:
        w.write_text("x.txt", "hello")
        with pytest.raises(RuntimeError, match="duplicate"):
            w.write_text("x.txt", "world")


def test_writer_close_is_idempotent() -> None:
    stream = io.BytesIO()
    w = CheckpointWriter(stream)
    w.close()
    w.close()  # second call must not raise


def test_writer_blocks_writes_after_close() -> None:
    stream = io.BytesIO()
    w = CheckpointWriter(stream)
    w.close()
    with pytest.raises(RuntimeError, match="closed"):
        w.write_text("x.txt", "hello")


def test_extract_uses_data_filter_path_traversal(tmp_path: Path) -> None:
    """Tar extraction MUST reject path-traversal entries (data filter behavior)."""
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w|") as tar:
        evil = b"x"
        info = tarfile.TarInfo(name="../escape.txt")
        info.size = len(evil)
        tar.addfile(info, io.BytesIO(evil))
    stream.seek(0)
    with pytest.raises(CheckpointInvalid):
        extract_checkpoint(stream, tmp_path)


def test_reader_validates_already_extracted_directory(tmp_path: Path) -> None:
    """Reader can be constructed directly from an extracted dir (for the wave-3 Store flow)."""
    # Materialize, extract, and re-construct the reader.
    stream = io.BytesIO()
    _write_minimal_checkpoint(stream)
    stream.seek(0)
    reader = extract_checkpoint(stream, tmp_path)
    # Build a second reader from the same root.
    reader2 = CheckpointReader(reader.root)
    assert reader2.manifest.experiment_id == reader.manifest.experiment_id
