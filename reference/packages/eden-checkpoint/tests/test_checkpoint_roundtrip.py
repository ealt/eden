"""End-to-end checkpoint round-trip: write a non-trivial archive, read it back, equivalence.

This is the wave-2 expression of the spec/v0/10-checkpoints.md §9
round-trip contract: every protocol-defined field of every preserved
object survives export + import. Wave 3 exercises this from the Store
layer; wave 2 exercises it from the format library directly.
"""

from __future__ import annotations

import io
from pathlib import Path

from eden_checkpoint import (
    ARTIFACT_URI_PREFIX,
    CHECKPOINT_FORMAT_VERSION,
    CHECKPOINT_SPEC_VERSION,
    DEFAULT_FILES,
    CheckpointManifest,
    CheckpointWriter,
    ManifestCounts,
    extract_checkpoint,
    sha256_hex,
)


def _build_payload() -> dict[str, list[dict[str, object]]]:
    """Build a non-trivial cross-product of every object kind."""
    return {
        "tasks": [
            {
                "task_id": "task-1",
                "kind": "ideation",
                "state": "completed",
                "payload": {"experiment_id": "exp-1"},
                "created_at": "2026-05-06T15:00:00Z",
                "updated_at": "2026-05-06T15:01:00Z",
                "created_by": "operator",
                "submitted_by": "ideator-1",
            },
            {
                "task_id": "task-2",
                "kind": "execution",
                "state": "pending",  # was claimed; reverted to pending on import per §9
                "payload": {"idea_id": "idea-1", "experiment_id": "exp-1"},
                "created_at": "2026-05-06T15:02:00Z",
                "updated_at": "2026-05-06T15:02:00Z",
                "created_by": "orchestrator-1",
                "target": {"kind": "worker", "id": "executor-2"},
            },
        ],
        "ideas": [
            {
                "idea_id": "idea-1",
                "experiment_id": "exp-1",
                "slug": "first-idea",
                "priority": 1.0,
                "parent_commits": ["a" * 40],
                "artifacts_uri": "checkpoint:sha256:" + sha256_hex(b"idea-1 content"),
                "state": "dispatched",
                "created_at": "2026-05-06T15:00:30Z",
                "created_by": "ideator-1",
            }
        ],
        "variants": [
            {
                "variant_id": "var-1",
                "experiment_id": "exp-1",
                "idea_id": "idea-1",
                "status": "success",
                "parent_commits": ["a" * 40],
                "branch": "work/first-idea-var-1",
                "commit_sha": "b" * 40,
                "variant_commit_sha": "c" * 40,
                "artifacts_uri": "checkpoint:sha256:" + sha256_hex(b"var-1 artifacts"),
                "description": "first variant",
                "metrics": {"accuracy": 0.95},
                "started_at": "2026-05-06T15:03:00Z",
                "completed_at": "2026-05-06T15:05:00Z",
                "executed_by": "executor-2",
                "evaluated_by": "evaluator-1",
            }
        ],
        "submissions": [
            {
                "task_id": "task-1",
                "status": "success",
                "idea_ids": ["idea-1"],
            },
            {
                "task_id": "task-2",
                "status": "success",
                "variant_id": "var-1",
                "commit_sha": "b" * 40,
            },
        ],
        "events": [
            {
                "event_id": "evt-1",
                "type": "task.created",
                "occurred_at": "2026-05-06T15:00:00Z",
                "experiment_id": "exp-1",
                "data": {"task_id": "task-1", "kind": "ideation"},
            },
            {
                "event_id": "evt-2",
                "type": "task.completed",
                "occurred_at": "2026-05-06T15:01:00Z",
                "experiment_id": "exp-1",
                "data": {"task_id": "task-1"},
            },
        ],
        "workers": [
            {
                "worker_id": "executor-2",
                "experiment_id": "exp-1",
                "registered_at": "2026-05-06T14:00:00Z",
                "registered_by": "admin",
                "labels": {"role": "executor"},
            }
        ],
        "groups": [
            {
                "group_id": "orchestrators",
                "experiment_id": "exp-1",
                "members": ["orchestrator-1"],
                "created_at": "2026-05-06T14:00:00Z",
                "created_by": "admin",
            }
        ],
    }


def _make_manifest(counts: dict[str, int]) -> CheckpointManifest:
    return CheckpointManifest.model_validate(
        {
            "checkpoint_format_version": CHECKPOINT_FORMAT_VERSION,
            "spec_version": CHECKPOINT_SPEC_VERSION,
            "experiment_id": "exp-1",
            "exported_at": "2026-05-06T15:00:00Z",
            "requires_credential_reissue": True,
            "exporter": {
                "implementation": "eden-checkpoint-tests/0",
                "atomicity_mechanism": "transactional_snapshot",
            },
            "counts": ManifestCounts(**counts).model_dump(),
            "files": DEFAULT_FILES.model_dump(),
        }
    )


def test_full_round_trip_preserves_structure(tmp_path: Path) -> None:
    payload = _build_payload()
    counts = {kind: len(rows) for kind, rows in payload.items()}

    stream = io.BytesIO()
    with CheckpointWriter(stream) as w:
        w.write_experiment_config(
            "parallel_variants: 1\nobjective:\n  expr: accuracy\n  direction: maximize\n"
        )
        w.write_experiment(
            {"experiment_id": "exp-1", "state": "terminated", "created_at": "2026-05-06T14:00:00Z"}
        )
        for kind in ("tasks", "ideas", "variants", "submissions", "events", "workers", "groups"):
            w.write_jsonl(kind, payload[kind])
        # Two artifacts (dedup later)
        w.write_artifact(b"idea-1 content")
        w.write_artifact(b"var-1 artifacts")
        # One duplicate to verify dedup.
        w.write_artifact(b"idea-1 content")
        w.write_repo_bundle(b"PLACEHOLDER bundle bytes")
        w.write_manifest(_make_manifest(counts))

    stream.seek(0)
    reader = extract_checkpoint(stream, tmp_path)

    # Manifest survived.
    assert reader.manifest.experiment_id == "exp-1"
    assert reader.manifest.exporter is not None
    assert reader.manifest.exporter.implementation == "eden-checkpoint-tests/0"

    # Every JSONL kind round-trips byte-for-byte at the row level.
    for kind, rows in payload.items():
        assert list(reader.iter_jsonl(kind)) == rows, f"{kind} divergence"

    # Artifacts dedup'd to two entries.
    digests = list(reader.iter_artifact_digests())
    assert len(digests) == 2
    assert reader.read_artifact(sha256_hex(b"idea-1 content")) == b"idea-1 content"
    assert reader.read_artifact(sha256_hex(b"var-1 artifacts")) == b"var-1 artifacts"

    # Bundle bytes survived.
    assert reader.read_repo_bundle_path().read_bytes() == b"PLACEHOLDER bundle bytes"

    # Experiment runtime object survived.
    exp = reader.read_experiment()
    assert exp["state"] == "terminated"
    assert exp["created_at"] == "2026-05-06T14:00:00Z"


def test_artifact_uri_in_payload_matches_archive_digest(tmp_path: Path) -> None:
    """An artifacts_uri value in JSONL data must match the archive's content-addressed file."""
    content = b"the artifact bytes"
    expected_uri = ARTIFACT_URI_PREFIX + sha256_hex(content)

    payload_idea = {
        "idea_id": "idea-1",
        "experiment_id": "exp-1",
        "slug": "x",
        "priority": 0.0,
        "parent_commits": ["a" * 40],
        "artifacts_uri": expected_uri,
        "state": "drafting",
        "created_at": "2026-05-06T15:00:00Z",
    }

    stream = io.BytesIO()
    with CheckpointWriter(stream) as w:
        w.write_experiment_config("x")
        w.write_experiment(
            {"experiment_id": "exp-1", "state": "running", "created_at": "2026-01-01T00:00:00Z"}
        )
        for k in ("tasks", "ideas", "variants", "submissions", "events", "workers", "groups"):
            if k == "ideas":
                w.write_jsonl(k, [payload_idea])
            else:
                w.write_jsonl(k, [])
        uri = w.write_artifact(content)
        assert uri == expected_uri
        w.write_repo_bundle(b"")
        w.write_manifest(
            _make_manifest({
                "tasks": 0, "ideas": 1, "variants": 0, "submissions": 0,
                "events": 0, "workers": 0, "groups": 0,
            })
        )
    stream.seek(0)
    reader = extract_checkpoint(stream, tmp_path)
    idea = next(reader.iter_jsonl("ideas"))
    digest = idea["artifacts_uri"].removeprefix(ARTIFACT_URI_PREFIX)
    assert reader.read_artifact(digest) == content
