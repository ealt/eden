"""CLI for the planner worker host."""

from __future__ import annotations

import argparse
import socket
from pathlib import Path

from eden_service_common import (
    StopFlag,
    add_common_arguments,
    add_exec_arguments,
    configure_logging,
    get_logger,
    install_stop_handlers,
    load_experiment_config,
    make_cidfile_callbacks,
    make_cidfile_path,
    parse_env_file,
    parse_log_level,
    reap_orphaned_containers,
    require_command,
    resolve_exec_args,
    wait_for_task_store,
    wrap_command,
)
from eden_wire import StoreClient

from .host import (
    build_subprocess_config,
    run_planner_loop,
    run_planner_subprocess_loop,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI args for the planner host."""
    parser = argparse.ArgumentParser(
        prog="eden-planner-host",
        description="EDEN reference planner worker host.",
    )
    add_common_arguments(parser)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument(
        "--mode",
        choices=["scripted", "subprocess"],
        default="scripted",
        help="Worker behaviour: deterministic in-process vs user-supplied subprocess.",
    )
    parser.add_argument(
        "--base-commit-sha",
        required=False,
        default=None,
        help=(
            "40- or 64-hex commit SHA threaded into every proposal's "
            "parent_commits. Required in --mode scripted."
        ),
    )
    parser.add_argument(
        "--proposals-per-plan",
        type=int,
        default=1,
    )
    # Subprocess-mode flags.
    parser.add_argument(
        "--experiment-config",
        default=None,
        help="Path to the experiment-config YAML (required in --mode subprocess).",
    )
    parser.add_argument(
        "--experiment-dir",
        default=None,
        help="Host-side path to the experiment directory (required in --mode subprocess).",
    )
    parser.add_argument(
        "--artifacts-dir",
        default=None,
        help="Where to write rationale artifacts (required in --mode subprocess).",
    )
    parser.add_argument("--plan-startup-deadline", type=float, default=30.0)
    parser.add_argument("--plan-task-deadline", type=float, default=120.0)
    parser.add_argument("--plan-shutdown-deadline", type=float, default=10.0)
    parser.add_argument("--plan-env-file", default=None)
    parser.add_argument("--poll-interval", type=float, default=0.1)
    parser.add_argument("--startup-timeout", type=float, default=30.0)
    add_exec_arguments(parser)
    args = parser.parse_args(argv)
    if args.mode == "scripted":
        if not args.base_commit_sha:
            parser.error("--base-commit-sha is required in --mode scripted")
        _validate_sha(args.base_commit_sha)
    else:
        for attr in ("experiment_config", "experiment_dir", "artifacts_dir"):
            if getattr(args, attr) is None:
                parser.error(
                    f"--{attr.replace('_', '-')} is required in --mode subprocess"
                )
    return args


def _validate_sha(value: str) -> None:
    if len(value) not in (40, 64) or any(c not in "0123456789abcdef" for c in value):
        raise SystemExit(
            f"--base-commit-sha {value!r}: expected 40- or 64-hex SHA"
        )


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m eden_planner_host``."""
    args = parse_args(argv)
    configure_logging(
        service="planner-host",
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
        token=args.shared_token,
        deadline_seconds=args.startup_timeout,
    )
    log.info("starting", worker_id=args.worker_id, mode=args.mode)
    with StoreClient(
        args.task_store_url,
        args.experiment_id,
        token=args.shared_token,
    ) as client:
        if args.mode == "scripted":
            run_planner_loop(
                store=client,
                worker_id=args.worker_id,
                base_commit_sha=args.base_commit_sha,
                proposals_per_plan=args.proposals_per_plan,
                poll_interval=args.poll_interval,
                stop=stop,
            )
        else:
            config = load_experiment_config(args.experiment_config)
            command = require_command(config, "plan_command")
            # User env file lays down the base; host-owned reserved
            # EDEN_* keys overlay on top so a user file can't redirect
            # the protocol surface (§D.0 contract).
            env: dict[str, str] = {}
            if args.plan_env_file:
                env.update(parse_env_file(args.plan_env_file))
            # When --exec-mode=docker, EDEN_EXPERIMENT_DIR inside the
            # spawned child container resolves to the bind-mount
            # target supplied via --exec-bind, NOT the host-side path.
            # We set EDEN_EXPERIMENT_DIR to the *worker-host*-side
            # path here; the wrap echoes it through to the child via
            # `-e EDEN_EXPERIMENT_DIR`. The bind mount in the
            # spawned child uses the same target path so the env var
            # resolves consistently in both places (because the child
            # mount target equals the worker host's path).
            env["EDEN_EXPERIMENT_DIR"] = str(Path(args.experiment_dir).resolve())
            try:
                exec_args = resolve_exec_args(args)
            except ValueError as exc:
                parser_error_msg = str(exc)
                raise SystemExit(f"eden-planner-host: {parser_error_msg}") from exc

            wrap_factory = None
            if exec_args.mode == "docker":
                # Reap any orphaned sibling containers from a prior
                # crash before we spawn fresh ones.
                host_id = socket.gethostname()
                reap_orphaned_containers(role="planner", host=host_id)

                # Capture per-spawn closure inputs.
                planner_command = command
                cwd_target = str(Path(args.experiment_dir).resolve())
                env_keys = list(env.keys())
                volumes = exec_args.volumes
                binds = exec_args.binds
                image = exec_args.image
                cidfile_dir = exec_args.cidfile_dir
                assert image is not None  # guaranteed by resolve_exec_args

                def _planner_wrap_factory():
                    cidfile = make_cidfile_path(
                        cidfile_dir=cidfile_dir, role="planner"
                    )
                    wrapped = wrap_command(
                        original_command=planner_command,
                        image=image,
                        cwd_target=cwd_target,
                        cidfile=cidfile,
                        role="planner",
                        task_id=host_id,
                        host_id=host_id,
                        volumes=volumes,
                        binds=binds,
                        env_keys=env_keys,
                    )
                    post_kill, cleanup = make_cidfile_callbacks(cidfile)
                    return wrapped, post_kill, [cleanup]

                wrap_factory = _planner_wrap_factory

            subprocess_config = build_subprocess_config(
                command=command,
                cwd=Path(args.experiment_dir).resolve(),
                env=env,
                startup_deadline=args.plan_startup_deadline,
                task_deadline=args.plan_task_deadline,
                shutdown_deadline=args.plan_shutdown_deadline,
                wrap_factory=wrap_factory,
            )
            run_planner_subprocess_loop(
                store=client,
                worker_id=args.worker_id,
                experiment_id=args.experiment_id,
                experiment_config=config,
                artifacts_dir=Path(args.artifacts_dir),
                subprocess_config=subprocess_config,
                poll_interval=args.poll_interval,
                stop=stop,
            )
    log.info("planner host exited")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
