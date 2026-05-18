"""Content-addressed artifact dedup, hashing, and URI rewrites."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from eden_checkpoint import (
    ARTIFACT_URI_PREFIX,
    CHECKPOINT_FORMAT_VERSION,
    CHECKPOINT_SPEC_VERSION,
    DEFAULT_FILES,
    CheckpointInvalid,
    CheckpointManifest,
    CheckpointWriter,
    ManifestCounts,
    extract_checkpoint,
    is_valid_sha256_hex,
    sha256_hex,
)


def _minimal_manifest() -> CheckpointManifest:
    return CheckpointManifest.model_validate(
        {
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
    )


def _scaffold(w: CheckpointWriter) -> None:
    w.write_experiment_config("x")
    w.write_experiment(
        {"experiment_id": "exp-1", "state": "running", "created_at": "2026-01-01T00:00:00Z"}
    )
    for k in ("tasks", "ideas", "variants", "submissions", "events", "workers", "groups"):
        w.write_jsonl(k, [])
    w.write_repo_bundle(b"")


def test_artifact_returns_canonical_uri(tmp_path: Path) -> None:
    payload = b"hello world"
    expected_digest = sha256_hex(payload)
    stream = io.BytesIO()
    with CheckpointWriter(stream) as w:
        _scaffold(w)
        uri = w.write_artifact(payload)
        w.write_manifest(_minimal_manifest())
    assert uri == f"{ARTIFACT_URI_PREFIX}{expected_digest}"
    stream.seek(0)
    reader = extract_checkpoint(stream, tmp_path)
    assert reader.read_artifact(expected_digest) == payload


def test_artifact_dedup(tmp_path: Path) -> None:
    """Two writes of the same bytes MUST share a single archive entry."""
    payload = b"identical content"
    stream = io.BytesIO()
    with CheckpointWriter(stream) as w:
        _scaffold(w)
        uri_a = w.write_artifact(payload)
        uri_b = w.write_artifact(payload)
        w.write_manifest(_minimal_manifest())
    assert uri_a == uri_b
    stream.seek(0)
    reader = extract_checkpoint(stream, tmp_path)
    digests = list(reader.iter_artifact_digests())
    assert digests == [sha256_hex(payload)]


def test_different_payloads_produce_different_digests(tmp_path: Path) -> None:
    stream = io.BytesIO()
    with CheckpointWriter(stream) as w:
        _scaffold(w)
        uri_a = w.write_artifact(b"a")
        uri_b = w.write_artifact(b"b")
        w.write_manifest(_minimal_manifest())
    assert uri_a != uri_b
    stream.seek(0)
    reader = extract_checkpoint(stream, tmp_path)
    digests = set(reader.iter_artifact_digests())
    assert len(digests) == 2


def test_reader_rejects_invalid_digest(tmp_path: Path) -> None:
    stream = io.BytesIO()
    with CheckpointWriter(stream) as w:
        _scaffold(w)
        w.write_artifact(b"data")
        w.write_manifest(_minimal_manifest())
    stream.seek(0)
    reader = extract_checkpoint(stream, tmp_path)
    with pytest.raises(CheckpointInvalid, match="invalid artifact digest"):
        reader.read_artifact("not_a_real_digest")


def test_reader_rejects_missing_artifact(tmp_path: Path) -> None:
    stream = io.BytesIO()
    with CheckpointWriter(stream) as w:
        _scaffold(w)
        w.write_manifest(_minimal_manifest())  # no artifacts written
    stream.seek(0)
    reader = extract_checkpoint(stream, tmp_path)
    # A digest of the right shape but not present.
    fake_digest = sha256_hex(b"absent")
    with pytest.raises(CheckpointInvalid, match="missing artifact"):
        reader.read_artifact(fake_digest)


def test_is_valid_sha256_hex_grammar() -> None:
    assert is_valid_sha256_hex("a" * 64)
    assert is_valid_sha256_hex("0" * 64)
    assert not is_valid_sha256_hex("a" * 63)
    assert not is_valid_sha256_hex("A" * 64)  # uppercase rejected
    assert not is_valid_sha256_hex("g" * 64)  # non-hex char rejected


def test_artifact_uri_prefix_constant() -> None:
    """The URI prefix MUST match the spec/v0/10-checkpoints.md §7 form."""
    assert ARTIFACT_URI_PREFIX == "checkpoint:sha256:"


def test_iter_artifact_digests_is_sorted(tmp_path: Path) -> None:
    """Multi-artifact iteration MUST yield a stable order across runs."""
    payloads = [b"alpha", b"beta", b"gamma", b"delta"]
    stream = io.BytesIO()
    with CheckpointWriter(stream) as w:
        _scaffold(w)
        for p in payloads:
            w.write_artifact(p)
        w.write_manifest(_minimal_manifest())
    stream.seek(0)
    reader = extract_checkpoint(stream, tmp_path)
    digests = list(reader.iter_artifact_digests())
    assert digests == sorted(digests)
