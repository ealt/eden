"""Eval-manifest builder tests (spec/v0/06-integrator.md §4.2)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from eden_contracts import Variant
from eden_git._manifest import ManifestFieldMissing, build_manifest


def _base_variant(**overrides: Any) -> Variant:
    defaults: dict[str, Any] = {
        "variant_id": "variant-1",
        "experiment_id": "exp_0123456789abcdefghjkmnpqrs",
        "idea_id": "idea-1",
        "status": "success",
        "parent_commits": ["a" * 40],
        "branch": "work/variant-1",
        "commit_sha": "b" * 40,
        "evaluation": {"score": 42, "latency": 1.5},
        "started_at": "2026-04-23T00:00:00Z",
        "completed_at": "2026-04-23T00:05:00Z",
    }
    # Optional fields: omit by default (Pydantic rejects explicit null); allow overrides.
    for k, v in list(overrides.items()):
        if v is None and k in {
            "commit_sha",
            "evaluation",
            "completed_at",
            "artifacts_uri",
            "description",
            "branch",
        }:
            defaults.pop(k, None)
            overrides.pop(k)
    defaults.update(overrides)
    return Variant(**defaults)


class TestRequiredFields:
    def test_all_required_fields_copied_verbatim(self) -> None:
        variant = _base_variant()
        manifest = json.loads(build_manifest(variant).decode("utf-8"))
        assert manifest == {
            "variant_id": "variant-1",
            "idea_id": "idea-1",
            "commit_sha": "b" * 40,
            "parent_commits": ["a" * 40],
            "evaluation": {"score": 42, "latency": 1.5},
            "completed_at": "2026-04-23T00:05:00Z",
        }

    def test_multi_parent_commits_preserved_in_order(self) -> None:
        parents = ["a" * 40, "b" * 40, "c" * 40]
        variant = _base_variant(parent_commits=parents)
        manifest = json.loads(build_manifest(variant).decode("utf-8"))
        assert manifest["parent_commits"] == parents


class TestOptionalFields:
    def test_artifacts_uri_emitted_when_present(self) -> None:
        variant = _base_variant(artifacts_uri="https://eval.example/t1/")
        manifest = json.loads(build_manifest(variant).decode("utf-8"))
        assert manifest["artifacts_uri"] == "https://eval.example/t1/"

    def test_description_emitted_when_present(self) -> None:
        variant = _base_variant(description="baseline run")
        manifest = json.loads(build_manifest(variant).decode("utf-8"))
        assert manifest["description"] == "baseline run"

    def test_optional_fields_absent_when_none(self) -> None:
        variant = _base_variant()
        manifest = json.loads(build_manifest(variant).decode("utf-8"))
        assert "artifacts_uri" not in manifest
        assert "description" not in manifest


class TestByteStability:
    """Load-bearing: the integrator's §5.3 idempotency check re-builds
    the manifest and compares bytes."""

    def test_repeated_calls_produce_identical_bytes(self) -> None:
        variant = _base_variant(description="a", artifacts_uri="https://x.example/")
        a = build_manifest(variant)
        b = build_manifest(variant)
        assert a == b

    def test_keys_sorted_regardless_of_variant_insertion_order(self) -> None:
        variant = _base_variant(evaluation={"z": 1, "a": 2, "m": 3})
        serialized = build_manifest(variant).decode("utf-8")
        assert serialized.index("\"a\": 2") < serialized.index("\"m\": 3")
        assert serialized.index("\"m\": 3") < serialized.index("\"z\": 1")

    def test_trailing_newline_present(self) -> None:
        variant = _base_variant()
        assert build_manifest(variant).endswith(b"\n")


class TestMissingRequiredFields:
    def test_missing_commit_sha_raises(self) -> None:
        variant = _base_variant(commit_sha=None, status="starting")
        with pytest.raises(ManifestFieldMissing):
            build_manifest(variant)

    def test_missing_evaluation_raises(self) -> None:
        variant = _base_variant(evaluation=None, status="starting")
        with pytest.raises(ManifestFieldMissing):
            build_manifest(variant)

    def test_missing_completed_at_raises(self) -> None:
        variant = _base_variant(completed_at=None, status="starting")
        with pytest.raises(ManifestFieldMissing):
            build_manifest(variant)
