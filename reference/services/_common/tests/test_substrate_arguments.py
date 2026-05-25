"""Tests for the 12a-1f substrate-access CLI helpers.

Covers ``add_substrate_arguments`` (CLI flag registration),
``resolve_substrate_args`` (Namespace → SubstrateArgs), and
``substrate_args_for_exec_mode`` (docker-mode suppression per §8.9).
"""

from __future__ import annotations

import argparse

import pytest
from eden_service_common import (
    SubstrateArgs,
    add_substrate_arguments,
    resolve_substrate_args,
    substrate_args_for_exec_mode,
)


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="test")
    add_substrate_arguments(parser)
    return parser


# ----------------------------------------------------------------------
# CLI parsing
# ----------------------------------------------------------------------


def test_no_flags_defaults_all_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # Clear env so the test isn't sensitive to the host's settings.
    for var in (
        "EDEN_ARTIFACT_URL",
        "EDEN_ARTIFACT_PATH_ROOT",
        "EDEN_READONLY_STORE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    args = _make_parser().parse_args([])
    assert args.artifact_url is None
    assert args.artifact_path_root is None
    assert args.readonly_store_url is None


def test_explicit_flags_parsed() -> None:
    args = _make_parser().parse_args(
        [
            "--artifact-url",
            "http://server/artifacts/",
            "--artifact-path-root",
            "/var/artifacts",
            "--readonly-store-url",
            "postgresql://ro:pw@db/eden",
        ]
    )
    assert args.artifact_url == "http://server/artifacts/"
    assert args.artifact_path_root == "/var/artifacts"
    assert args.readonly_store_url == "postgresql://ro:pw@db/eden"


def test_env_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "EDEN_ARTIFACT_URL", "http://from-env:8080/artifacts/"
    )
    monkeypatch.setenv(
        "EDEN_ARTIFACT_PATH_ROOT", "/var/from-env"
    )
    monkeypatch.setenv(
        "EDEN_READONLY_STORE_URL", "postgresql://from:env@host/db"
    )
    # Re-create parser AFTER env is set; add_substrate_arguments
    # captures env values at registration time.
    args = _make_parser().parse_args([])
    assert args.artifact_url == "http://from-env:8080/artifacts/"
    assert args.artifact_path_root == "/var/from-env"
    assert args.readonly_store_url == "postgresql://from:env@host/db"


def test_explicit_flag_overrides_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EDEN_ARTIFACT_URL", "http://from-env/")
    args = _make_parser().parse_args(
        ["--artifact-url", "http://from-flag/"]
    )
    assert args.artifact_url == "http://from-flag/"


# ----------------------------------------------------------------------
# resolve_substrate_args
# ----------------------------------------------------------------------


def test_resolve_all_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "EDEN_ARTIFACT_URL",
        "EDEN_ARTIFACT_PATH_ROOT",
        "EDEN_READONLY_STORE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    args = _make_parser().parse_args([])
    sub = resolve_substrate_args(args, repo_dir=None)
    assert sub == SubstrateArgs(
        repo_dir=None,
        artifact_url=None,
        artifact_path_root=None,
        readonly_store_url=None,
    )
    assert sub.to_env() == {}


def test_resolve_all_set() -> None:
    args = _make_parser().parse_args(
        [
            "--artifact-url",
            "http://server/artifacts/",
            "--artifact-path-root",
            "/var/artifacts",
            "--readonly-store-url",
            "postgresql://ro:pw@db/eden",
        ]
    )
    sub = resolve_substrate_args(args, repo_dir="/var/lib/eden/repo")
    assert sub.repo_dir == "/var/lib/eden/repo"
    assert sub.artifact_url == "http://server/artifacts/"
    assert sub.artifact_path_root == "/var/artifacts"
    assert sub.readonly_store_url == "postgresql://ro:pw@db/eden"
    env = sub.to_env()
    assert env == {
        "EDEN_REPO_DIR": "/var/lib/eden/repo",
        "EDEN_ARTIFACT_URL": "http://server/artifacts/",
        "EDEN_ARTIFACT_PATH_ROOT": "/var/artifacts",
        "EDEN_READONLY_STORE_URL": "postgresql://ro:pw@db/eden",
    }


def test_empty_string_is_treated_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty-string env value collapses to None (no key in to_env)."""
    monkeypatch.setenv("EDEN_ARTIFACT_URL", "")
    args = _make_parser().parse_args([])
    sub = resolve_substrate_args(args, repo_dir=None)
    assert sub.artifact_url is None
    assert "EDEN_ARTIFACT_URL" not in sub.to_env()


def test_partial_artifact_pair_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex round-2: the artifact-URL / path-root pair is
    both-or-neither per the binding doc §1.1. Setting only one
    raises ValueError at resolve time so a half-configured
    deployment fails fast instead of shipping inconsistent
    artifact-URI translation to subprocesses.
    """
    monkeypatch.delenv("EDEN_ARTIFACT_PATH_ROOT", raising=False)
    args = _make_parser().parse_args(
        ["--artifact-url", "http://server/artifacts/"]
    )
    with pytest.raises(ValueError, match="--artifact-path-root"):
        resolve_substrate_args(args, repo_dir=None)


def test_strip_reserved_substrate_keys_removes_all_four(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex round-3: a user env file MUST NOT be able to
    reintroduce substrate keys after the host has suppressed
    (docker mode) or selectively configured them.
    """
    from eden_service_common import (
        RESERVED_SUBSTRATE_ENV_KEYS,
        strip_reserved_substrate_keys,
    )

    # Build a user-style env that includes every reserved key
    # plus an innocent passthrough.
    user_env = {
        "EDEN_REPO_DIR": "/hacker/repo",
        "EDEN_ARTIFACT_URL": "http://hacker/artifacts/",
        "EDEN_ARTIFACT_PATH_ROOT": "/hacker/artifacts",
        "EDEN_READONLY_STORE_URL": "postgresql://hacker@evil/db",
        "OPENAI_API_KEY": "passthrough-keep-this",
    }
    out = strip_reserved_substrate_keys(dict(user_env))
    # Every reserved key MUST be gone.
    for key in RESERVED_SUBSTRATE_ENV_KEYS:
        assert key not in out, f"reserved key {key!r} survived strip"
    # Non-reserved keys are preserved.
    assert out == {"OPENAI_API_KEY": "passthrough-keep-this"}


def test_reserved_substrate_env_keys_set() -> None:
    """The reserved-key set has exactly the four 12a-1f substrate
    keys. Mechanical guard against silent expansion.
    """
    from eden_service_common import RESERVED_SUBSTRATE_ENV_KEYS

    assert frozenset(
        {
            "EDEN_REPO_DIR",
            "EDEN_ARTIFACT_URL",
            "EDEN_ARTIFACT_PATH_ROOT",
            "EDEN_READONLY_STORE_URL",
        }
    ) == RESERVED_SUBSTRATE_ENV_KEYS


def test_partial_artifact_pair_rejected_other_direction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Symmetric pair-enforcement: --artifact-path-root without
    --artifact-url also raises (Codex round-2).
    """
    monkeypatch.delenv("EDEN_ARTIFACT_URL", raising=False)
    args = _make_parser().parse_args(
        ["--artifact-path-root", "/var/artifacts"]
    )
    with pytest.raises(ValueError, match="--artifact-url"):
        resolve_substrate_args(args, repo_dir=None)


# ----------------------------------------------------------------------
# substrate_args_for_exec_mode — DooD suppression (§8.9)
# ----------------------------------------------------------------------


def test_host_mode_passes_through() -> None:
    sub = SubstrateArgs(
        repo_dir="/repo",
        artifact_url="http://x/",
        artifact_path_root="/x",
        readonly_store_url="postgresql://x",
    )
    assert substrate_args_for_exec_mode(sub, exec_mode="host") == sub


def test_docker_mode_no_network_suppresses_all() -> None:
    """Docker mode without ``--exec-network`` drops all four keys.

    Reason: spawned siblings attach to the default bridge network and
    cannot resolve compose-internal hostnames.
    """
    sub = SubstrateArgs(
        repo_dir="/repo",
        artifact_url="http://x/",
        artifact_path_root="/x",
        readonly_store_url="postgresql://x",
    )
    suppressed = substrate_args_for_exec_mode(sub, exec_mode="docker")
    assert suppressed == SubstrateArgs(
        repo_dir=None,
        artifact_url=None,
        artifact_path_root=None,
        readonly_store_url=None,
    )
    assert suppressed.to_env() == {}


def test_docker_mode_with_network_forwards_all() -> None:
    """Issue #155: ``--exec-network`` un-suppresses substrate keys.

    When the operator attaches the spawned sibling to the compose
    project network, compose-internal hostnames resolve and the host
    forwards the substrate keys.
    """
    sub = SubstrateArgs(
        repo_dir="/repo",
        artifact_url="http://x/",
        artifact_path_root="/x",
        readonly_store_url="postgresql://x",
    )
    out = substrate_args_for_exec_mode(
        sub, exec_mode="docker", exec_network="eden-reference_default"
    )
    assert out == sub


def test_host_mode_ignores_exec_network() -> None:
    """``exec_network`` is meaningful only in docker mode; host mode
    passes through unconditionally."""
    sub = SubstrateArgs(
        repo_dir="/repo",
        artifact_url="http://x/",
        artifact_path_root="/x",
        readonly_store_url="postgresql://x",
    )
    assert (
        substrate_args_for_exec_mode(
            sub, exec_mode="host", exec_network="ignored"
        )
        == sub
    )
