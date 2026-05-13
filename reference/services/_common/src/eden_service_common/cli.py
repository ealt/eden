"""Shared CLI argument helpers.

Every reference service takes the same first four flags. Extracting
them here keeps the flag spellings and help text consistent across
services.
"""

from __future__ import annotations

import argparse
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from .auth import DEFAULT_CREDENTIALS_DIR
from .container_exec import (
    BindMount,
    VolumeMount,
    parse_bind_spec,
    parse_volume_spec,
)

_LOG_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def add_common_arguments(
    parser: argparse.ArgumentParser, *, require_task_store_url: bool = True
) -> None:
    """Register ``--task-store-url``, ``--experiment-id``, ``--admin-token``, ``--log-level``."""
    parser.add_argument(
        "--task-store-url",
        required=require_task_store_url,
        help="Base URL of the task-store server (e.g. http://127.0.0.1:8080).",
    )
    parser.add_argument(
        "--experiment-id",
        required=True,
        help="Experiment identifier — must match the task-store's configured experiment.",
    )
    parser.add_argument(
        "--admin-token",
        default=None,
        help=(
            "Deployment admin token (also read from $EDEN_ADMIN_TOKEN). "
            "Used at startup to register this worker with the task-store "
            "if no persisted credential exists, or to reissue a stale "
            "credential. Worker-host requests run under the per-worker "
            "credential, NOT the admin token."
        ),
    )
    parser.add_argument(
        "--credentials-dir",
        default=None,
        help=(
            "Directory where the worker's per-worker credential is "
            "persisted (default: $EDEN_WORKER_CREDENTIALS_DIR or "
            f"{DEFAULT_CREDENTIALS_DIR}). One token file per worker_id."
        ),
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=list(_LOG_LEVELS),
        help="Log level (default: info).",
    )


def resolve_admin_token(args: argparse.Namespace) -> str | None:
    """Resolve the admin token from ``--admin-token`` or ``$EDEN_ADMIN_TOKEN``.

    Returns ``None`` when neither source supplies a value; the caller
    decides whether that's fatal (it is for first-time worker
    registration; not for restart-with-persisted-credential).
    """
    explicit = getattr(args, "admin_token", None)
    if explicit:
        return explicit
    return os.environ.get("EDEN_ADMIN_TOKEN")


def resolve_credentials_dir(args: argparse.Namespace) -> Path:
    """Resolve the credentials directory from CLI arg, env, or default."""
    explicit = getattr(args, "credentials_dir", None)
    if explicit:
        return Path(explicit)
    env = os.environ.get("EDEN_WORKER_CREDENTIALS_DIR")
    if env:
        return Path(env)
    return DEFAULT_CREDENTIALS_DIR


def parse_log_level(value: str) -> int:
    """Map a ``--log-level`` string to the ``logging`` module's integer level."""
    return _LOG_LEVELS[value]


@dataclass(frozen=True)
class ExecArgs:
    """Resolved ``--exec-*`` arguments for subprocess-mode hosts."""

    mode: str
    """Either ``host`` (default; user command runs on the worker host
    container directly) or ``docker`` (wrapped in ``docker run``)."""
    image: str | None
    volumes: list[VolumeMount]
    binds: list[BindMount]
    cidfile_dir: Path


def add_exec_arguments(parser: argparse.ArgumentParser) -> None:
    """Register the ``--exec-*`` family of flags on ``parser``.

    Used by every subprocess-mode host (ideator / executor /
    evaluator). Validation of the docker-mode requirements
    (image set, mounts present) happens in :func:`resolve_exec_args`
    once the parser has populated the namespace, since per-role
    validation depends on whether ``--mode subprocess`` was selected.
    """
    parser.add_argument(
        "--exec-mode",
        default="host",
        choices=("host", "docker"),
        help=(
            "How to invoke the user's *_command (default: host). "
            "'docker' wraps every spawn in `docker run` (DooD); "
            "the worker host container must mount /var/run/docker.sock."
        ),
    )
    parser.add_argument(
        "--exec-image",
        default=os.environ.get("EDEN_EXEC_IMAGE"),
        help=(
            "Image to run when --exec-mode=docker. Required in that "
            "mode; falls back to $EDEN_EXEC_IMAGE."
        ),
    )
    parser.add_argument(
        "--exec-volume",
        action="append",
        default=[],
        metavar="NAME:TARGET[:ro|rw]",
        help=(
            "Forward a docker named volume into spawned child "
            "containers. Repeatable. Required for executor / "
            "evaluator (eden-bare-repo + eden-worktrees)."
        ),
    )
    parser.add_argument(
        "--exec-bind",
        action="append",
        default=[],
        metavar="HOST_PATH:TARGET[:ro|rw]",
        help=(
            "Forward a host-side bind mount into spawned child "
            "containers. Repeatable. Required for the experiment-dir "
            "bind (host-side absolute path)."
        ),
    )
    parser.add_argument(
        "--cidfile-dir",
        default="/var/lib/eden/cidfiles",
        help=(
            "Directory where per-spawn cidfiles are written "
            "(--exec-mode=docker only)."
        ),
    )


def resolve_exec_args(args: argparse.Namespace) -> ExecArgs:
    """Validate and pack the parsed ``--exec-*`` flags.

    For ``--exec-mode docker``, ``--exec-image`` is required. Volume
    and bind specs are parsed strictly; malformed specs raise
    :class:`ValueError` (caller maps to ``parser.error``).
    """
    mode = args.exec_mode
    image = args.exec_image
    if mode == "docker" and not image:
        raise ValueError(
            "--exec-mode=docker requires --exec-image (or $EDEN_EXEC_IMAGE)"
        )
    volumes = [parse_volume_spec(v) for v in (args.exec_volume or [])]
    binds = [parse_bind_spec(b) for b in (args.exec_bind or [])]
    return ExecArgs(
        mode=mode,
        image=image,
        volumes=volumes,
        binds=binds,
        cidfile_dir=Path(args.cidfile_dir),
    )
