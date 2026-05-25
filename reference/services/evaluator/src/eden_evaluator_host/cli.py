"""CLI for the evaluator worker host."""

from __future__ import annotations

import argparse
import socket
from pathlib import Path

from eden_service_common import (
    StopFlag,
    add_common_arguments,
    add_exec_arguments,
    add_substrate_arguments,
    configure_logging,
    credential_secret,
    ensure_repo_clone,
    get_logger,
    install_stop_handlers,
    load_experiment_config,
    parse_env_file,
    parse_log_level,
    reap_orphaned_containers,
    require_command,
    resolve_exec_args,
    resolve_substrate_args,
    resolve_worker_bearer,
    strip_reserved_substrate_keys,
    substrate_args_for_exec_mode,
    wait_for_task_store,
)
from eden_wire import StoreClient

from .host import run_evaluator_loop
from .subprocess_mode import (
    EvaluatorSubprocessConfig,
    run_evaluator_subprocess_loop,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:  # slop-allow: argparse builder; one add_argument per CLI flag with no branching, plus mode-specific validation at the end; splitting fragments the flat flag manifest without reducing logic.
    """Parse CLI args for the evaluator host."""
    parser = argparse.ArgumentParser(
        prog="eden-evaluator-host",
        description="EDEN reference evaluator worker host.",
    )
    add_common_arguments(parser)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument(
        "--mode",
        choices=["scripted", "subprocess"],
        default="scripted",
    )
    parser.add_argument(
        "--experiment-config",
        required=True,
        help=(
            "YAML experiment-config file — read for evaluation_schema so "
            "emitted metrics validate."
        ),
    )
    parser.add_argument(
        "--fail-every",
        type=int,
        default=None,
    )
    parser.add_argument(
        "--artifacts-dir",
        default=None,
        help=(
            "Scripted-mode only: where to write fixture artifact "
            "files when --emit-fixture-artifacts is set."
        ),
    )
    parser.add_argument(
        "--emit-fixture-artifacts",
        action="store_true",
        help=(
            "Scripted-mode only: write small placeholder fixture "
            "files under --artifacts-dir and stamp real "
            "file:///var/lib/eden/artifacts/... URIs onto "
            "EvaluationSubmissions (instead of the default "
            "fictional /tmp/artifacts/... pointers). See issue #111."
        ),
    )
    # Subprocess-mode flags.
    parser.add_argument("--experiment-dir", default=None)
    parser.add_argument(
        "--repo-path",
        default=None,
        help=(
            "Bare git repo (required in --mode subprocess). When "
            "--forgejo-url is set, this becomes the local bare clone, "
            "created at startup and refreshed via fetch_all_heads."
        ),
    )
    parser.add_argument(
        "--forgejo-url",
        dest="forgejo_url",
        default=None,
        help=(
            "Optional HTTP(S) URL of the central git remote (Phase 10d "
            "follow-up B). When set, the evaluator clones --repo-path "
            "from this URL at startup and fetches each variant's "
            "work/* branch before the worktree add."
        ),
    )
    parser.add_argument(
        "--credential-helper",
        default=None,
        help=(
            "Optional path to a git credential-helper script for "
            "HTTP Basic auth against --forgejo-url."
        ),
    )
    parser.add_argument(
        "--worktrees-dir",
        default="/var/lib/eden/worktrees",
    )
    parser.add_argument("--evaluation-task-deadline", type=float, default=300.0)
    parser.add_argument("--evaluation-shutdown-deadline", type=float, default=10.0)
    parser.add_argument("--evaluation-env-file", default=None)
    parser.add_argument("--poll-interval", type=float, default=0.1)
    parser.add_argument("--startup-timeout", type=float, default=30.0)
    add_exec_arguments(parser)
    # 12a-1f substrate-access env flags. See §5.6 of the plan.
    add_substrate_arguments(parser)
    args = parser.parse_args(argv)
    if args.mode == "subprocess":
        for attr in ("experiment_dir", "repo_path"):
            if getattr(args, attr) is None:
                parser.error(
                    f"--{attr.replace('_', '-')} is required in --mode subprocess"
                )
        if args.emit_fixture_artifacts:
            parser.error(
                "--emit-fixture-artifacts is scripted-mode only "
                "(subprocess mode emits real artifacts via the user-supplied "
                "evaluation_command)"
            )
    else:
        if args.emit_fixture_artifacts and args.artifacts_dir is None:
            parser.error(
                "--artifacts-dir is required when --emit-fixture-artifacts is set"
            )
    return args


def _run_subprocess_mode(
    args: argparse.Namespace,
    *,
    log,  # noqa: ANN001 — _CtxAdapter
    client: StoreClient,
    bearer: str | None,
    config,  # noqa: ANN001 — ExperimentConfig
    stop: StopFlag,
) -> None:
    """Drive the subprocess-mode evaluator loop end-to-end."""
    command = require_command(config, "evaluation_command")
    env: dict[str, str] = {}
    if args.evaluation_env_file:
        user_env = parse_env_file(args.evaluation_env_file)
        # Codex round-3: strip reserved substrate keys from
        # the user env BEFORE merging so a user file can't
        # reintroduce keys the host suppressed or selectively
        # configured.
        env.update(strip_reserved_substrate_keys(dict(user_env)))
    try:
        exec_args = resolve_exec_args(args)
    except ValueError as exc:
        raise SystemExit(f"eden-evaluator-host: {exc}") from exc
    host_id = socket.gethostname()
    if exec_args.mode == "docker":
        reap_orphaned_containers(role="evaluator", host=host_id)
    # 12a-1f substrate access: thread the four substrate env
    # vars (EDEN_REPO_DIR / EDEN_ARTIFACT_URL /
    # EDEN_ARTIFACT_PATH_ROOT / EDEN_READONLY_STORE_URL) into
    # the spawned child. Suppressed under --exec-mode docker
    # per §6.4 / §8.9 (sibling containers can't resolve
    # compose-internal hostnames).
    substrate = resolve_substrate_args(args, repo_dir=args.repo_path)
    substrate = substrate_args_for_exec_mode(substrate, exec_mode=exec_args.mode)
    if exec_args.mode == "docker" and (
        args.repo_path is not None
        or args.artifact_url is not None
        or args.readonly_store_url is not None
    ):
        log.warning(
            "substrate_access_disabled_in_exec_mode_docker",
            extra={
                "see": "spec/v0/reference-bindings/worker-host-subprocess.md §9",
            },
        )
    env.update(substrate.to_env())
    # parse_args validates these are set in subprocess mode;
    # asserts narrow types for pyright + surface a clear
    # internal-bug message if the invariant ever breaks.
    assert args.experiment_dir is not None
    assert args.repo_path is not None
    sub_config = EvaluatorSubprocessConfig(
        command=command,
        experiment_dir=Path(args.experiment_dir).resolve(),
        env=env,
        repo_path=Path(args.repo_path).resolve(),
        worktrees_root=Path(args.worktrees_dir),
        task_deadline=args.evaluation_task_deadline,
        shutdown_deadline=args.evaluation_shutdown_deadline,
        exec_mode=exec_args.mode,
        exec_image=exec_args.image,
        exec_volumes=tuple(exec_args.volumes),
        exec_binds=tuple(exec_args.binds),
        cidfile_dir=exec_args.cidfile_dir if exec_args.mode == "docker" else None,
        host_id=host_id,
        worker_credential=credential_secret(bearer),
    )
    run_evaluator_subprocess_loop(
        store=client,
        worker_id=args.worker_id,
        experiment_config=config,
        config=sub_config,
        poll_interval=args.poll_interval,
        stop=stop,
    )


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m eden_evaluator_host``."""
    args = parse_args(argv)
    configure_logging(
        service="evaluator-host",
        experiment_id=args.experiment_id,
        level=parse_log_level(args.log_level),
    )
    log = get_logger(__name__)
    stop = StopFlag()
    install_stop_handlers(stop)
    log.info("waiting_for_task_store")
    wait_for_task_store(
        base_url=args.task_store_url,
        experiment_id=args.experiment_id,
        token=None,
        deadline_seconds=args.startup_timeout,
    )
    config = load_experiment_config(args.experiment_config)
    bearer = resolve_worker_bearer(
        args, worker_id=args.worker_id, labels={"role": "evaluator"}
    )
    log.info("starting", worker_id=args.worker_id, mode=args.mode)
    if args.mode == "subprocess":
        # parse_args validated repo_path is set in subprocess mode.
        assert args.repo_path is not None
        ensure_repo_clone(
            log=log,
            repo_path=args.repo_path,
            forgejo_url=args.forgejo_url,
            credential_helper=args.credential_helper,
        )
    with StoreClient(
        args.task_store_url,
        args.experiment_id,
        bearer=bearer,
    ) as client:
        if args.mode == "scripted":
            scripted_artifacts_dir = (
                Path(args.artifacts_dir)
                if args.emit_fixture_artifacts and args.artifacts_dir is not None
                else None
            )
            run_evaluator_loop(
                store=client,
                worker_id=args.worker_id,
                evaluation_schema=config.evaluation_schema,
                fail_every=args.fail_every,
                poll_interval=args.poll_interval,
                stop=stop,
                artifacts_dir=scripted_artifacts_dir,
            )
        else:
            _run_subprocess_mode(
                args, log=log, client=client, bearer=bearer, config=config, stop=stop
            )
    log.info("evaluator host exited")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
