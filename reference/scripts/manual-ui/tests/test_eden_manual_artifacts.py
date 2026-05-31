"""CLI write-side artifact tests for the standalone ``eden-manual`` script.

The CLI ships as a system-``python3`` script and can't import any workspace
package, so it hand-mirrors ``eden_service_common.artifacts``. Issue #168 made
that mirror hierarchical; the two traps these tests lock in:

1. **Write-side URI stamping** must use the FULL nested relative path under the
   container artifacts dir, not the basename — the §D.4 trap. The read-side
   ``_translate_artifacts_uri_to_host`` was already nested-safe; the stamp was
   not.
2. The CLI's relative path must match what the shared
   ``entity_artifact_dir`` + ``ArtifactNaming`` policy produces, so the layout
   stays identical across the importable helper and the hand-mirror.
"""

from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest
from eden_service_common.artifacts import (
    entity_artifact_dir,
    idea_naming,
    submission_naming,
)

_SCRIPT = Path(__file__).resolve().parents[1] / "eden-manual"


def _load_cli():
    # The CLI ships without a .py extension, so spec_from_file_location can't
    # infer a loader — supply a SourceFileLoader explicitly.
    loader = SourceFileLoader("eden_manual_cli", str(_SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def cli():
    return _load_cli()


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return {"EDEN_EXPERIMENT_DATA_ROOT": str(tmp_path)}


def test_ideation_text_only_stamps_full_nested_uri(cli, env, tmp_path) -> None:
    uri = cli._write_artifact_for_role(
        env,
        relative_dir="ideas/idea-abc",
        text_only_name="content.md",
        single_stem=None,
        bundle_name="bundle.tar.gz",
        headline="content.md",
        text_content="# my idea",
        file_paths=[],
    )
    # §D.4: full nested relative path, NOT the basename-only stamp.
    assert uri == "file:///var/lib/eden/artifacts/ideas/idea-abc/content.md"
    # Bytes landed on the host bind-mount side under the same rel path.
    assert (
        tmp_path / "artifacts" / "ideas" / "idea-abc" / "content.md"
    ).read_text() == "# my idea"


def test_ideation_single_upload_keeps_original_name(cli, env, tmp_path) -> None:
    src = tmp_path / "design.pdf"
    src.write_bytes(b"%PDF")
    uri = cli._write_artifact_for_role(
        env,
        relative_dir="ideas/idea-abc",
        text_only_name="content.md",
        single_stem=None,
        bundle_name="bundle.tar.gz",
        headline="content.md",
        text_content=None,
        file_paths=[src],
    )
    assert uri == "file:///var/lib/eden/artifacts/ideas/idea-abc/design.pdf"


def test_evaluation_uses_stem_under_variant_evaluator(cli, env) -> None:
    uri = cli._write_artifact_for_role(
        env,
        relative_dir="variants/var-1/evaluator",
        text_only_name="eval-xyz.md",
        single_stem="eval-xyz",
        bundle_name="eval-xyz.tar.gz",
        headline="evaluation.md",
        text_content="score 9",
        file_paths=[],
    )
    assert uri == (
        "file:///var/lib/eden/artifacts/variants/var-1/evaluator/eval-xyz.md"
    )


def test_cli_relative_dir_matches_shared_helper(cli, env, tmp_path) -> None:
    """The CLI's stamped path must match entity_artifact_dir + ArtifactNaming."""
    # Idea text-only.
    cli_uri = cli._write_artifact_for_role(
        env,
        relative_dir="ideas/idea-1",
        text_only_name="content.md",
        single_stem=None,
        bundle_name="bundle.tar.gz",
        headline="content.md",
        text_content="x",
        file_paths=[],
    )
    base = Path("/var/lib/eden/artifacts")
    helper_dir = entity_artifact_dir(
        base, producer="ideator", entity_id="idea-1"
    )
    # entity_artifact_dir resolves its base (e.g. /var → /private/var on
    # macOS), so compare the layout sub-path relative to the resolved base
    # rather than the literal container path the CLI stamps.
    rel = helper_dir.relative_to(base.resolve())
    assert rel.as_posix() == "ideas/idea-1"
    assert idea_naming().text_only_name == "content.md"
    assert cli_uri == (
        "file:///var/lib/eden/artifacts/ideas/idea-1/content.md"
    )


def test_nested_uri_round_trips_through_host_translate(cli, env, tmp_path) -> None:
    """The §D.4 read-side: a nested URI translates back to the host path."""
    uri = "file:///var/lib/eden/artifacts/ideas/idea-9/content.md"
    host = cli._translate_artifacts_uri_to_host(env, uri)
    assert host == (
        tmp_path / "artifacts" / "ideas" / "idea-9" / "content.md"
    )


def test_submission_naming_parity(cli) -> None:
    """Sanity: the shared submission_naming stem policy the CLI mirrors."""
    naming = submission_naming("eval-xyz", headline="evaluation.md")
    assert naming.text_only_name == "eval-xyz.md"
    assert naming.bundle_name == "eval-xyz.tar.gz"
