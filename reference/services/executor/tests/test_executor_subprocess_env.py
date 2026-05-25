"""Tests for issue #154 substrate-access env-var threading in executor CLI.

Mirrors test_evaluator_subprocess_env.py / test_ideator_subprocess_env.py:
``add_substrate_arguments`` registration + env composition + DooD
suppression (and DooD-with-``--exec-network`` un-suppression per
issue #155) for the executor host.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest
from eden_executor_host.cli import parse_args
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
        "EDEN_EXEC_NETWORK",
    ):
        monkeypatch.delenv(var, raising=False)


def _common(*extra: str) -> list[str]:
    return [
        "--task-store-url",
        "http://store",
        "--experiment-id",
        "exp",
        "--worker-id",
        "executor-1",
        "--repo-path",
        "/var/lib/eden/repo",
        *extra,
    ]


def test_substrate_flags_parsed(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "parallel_variants: 1\n"
        "evaluation_schema:\n  score: real\n"
        "objective:\n  expr: score\n  direction: maximize\n"
        "execution_command: 'true'\n"
    )
    args = parse_args(
        _common(
            "--mode",
            "subprocess",
            "--experiment-config",
            str(cfg),
            "--experiment-dir",
            str(tmp_path),
            "--artifact-url",
            "http://server/artifacts/",
            "--artifact-path-root",
            "/var/lib/eden/artifacts",
            "--readonly-store-url",
            "postgresql://ro@db/eden",
        )
    )
    assert args.artifact_url == "http://server/artifacts/"
    assert args.artifact_path_root == "/var/lib/eden/artifacts"
    assert args.readonly_store_url == "postgresql://ro@db/eden"


def test_scripted_mode_substrate_flags_optional() -> None:
    """The executor CLI accepts (and ignores) substrate flags in scripted
    mode — they're forwarded only in subprocess mode. Mirrors ideator."""
    args = parse_args(_common("--mode", "scripted"))
    assert args.artifact_url is None
    assert args.artifact_path_root is None
    assert args.readonly_store_url is None


def _ns(**kwargs) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


def test_host_mode_threads_all_four_keys() -> None:
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


def test_docker_mode_no_network_suppresses_keys() -> None:
    """Docker mode without ``--exec-network`` drops all four keys."""
    args = _ns(
        repo_path="/var/lib/eden/repo",
        artifact_url="http://srv/artifacts/",
        artifact_path_root="/var/artifacts",
        readonly_store_url="postgresql://ro@db",
    )
    substrate = resolve_substrate_args(args, repo_dir=args.repo_path)
    substrate = substrate_args_for_exec_mode(substrate, exec_mode="docker")
    assert substrate.to_env() == {}


def test_docker_mode_with_network_forwards_keys() -> None:
    """Issue #155: ``--exec-network`` un-suppresses substrate keys."""
    args = _ns(
        repo_path="/var/lib/eden/repo",
        artifact_url="http://srv/artifacts/",
        artifact_path_root="/var/artifacts",
        readonly_store_url="postgresql://ro@db",
    )
    substrate = resolve_substrate_args(args, repo_dir=args.repo_path)
    substrate = substrate_args_for_exec_mode(
        substrate, exec_mode="docker", exec_network="eden-reference_default"
    )
    assert substrate.to_env() == {
        "EDEN_REPO_DIR": "/var/lib/eden/repo",
        "EDEN_ARTIFACT_URL": "http://srv/artifacts/",
        "EDEN_ARTIFACT_PATH_ROOT": "/var/artifacts",
        "EDEN_READONLY_STORE_URL": "postgresql://ro@db",
    }
