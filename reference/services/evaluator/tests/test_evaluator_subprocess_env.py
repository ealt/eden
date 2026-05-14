"""Tests for the 12a-1f substrate-access env-var threading in evaluator CLI.

Covers the parallel of test_ideator_subprocess_env.py:
``add_substrate_arguments`` registration + env composition + DooD
suppression for the evaluator host.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest
from eden_evaluator_host.cli import parse_args
from eden_service_common import (
    resolve_substrate_args,
    substrate_args_for_exec_mode,
)


@pytest.fixture(autouse=True)
def _clear_substrate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "EDEN_ARTIFACT_URL",
        "EDEN_ARTIFACT_PATH_ROOT",
        "EDEN_READONLY_STORE_URL",
    ):
        monkeypatch.delenv(var, raising=False)


def test_substrate_flags_parsed(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "parallel_variants: 1\nmax_variants: 1\nmax_wall_time: 1h\n"
        "evaluation_schema:\n  score: real\n"
        "objective:\n  expr: score\n  direction: maximize\n"
        "evaluation_command: 'true'\n"
    )
    args = parse_args(
        [
            "--task-store-url",
            "http://store",
            "--experiment-id",
            "exp",
            "--worker-id",
            "evaluator-1",
            "--experiment-config",
            str(cfg),
            "--mode",
            "subprocess",
            "--experiment-dir",
            str(tmp_path),
            "--repo-path",
            str(tmp_path / "repo"),
            "--artifact-url",
            "http://server/artifacts/",
            "--artifact-path-root",
            "/var/lib/eden/artifacts",
            "--readonly-store-url",
            "postgresql://ro@db/eden",
        ]
    )
    assert args.artifact_url == "http://server/artifacts/"
    assert args.artifact_path_root == "/var/lib/eden/artifacts"
    assert args.readonly_store_url == "postgresql://ro@db/eden"


def _ns(**kwargs) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


def test_host_mode_threads_substrate_keys() -> None:
    args = _ns(
        repo_path="/var/lib/eden/repo",
        artifact_url="http://srv/artifacts/",
        artifact_path_root="/var/artifacts",
        readonly_store_url="postgresql://ro@db",
    )
    substrate = resolve_substrate_args(args, repo_dir=args.repo_path)
    substrate = substrate_args_for_exec_mode(substrate, exec_mode="host")
    assert substrate.to_env() == {
        "EDEN_REPO_DIR": "/var/lib/eden/repo",
        "EDEN_ARTIFACT_URL": "http://srv/artifacts/",
        "EDEN_ARTIFACT_PATH_ROOT": "/var/artifacts",
        "EDEN_READONLY_STORE_URL": "postgresql://ro@db",
    }


def test_docker_mode_suppresses_substrate_keys() -> None:
    args = _ns(
        repo_path="/var/lib/eden/repo",
        artifact_url="http://srv/artifacts/",
        artifact_path_root="/var/artifacts",
        readonly_store_url="postgresql://ro@db",
    )
    substrate = resolve_substrate_args(args, repo_dir=args.repo_path)
    substrate = substrate_args_for_exec_mode(substrate, exec_mode="docker")
    assert substrate.to_env() == {}
