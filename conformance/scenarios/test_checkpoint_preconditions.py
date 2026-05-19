"""Checkpoint precondition conformance scenarios — chapter 10 §§11-13.

Per chapter 9 §5 "Checkpoint preconditions": experiment-id collision
(409 ``eden://error/experiment-id-conflict``), spec-version /
format-version mismatches (409), cross-reference validation failures
(400 ``eden://error/checkpoint-invalid``). No state mutated on
rejection.
"""

from __future__ import annotations

import json
import tarfile
from io import BytesIO

import pytest
from conformance.harness import _seed
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = "Checkpoint preconditions"


def _hand_craft_archive(
    *,
    experiment_id: str,
    spec_version: str = "v0",
    checkpoint_format_version: str = "1",
    manifest_overrides: dict | None = None,
) -> bytes:
    """Build a minimal valid-shape archive with optional manifest overrides.

    Used for negative tests that need a manifest with specific
    spec_version / checkpoint_format_version values.
    """
    manifest = {
        "checkpoint_format_version": checkpoint_format_version,
        "spec_version": spec_version,
        "experiment_id": experiment_id,
        "exported_at": "2026-05-06T15:00:00Z",
        "requires_credential_reissue": True,
        "counts": {
            "tasks": 0, "ideas": 0, "variants": 0, "submissions": 0,
            "events": 0, "workers": 0, "groups": 0,
        },
        "files": {
            "experiment_config": "experiment-config.yaml",
            "experiment": "experiment.json",
            "tasks": "tasks.jsonl",
            "ideas": "ideas.jsonl",
            "variants": "variants.jsonl",
            "submissions": "submissions.jsonl",
            "events": "events.jsonl",
            "workers": "workers.jsonl",
            "groups": "groups.jsonl",
            "repo_bundle": "repo.bundle",
            "artifacts_dir": "artifacts/sha256",
        },
    }
    if manifest_overrides:
        manifest.update(manifest_overrides)

    buf = BytesIO()
    with tarfile.open(fileobj=buf, mode="w|") as tar:
        files = {
            "checkpoint/manifest.json": json.dumps(manifest, indent=2).encode("utf-8"),
            "checkpoint/experiment-config.yaml": b"x\n",
            "checkpoint/experiment.json": json.dumps({
                "experiment_id": experiment_id,
                "state": "running",
                "created_at": "2026-04-23T00:00:00Z",
            }).encode("utf-8"),
            "checkpoint/tasks.jsonl": b"",
            "checkpoint/ideas.jsonl": b"",
            "checkpoint/variants.jsonl": b"",
            "checkpoint/submissions.jsonl": b"",
            "checkpoint/events.jsonl": b"",
            "checkpoint/workers.jsonl": b"",
            "checkpoint/groups.jsonl": b"",
            "checkpoint/repo.bundle": b"",
        }
        for name, content in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            info.mode = 0o644
            tar.addfile(info, BytesIO(content))
    return buf.getvalue()


def test_spec_version_mismatch_returns_409(
    wire_client: WireClient,
) -> None:
    """spec/v0/10-checkpoints.md §13 — spec_version mismatch is rejected.

    Per chapter 10 §13 a consumer encountering a non-matching
    ``spec_version`` MUST reject the checkpoint with 409
    ``eden://error/spec-version-mismatch``.
    """
    archive = _hand_craft_archive(
        experiment_id=wire_client.experiment_id, spec_version="v99"
    )
    resp = _seed.import_checkpoint(wire_client, archive)
    assert resp.status_code == 409
    assert resp.json()["type"] == "eden://error/spec-version-mismatch"


def test_unsupported_format_version_returns_409(
    wire_client: WireClient,
) -> None:
    """spec/v0/10-checkpoints.md §13 — unsupported checkpoint_format_version is rejected.

    Per chapter 10 §13 a consumer encountering an unrecognized
    ``checkpoint_format_version`` MUST reject with 409
    ``eden://error/unsupported-checkpoint-version``.
    """
    archive = _hand_craft_archive(
        experiment_id=wire_client.experiment_id, checkpoint_format_version="999"
    )
    resp = _seed.import_checkpoint(wire_client, archive)
    assert resp.status_code == 409
    assert resp.json()["type"] == "eden://error/unsupported-checkpoint-version"


def test_corrupt_archive_returns_400(
    wire_client: WireClient,
) -> None:
    """spec/v0/10-checkpoints.md §12 — malformed archive is rejected before any commit.

    A non-tar body returns 400 ``eden://error/checkpoint-invalid``
    per chapter 10 §12; no state mutates on the receiver.
    """
    resp = _seed.import_checkpoint(wire_client, b"this is not a tar")
    assert resp.status_code == 400
    assert resp.json()["type"] == "eden://error/checkpoint-invalid"


def test_header_mismatch_returns_400(
    wire_client: WireClient,
) -> None:
    """spec/v0/07-wire-protocol.md §1.3 — header carve-out mismatch is rejected.

    The §1.3 carve-out makes ``X-Eden-Experiment-Id`` OPTIONAL on the
    ``/v0/checkpoints/import`` endpoint, but if present it MUST equal
    the post-rewrite experiment_id. A mismatch returns 400
    ``eden://error/experiment-id-mismatch``.
    """
    archive = _hand_craft_archive(experiment_id=wire_client.experiment_id)
    resp = _seed.import_checkpoint(
        wire_client,
        archive,
        omit_experiment_header=False,
        extra_headers={"X-Eden-Experiment-Id": "exp-wrong-id"},
    )
    assert resp.status_code == 400
    assert resp.json()["type"] == "eden://error/experiment-id-mismatch"
