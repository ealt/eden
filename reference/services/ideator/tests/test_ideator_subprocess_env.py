"""Tests for the 12a-1f substrate-access env-var threading in ideator CLI.

Covers ``parse_args`` + ``main()``'s env-overlay logic via a focused
test that exercises just the CLI-resolution path (not a full
subprocess spawn). The end-to-end smoke verifies the env actually
lands in the child.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest
from eden_ideator_host.cli import parse_args
from eden_service_common import (
    resolve_substrate_args,
    substrate_args_for_exec_mode,
)


@pytest.fixture(autouse=True)
def _clear_substrate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip substrate env vars so test parameter parsing is hermetic."""
    for var in (
        "EDEN_ARTIFACT_URL",
        "EDEN_ARTIFACT_PATH_ROOT",
        "EDEN_READONLY_STORE_URL",
    ):
        monkeypatch.delenv(var, raising=False)


def _common_args(*extra: str) -> list[str]:
    return [
        "--task-store-url",
        "http://store",
        "--experiment-id",
        "exp",
        "--worker-id",
        "ideator-1",
        *extra,
    ]


# ----------------------------------------------------------------------
# CLI parsing
# ----------------------------------------------------------------------


def test_scripted_mode_substrate_flags_optional() -> None:
    args = parse_args(
        _common_args("--mode", "scripted", "--base-commit-sha", "a" * 40)
    )
    assert args.mode == "scripted"
    assert args.artifact_url is None
    assert args.artifact_path_root is None
    assert args.readonly_store_url is None
    assert args.repo_path is None
    assert args.gitea_url is None
    assert args.credential_helper is None


def test_subprocess_mode_substrate_flags_parsed(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("parallel_variants: 1\n"
                   "evaluation_schema:\n  score: real\n"
                   "objective:\n  expr: score\n  direction: maximize\n"
                   "ideation_command: 'true'\n")
    args = parse_args(
        _common_args(
            "--mode",
            "subprocess",
            "--experiment-config",
            str(cfg),
            "--experiment-dir",
            str(tmp_path),
            "--artifacts-dir",
            str(tmp_path / "art"),
            "--repo-path",
            "/var/lib/eden/repo",
            "--gitea-url",
            "http://gitea/eden/exp.git",
            "--credential-helper",
            "/etc/eden/helper.sh",
            "--artifact-url",
            "http://server/artifacts/",
            "--artifact-path-root",
            "/var/lib/eden/artifacts",
            "--readonly-store-url",
            "postgresql://ro@db/eden",
        )
    )
    assert args.mode == "subprocess"
    assert args.repo_path == "/var/lib/eden/repo"
    assert args.gitea_url == "http://gitea/eden/exp.git"
    assert args.credential_helper == "/etc/eden/helper.sh"
    assert args.artifact_url == "http://server/artifacts/"
    assert args.artifact_path_root == "/var/lib/eden/artifacts"
    assert args.readonly_store_url == "postgresql://ro@db/eden"


# ----------------------------------------------------------------------
# Substrate-env composition + DooD suppression
# ----------------------------------------------------------------------


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
    env = substrate.to_env()
    assert env == {
        "EDEN_REPO_DIR": "/var/lib/eden/repo",
        "EDEN_ARTIFACT_URL": "http://srv/artifacts/",
        "EDEN_ARTIFACT_PATH_ROOT": "/var/artifacts",
        "EDEN_READONLY_STORE_URL": "postgresql://ro@db",
    }


def test_docker_mode_suppresses_all_four_keys() -> None:
    """Per §6.4 / §8.9: --exec-mode docker → no substrate keys."""
    args = _ns(
        repo_path="/var/lib/eden/repo",
        artifact_url="http://srv/artifacts/",
        artifact_path_root="/var/artifacts",
        readonly_store_url="postgresql://ro@db",
    )
    substrate = resolve_substrate_args(args, repo_dir=args.repo_path)
    substrate = substrate_args_for_exec_mode(substrate, exec_mode="docker")
    assert substrate.to_env() == {}


def test_partial_opt_in_only_threads_set_keys() -> None:
    args = _ns(
        repo_path=None,
        artifact_url="http://srv/artifacts/",
        artifact_path_root="/var/artifacts",
        readonly_store_url=None,
    )
    substrate = resolve_substrate_args(args, repo_dir=args.repo_path)
    env = substrate.to_env()
    # Vars are absent when the corresponding flag is omitted (so a
    # deployment that doesn't opt in doesn't get the env var set
    # to an empty string).
    assert env == {
        "EDEN_ARTIFACT_URL": "http://srv/artifacts/",
        "EDEN_ARTIFACT_PATH_ROOT": "/var/artifacts",
    }
    assert "EDEN_REPO_DIR" not in env
    assert "EDEN_READONLY_STORE_URL" not in env
