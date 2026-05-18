"""JSONL writer / reader: order preserved, malformed input rejected."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from eden_checkpoint import (
    CHECKPOINT_FORMAT_VERSION,
    CHECKPOINT_SPEC_VERSION,
    DEFAULT_FILES,
    CheckpointInvalid,
    CheckpointManifest,
    CheckpointWriter,
    ManifestCounts,
    extract_checkpoint,
)


def _manifest_with_counts(**counts: int) -> CheckpointManifest:
    full = {
        "tasks": 0, "ideas": 0, "variants": 0, "submissions": 0,
        "events": 0, "workers": 0, "groups": 0,
    }
    full.update(counts)
    return CheckpointManifest.model_validate(
        {
            "checkpoint_format_version": CHECKPOINT_FORMAT_VERSION,
            "spec_version": CHECKPOINT_SPEC_VERSION,
            "experiment_id": "exp-1",
            "exported_at": "2026-05-06T15:00:00Z",
            "requires_credential_reissue": True,
            "counts": ManifestCounts(**full).model_dump(),
            "files": DEFAULT_FILES.model_dump(),
        }
    )


def _write_with_rows(stream: io.BytesIO, kind: str, rows: list[dict]) -> None:
    with CheckpointWriter(stream) as w:
        w.write_experiment_config("x")
        w.write_experiment(
            {"experiment_id": "exp-1", "state": "running", "created_at": "2026-01-01T00:00:00Z"}
        )
        for k in ("tasks", "ideas", "variants", "submissions", "events", "workers", "groups"):
            if k == kind:
                w.write_jsonl(k, rows)
            else:
                w.write_jsonl(k, [])
        w.write_repo_bundle(b"")
        w.write_manifest(_manifest_with_counts(**{kind: len(rows)}))


def test_row_order_preserved(tmp_path: Path) -> None:
    rows = [{"task_id": f"t-{i}", "i": i} for i in range(50)]
    stream = io.BytesIO()
    _write_with_rows(stream, "tasks", rows)
    stream.seek(0)
    reader = extract_checkpoint(stream, tmp_path)
    read_back = list(reader.iter_jsonl("tasks"))
    assert read_back == rows


def test_row_content_round_trip_with_unicode(tmp_path: Path) -> None:
    rows = [{"slug": "test", "description": "café — résumé — 日本語"}]
    stream = io.BytesIO()
    _write_with_rows(stream, "ideas", rows)
    stream.seek(0)
    reader = extract_checkpoint(stream, tmp_path)
    assert list(reader.iter_jsonl("ideas"))[0]["description"] == "café — résumé — 日本語"


def test_empty_jsonl_file_round_trips(tmp_path: Path) -> None:
    stream = io.BytesIO()
    _write_with_rows(stream, "tasks", [])
    stream.seek(0)
    reader = extract_checkpoint(stream, tmp_path)
    assert list(reader.iter_jsonl("tasks")) == []


def test_writer_counts_per_kind(tmp_path: Path) -> None:
    stream = io.BytesIO()
    with CheckpointWriter(stream) as w:
        n = w.write_jsonl("tasks", [{"task_id": "t-1"}, {"task_id": "t-2"}, {"task_id": "t-3"}])
    assert n == 3


def test_unknown_jsonl_kind_rejected() -> None:
    stream = io.BytesIO()
    with CheckpointWriter(stream) as w, pytest.raises(ValueError, match="unknown JSONL kind"):
        w.write_jsonl("not_a_kind", [])


def test_reader_rejects_blank_line(tmp_path: Path) -> None:
    """The format MUST NOT contain blank lines in JSONL files."""
    stream = io.BytesIO()
    # Hand-craft a JSONL with a blank line by writing raw bytes.
    import tarfile
    with tarfile.open(fileobj=stream, mode="w|") as tar:
        manifest_bytes = _manifest_with_counts().model_dump_json(indent=2).encode("utf-8")
        info = tarfile.TarInfo(name="checkpoint/manifest.json")
        info.size = len(manifest_bytes)
        tar.addfile(info, io.BytesIO(manifest_bytes))
        # Two valid rows separated by a blank line.
        bad_jsonl = b'{"task_id":"t1"}\n\n{"task_id":"t2"}\n'
        info = tarfile.TarInfo(name="checkpoint/tasks.jsonl")
        info.size = len(bad_jsonl)
        tar.addfile(info, io.BytesIO(bad_jsonl))
    stream.seek(0)
    reader = extract_checkpoint(stream, tmp_path)
    with pytest.raises(CheckpointInvalid, match="empty line"):
        list(reader.iter_jsonl("tasks"))


def test_reader_rejects_malformed_json(tmp_path: Path) -> None:
    stream = io.BytesIO()
    import tarfile
    with tarfile.open(fileobj=stream, mode="w|") as tar:
        manifest_bytes = _manifest_with_counts().model_dump_json(indent=2).encode("utf-8")
        info = tarfile.TarInfo(name="checkpoint/manifest.json")
        info.size = len(manifest_bytes)
        tar.addfile(info, io.BytesIO(manifest_bytes))
        bad = b"not json at all\n"
        info = tarfile.TarInfo(name="checkpoint/tasks.jsonl")
        info.size = len(bad)
        tar.addfile(info, io.BytesIO(bad))
    stream.seek(0)
    reader = extract_checkpoint(stream, tmp_path)
    with pytest.raises(CheckpointInvalid, match="malformed JSON"):
        list(reader.iter_jsonl("tasks"))


def test_reader_rejects_missing_jsonl_file(tmp_path: Path) -> None:
    """A manifest that promises a JSONL file but ships none MUST be rejected."""
    stream = io.BytesIO()
    import tarfile
    with tarfile.open(fileobj=stream, mode="w|") as tar:
        manifest_bytes = _manifest_with_counts().model_dump_json(indent=2).encode("utf-8")
        info = tarfile.TarInfo(name="checkpoint/manifest.json")
        info.size = len(manifest_bytes)
        tar.addfile(info, io.BytesIO(manifest_bytes))
    stream.seek(0)
    reader = extract_checkpoint(stream, tmp_path)
    with pytest.raises(CheckpointInvalid, match="missing JSONL"):
        list(reader.iter_jsonl("tasks"))
