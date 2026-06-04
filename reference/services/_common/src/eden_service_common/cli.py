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


def _non_empty_str(value: str) -> str:
    """Reject empty / whitespace-only strings as an ``argparse`` ``type=`` validator.

    Used on flags whose downstream consumers (e.g. Pydantic models
    with ``Field(min_length=1)``) would otherwise raise mid-request on
    an empty value. Failing here surfaces a clear "argument required"
    error at startup instead of a 500 on the first operator action.
    """
    if not value.strip():
        raise argparse.ArgumentTypeError("must be a non-empty string")
    return value


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
        type=_non_empty_str,
        help="Experiment identifier — must match the task-store's configured experiment.",
    )
    parser.add_argument(
        "--admin-token",
        default=None,
        help=(
            "Deployment admin token (also read from $EDEN_ADMIN_TOKEN). "
            "Used at startup to reissue this worker's credential when no "
            "persisted token exists or the persisted one is stale. The "
            "worker_id itself is minted once by setup-experiment (read "
            "via the service's EDEN_*_WORKER_ID env var); the service "
            "never fresh-registers. Worker-host requests run under the "
            "per-worker credential, NOT the admin token."
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
    network: str | None = None
    """Docker network attached to spawned sibling containers via
    ``--network`` (docker mode only). When set, substrate env keys
    are FORWARDED into the spawned child (compose-internal hostnames
    like ``task-store-server`` / ``postgres`` resolve from inside
    the sibling); when ``None``, substrate env keys are suppressed
    per the DooD posture documented at
    [`spec/v0/reference-bindings/worker-host-subprocess.md`] §9.3."""


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
    parser.add_argument(
        "--exec-network",
        default=os.environ.get("EDEN_EXEC_NETWORK"),
        help=(
            "Docker network to attach spawned sibling containers to "
            "(--exec-mode=docker only). Required for the spawned "
            "child to reach compose-internal hostnames (e.g. "
            "task-store-server:8080, postgres:5432) so the Phase "
            "12a-1f substrate env vars (EDEN_ARTIFACT_URL, "
            "EDEN_READONLY_STORE_URL) are usable from inside the "
            "sibling. Falls back to $EDEN_EXEC_NETWORK. When unset, "
            "substrate env keys are suppressed in docker mode (the "
            "URLs would not resolve from the bridge network)."
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
    network = getattr(args, "exec_network", None) or None
    return ExecArgs(
        mode=mode,
        image=image,
        volumes=volumes,
        binds=binds,
        cidfile_dir=Path(args.cidfile_dir),
        network=network,
    )


RESERVED_SUBSTRATE_ENV_KEYS: frozenset[str] = frozenset(
    {
        "EDEN_REPO_DIR",
        "EDEN_ARTIFACT_URL",
        "EDEN_ARTIFACT_PATH_ROOT",
        "EDEN_READONLY_STORE_URL",
    }
)
"""Substrate-access env keys the host CLI OWNS — user env files
MUST NOT redirect them.

A user-supplied ``--ideation-env-file`` (etc.) could otherwise
reintroduce these keys after the host's docker-mode suppression
or partial-pair validation has run. The ideator + evaluator CLI
call :func:`strip_reserved_substrate_keys` on the parsed user
env before overlaying the host-resolved
:func:`SubstrateArgs.to_env` so the host's view is authoritative
in every mode.
"""


def strip_reserved_substrate_keys(env: dict[str, str]) -> dict[str, str]:
    """Remove every :data:`RESERVED_SUBSTRATE_ENV_KEYS` entry from ``env``.

    Mutates AND returns ``env`` for ergonomic chaining. Used by
    the ideator + evaluator host CLI to defang user env files that
    would otherwise re-inject substrate keys the host explicitly
    suppressed (docker mode) or selectively configured (partial
    opt-in). Codex round-3 finding on user-env override of the
    host's substrate posture.
    """
    for key in RESERVED_SUBSTRATE_ENV_KEYS:
        env.pop(key, None)
    return env


@dataclass(frozen=True)
class SubstrateArgs:
    """Resolved 12a-1f substrate-access env values.

    Each field is the value the host should set on the spawned
    ``*_command``'s ``EDEN_*`` env var, or ``None`` when the
    operator did not opt in (the host then omits the env var from
    the child entirely, so ``os.environ.get(...)`` returns ``None``
    in user code).

    See ``spec/v0/reference-bindings/worker-host-subprocess.md`` §9.
    """

    repo_dir: str | None
    artifact_url: str | None
    artifact_path_root: str | None
    readonly_store_url: str | None

    def to_env(self) -> dict[str, str]:
        """Render the resolved values as an env-overlay dict.

        Suitable for ``env.update(substrate.to_env())`` onto the
        spawned subprocess environment. Unset fields are omitted
        from the result (so they don't land in the child as empty
        strings — which would differ from "unset" for some Python
        ``os.environ.get`` callers).
        """
        env: dict[str, str] = {}
        if self.repo_dir is not None:
            env["EDEN_REPO_DIR"] = self.repo_dir
        if self.artifact_url is not None:
            env["EDEN_ARTIFACT_URL"] = self.artifact_url
        if self.artifact_path_root is not None:
            env["EDEN_ARTIFACT_PATH_ROOT"] = self.artifact_path_root
        if self.readonly_store_url is not None:
            env["EDEN_READONLY_STORE_URL"] = self.readonly_store_url
        return env


def add_substrate_arguments(parser: argparse.ArgumentParser) -> None:
    """Register the 12a-1f ``--artifact-*`` + ``--readonly-store-url`` flags.

    Used by the ideator + evaluator hosts (subprocess mode). The
    git-substrate flag ``--repo-path`` is service-specific (already
    present on executor / evaluator; added to ideator by 12a-1f) and
    is NOT registered here — only the three substrate-access flags
    that thread directly into the spawned child's env.

    Each flag falls back to its ``EDEN_*`` env var; the convention
    matches the rest of the CLI surface.
    """
    parser.add_argument(
        "--artifact-url",
        default=os.environ.get("EDEN_ARTIFACT_URL"),
        help=(
            "HTTP base URL (ending in /) of the artifact-serving "
            "route, e.g. "
            "http://task-store-server:8080/_reference/experiments/"
            "<id>/artifacts/. Threaded to spawned *_command "
            "subprocesses as EDEN_ARTIFACT_URL. Falls back to "
            "$EDEN_ARTIFACT_URL."
        ),
    )
    parser.add_argument(
        "--artifact-path-root",
        default=os.environ.get("EDEN_ARTIFACT_PATH_ROOT"),
        help=(
            "Filesystem root the EDEN_ARTIFACT_URL is rooted at "
            "(e.g. /var/lib/eden/artifacts). Subprocesses use this "
            "to translate file:// URIs into relative paths under "
            "EDEN_ARTIFACT_URL. Pair with --artifact-url; both or "
            "neither. Falls back to $EDEN_ARTIFACT_PATH_ROOT."
        ),
    )
    parser.add_argument(
        "--readonly-store-url",
        default=os.environ.get("EDEN_READONLY_STORE_URL"),
        help=(
            "Postgres DSN with read-only privileges, e.g. "
            "postgresql://eden_readonly:<pwd>@postgres:5432/eden. "
            "Threaded to spawned *_command subprocesses as "
            "EDEN_READONLY_STORE_URL. Falls back to "
            "$EDEN_READONLY_STORE_URL."
        ),
    )


def resolve_substrate_args(
    args: argparse.Namespace, *, repo_dir: str | Path | None = None
) -> SubstrateArgs:
    """Pack the parsed substrate flags into a :class:`SubstrateArgs`.

    ``repo_dir`` is supplied separately because the spelling of the
    git-substrate flag differs across services (``--repo-path`` on
    ideator / executor / evaluator). The caller passes the resolved
    repo dir (typically ``args.repo_path``); ``None`` means the host
    is not configured with a local clone (e.g. ideator in
    scripted-mode, or subprocess-mode without ``--repo-path``).

    Raises :class:`ValueError` when only ONE of ``--artifact-url`` /
    ``--artifact-path-root`` is set (binding doc §1.1 requires the
    pair both-or-neither so the subprocess's
    ``file://URI``-to-URL translation is well-defined). Codex
    round-2 finding on the pair-enforcement gap.
    """
    artifact_url = getattr(args, "artifact_url", None) or None
    artifact_path_root = getattr(args, "artifact_path_root", None) or None
    if (artifact_url is None) != (artifact_path_root is None):
        which_set = (
            "--artifact-url" if artifact_url is not None else "--artifact-path-root"
        )
        which_missing = (
            "--artifact-path-root" if artifact_url is not None else "--artifact-url"
        )
        raise ValueError(
            f"{which_set} is set but {which_missing} is not — the "
            "artifact-URL / path-root pair is both-or-neither per the "
            "binding doc (spec/v0/reference-bindings/"
            "worker-host-subprocess.md §1.1); the subprocess's "
            "file://URI → URL translation requires both."
        )
    return SubstrateArgs(
        repo_dir=str(repo_dir) if repo_dir is not None else None,
        artifact_url=artifact_url,
        artifact_path_root=artifact_path_root,
        readonly_store_url=getattr(args, "readonly_store_url", None) or None,
    )


def substrate_args_for_exec_mode(
    substrate: SubstrateArgs,
    *,
    exec_mode: str,
    exec_network: str | None = None,
) -> SubstrateArgs:
    """Suppress substrate values when DooD cannot reach compose-internal hosts.

    Per the binding doc's §9.3 trust-boundary note, sibling containers
    started by the host docker daemon resolve hostnames against the
    default bridge network by default — so the compose-internal
    hostnames in ``EDEN_ARTIFACT_URL`` / ``EDEN_READONLY_STORE_URL``
    would not resolve from inside them, and ``EDEN_REPO_DIR`` would
    point at a host-side filesystem path that isn't mounted into the
    sibling.

    Issue #155 introduced ``--exec-network`` to let the operator
    attach spawned siblings to the compose project network. When
    ``exec_network`` is set, those hostnames DO resolve from inside
    the sibling and the substrate keys are forwarded. When
    ``exec_network`` is ``None`` (and the operator hasn't opted into
    a reachable network), the substrate keys are dropped — the host
    logs a WARN so operators understand why substrate access is
    inert.

    Note: ``EDEN_REPO_DIR`` still requires the operator to bind-mount
    the bare repo into the sibling at the same path; ``--exec-bind``
    on the compose host command line is the wiring point.
    """
    if exec_mode == "docker" and exec_network is None:
        return SubstrateArgs(
            repo_dir=None,
            artifact_url=None,
            artifact_path_root=None,
            readonly_store_url=None,
        )
    return substrate
