"""Manifest model behavior: validation, round-trip, and JSON Schema parity."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from eden_checkpoint import (
    CHECKPOINT_FORMAT_VERSION,
    CHECKPOINT_SPEC_VERSION,
    DEFAULT_FILES,
    CheckpointManifest,
    ExporterInfo,
    ManifestCounts,
)
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

SCHEMA_PATH = (
    Path(__file__).resolve().parents[4]
    / "spec"
    / "v0"
    / "schemas"
    / "checkpoint-manifest.schema.json"
)


def _schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _canonical_manifest_dict() -> dict[str, Any]:
    return {
        "checkpoint_format_version": CHECKPOINT_FORMAT_VERSION,
        "spec_version": CHECKPOINT_SPEC_VERSION,
        "experiment_id": "exp_0123456789abcdefghjkmnpqrs",
        "exported_at": "2026-05-06T15:00:00Z",
        "exporter": {
            "implementation": "eden-reference/0.x",
            "atomicity_mechanism": "transactional_snapshot",
        },
        "requires_credential_reissue": True,
        "counts": {
            "tasks": 42,
            "ideas": 12,
            "variants": 8,
            "submissions": 8,
            "events": 60,
            "workers": 4,
            "groups": 2,
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


def test_canonical_manifest_validates() -> None:
    manifest = CheckpointManifest.model_validate(_canonical_manifest_dict())
    assert manifest.experiment_id == "exp_0123456789abcdefghjkmnpqrs"
    assert manifest.counts.tasks == 42
    assert manifest.files.repo_bundle == "repo.bundle"


def test_manifest_model_dump_roundtrips_through_schema() -> None:
    """A round-trip through the model produces JSON the schema still accepts."""
    src = _canonical_manifest_dict()
    model = CheckpointManifest.model_validate(src)
    dumped = model.model_dump(mode="json", exclude_none=True)
    validator = Draft202012Validator(_schema(), format_checker=FormatChecker())
    validator.validate(dumped)


def test_manifest_accepts_minimal_exporter() -> None:
    """Exporter is optional; the manifest validates without it."""
    src = _canonical_manifest_dict()
    del src["exporter"]
    manifest = CheckpointManifest.model_validate(src)
    assert manifest.exporter is None


def test_manifest_rejects_missing_required_field() -> None:
    src = _canonical_manifest_dict()
    del src["exported_at"]
    with pytest.raises(ValidationError):
        CheckpointManifest.model_validate(src)


def test_manifest_rejects_negative_count() -> None:
    src = _canonical_manifest_dict()
    src["counts"]["tasks"] = -1
    with pytest.raises(ValidationError):
        CheckpointManifest.model_validate(src)


def test_manifest_rejects_malformed_timestamp() -> None:
    src = _canonical_manifest_dict()
    src["exported_at"] = "2026-05-06 15:00:00"  # no Z, wrong separator
    with pytest.raises(ValidationError):
        CheckpointManifest.model_validate(src)


def test_manifest_rejects_empty_format_version() -> None:
    src = _canonical_manifest_dict()
    src["checkpoint_format_version"] = ""
    with pytest.raises(ValidationError):
        CheckpointManifest.model_validate(src)


def test_default_files_matches_canonical_layout() -> None:
    """DEFAULT_FILES MUST match the spec/v0/10-checkpoints.md §3 layout."""
    expected = _canonical_manifest_dict()["files"]
    assert DEFAULT_FILES.model_dump() == expected


@pytest.mark.parametrize("field", ["implementation", "atomicity_mechanism"])
def test_exporter_info_fields_optional(field: str) -> None:
    """Either ExporterInfo field MAY be present or absent independently."""
    src = {"implementation": "x", "atomicity_mechanism": "y"}
    del src[field]
    info = ExporterInfo.model_validate(src)
    assert getattr(info, field) is None


def test_counts_must_be_present_and_complete() -> None:
    src = _canonical_manifest_dict()
    del src["counts"]["workers"]
    with pytest.raises(ValidationError):
        CheckpointManifest.model_validate(src)


def test_counts_model_accepts_zero() -> None:
    """Empty experiments produce zero-count manifests."""
    counts = ManifestCounts(
        tasks=0, ideas=0, variants=0, submissions=0, events=0, workers=0, groups=0
    )
    assert counts.tasks == 0


def test_schema_accepts_canonical_manifest_dict() -> None:
    """The JSON Schema accepts the same canonical dict the model does (sanity)."""
    validator = Draft202012Validator(_schema(), format_checker=FormatChecker())
    validator.validate(_canonical_manifest_dict())
