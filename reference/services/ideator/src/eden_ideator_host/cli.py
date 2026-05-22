"""CLI for the ideator worker host."""

from __future__ import annotations

import argparse
import socket
from pathlib import Path

from eden_git import GitRepo
from eden_service_common import (
    StopFlag,
    add_common_arguments,
    add_exec_arguments,
    add_substrate_arguments,
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
    resolve_substrate_args,
    resolve_worker_bearer,
    strip_reserved_substrate_keys,
    substrate_args_for_exec_mode,
    wait_for_task_store,
    wrap_command,
)
from eden_wire import StoreClient

from .host import (
    build_subprocess_config,
    run_ideator_loop,
    run_ideator_subprocess_loop,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI args for the ideator host."""
    parser = argparse.ArgumentParser(
        prog="eden-ideator-host",
        description="EDEN reference ideator worker host.",
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
            "40- or 64-hex commit SHA threaded into every idea's "
            "parent_commits. Required in --mode scripted."
        ),
    )
    parser.add_argument(
        "--ideas-per-ideation",
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
        help="Where to write content artifacts (required in --mode subprocess).",
    )
    # 12a-1f git substrate: optional clone-on-startup wiring mirroring
    # executor / evaluator. Subprocess-mode only — scripted-mode
    # ideator never reads git, and these flags are validated as a
    # set (all-three or none).
    parser.add_argument(
        "--repo-path",
        default=None,
        help=(
            "Bare git repo (subprocess mode only). When "
            "--forgejo-url is also set, this becomes the local bare "
            "clone of the Forgejo-hosted repo (cloned at startup if "
            "absent, otherwise fetched). The path is also threaded "
            "to the spawned *_command's env as EDEN_REPO_DIR so "
            "agentic ideators can `git log` / `git show` against "
            "the full ref space."
        ),
    )
    parser.add_argument(
        "--forgejo-url",
        default=None,
        help=(
            "Optional HTTP(S) URL of the central git remote. Pair "
            "with --repo-path + --credential-helper."
        ),
    )
    parser.add_argument(
        "--credential-helper",
        default=None,
        help=(
            "Path to a git credential-helper script for HTTP Basic "
            "auth against --forgejo-url."
        ),
    )
    parser.add_argument("--ideation-startup-deadline", type=float, default=30.0)
    parser.add_argument("--ideation-task-deadline", type=float, default=120.0)
    parser.add_argument("--ideation-shutdown-deadline", type=float, default=10.0)
    parser.add_argument("--ideation-env-file", default=None)
    parser.add_argument("--poll-interval", type=float, default=0.1)
    parser.add_argument("--startup-timeout", type=float, default=30.0)
    add_exec_arguments(parser)
    # 12a-1f substrate-access env flags. Registered for both
    # scripted and subprocess modes — scripted mode ignores them
    # (no subprocess to forward them to). See §5.5 of the plan.
    add_substrate_arguments(parser)
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


def _credential_secret(bearer: str | None) -> str | None:
    """Extract the secret half of a §13.1 ``<principal>:<secret>`` bearer."""
    if bearer is None or ":" not in bearer:
        return None
    return bearer.split(":", 1)[1]


def _validate_sha(value: str) -> None:
    if len(value) not in (40, 64) or any(c not in "0123456789abcdef" for c in value):
        raise SystemExit(
            f"--base-commit-sha {value!r}: expected 40- or 64-hex SHA"
        )


def _ensure_repo_clone(
    *,
    log,  # noqa: ANN001 — _CtxAdapter, not exposed
    repo_path: str,
    forgejo_url: str | None,
    credential_helper: str | None,
) -> None:
    """Materialize the ideator's local clone per Phase 10d follow-up B §D.5.

    Mirrors the executor / evaluator clone-on-startup pattern. No-op
    when ``forgejo_url`` is ``None`` (the chunk-10d posture). Otherwise:
    clone --bare at first run, fetch_all_heads on subsequent starts.
    """
    if forgejo_url is None:
        return
    path = Path(repo_path)
    if (path / "HEAD").is_file():
        log.info("fetching_remote_heads", url=forgejo_url)
        GitRepo(path).fetch_all_heads()
        return
    log.info("cloning_from_remote", url=forgejo_url, dest=str(path))
    GitRepo.clone_from(
        url=forgejo_url,
        dest=path,
        bare=True,
        credential_helper=credential_helper,
    )


def _build_subprocess_env(
    args: argparse.Namespace,
    exec_args,  # noqa: ANN001 — ResolvedExecArgs
    log,  # noqa: ANN001 — _CtxAdapter
) -> dict[str, str]:
    """Compose the spawned ideation_command's env per §D.0 contract.

    User env file lays down the base; host-owned reserved EDEN_* keys
    overlay on top so a user file can't redirect the protocol surface.
    """
    env: dict[str, str] = {}
    if args.ideation_env_file:
        user_env = parse_env_file(args.ideation_env_file)
        # Codex round-3: strip reserved substrate keys from the user env
        # BEFORE merging so a user file can NEVER reintroduce keys the
        # host suppressed (e.g. under --exec-mode docker) or selectively
        # configured. The host's authoritative substrate overlay is
        # applied below.
        env.update(strip_reserved_substrate_keys(dict(user_env)))
    # When --exec-mode=docker, EDEN_EXPERIMENT_DIR inside the spawned
    # child container resolves to the bind-mount target supplied via
    # --exec-bind, NOT the host-side path. We set EDEN_EXPERIMENT_DIR
    # to the *worker-host*-side path here; the wrap echoes it through
    # to the child via `-e EDEN_EXPERIMENT_DIR`. The bind mount in the
    # spawned child uses the same target path so the env var resolves
    # consistently in both places (because the child mount target
    # equals the worker host's path).
    env["EDEN_EXPERIMENT_DIR"] = str(Path(args.experiment_dir).resolve())
    # 12a-1f substrate access: thread EDEN_REPO_DIR / EDEN_ARTIFACT_URL /
    # EDEN_ARTIFACT_PATH_ROOT / EDEN_READONLY_STORE_URL into the spawned
    # child's env so agentic ideators can read git / artifacts / the
    # Postgres readonly substrate without making per-query wire
    # round-trips. Per §6.4 / §8.9 of the plan, these keys are
    # SUPPRESSED in --exec-mode docker because sibling containers can't
    # resolve compose-internal hostnames (no --network plumbing in
    # wrap_command yet).
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
    return env


def _build_docker_wrap_factory(
    *,
    args: argparse.Namespace,
    exec_args,  # noqa: ANN001 — ResolvedExecArgs
    command: str,
    env_keys: list[str],
):  # noqa: ANN201 — returns Callable[[], tuple[...]]
    """Construct the per-spawn DooD wrap factory (docker exec-mode only)."""
    # Reap any orphaned sibling containers from a prior crash before we
    # spawn fresh ones.
    host_id = socket.gethostname()
    reap_orphaned_containers(role="ideator", host=host_id)

    # Capture per-spawn closure inputs.
    ideator_command = command
    cwd_target = str(Path(args.experiment_dir).resolve())
    volumes = exec_args.volumes
    binds = exec_args.binds
    image = exec_args.image
    cidfile_dir = exec_args.cidfile_dir
    assert image is not None  # guaranteed by resolve_exec_args

    def _ideator_wrap_factory():
        cidfile = make_cidfile_path(cidfile_dir=cidfile_dir, role="ideator")
        wrapped = wrap_command(
            original_command=ideator_command,
            image=image,
            cwd_target=cwd_target,
            cidfile=cidfile,
            role="ideator",
            task_id=host_id,
            host_id=host_id,
            volumes=volumes,
            binds=binds,
            env_keys=env_keys,
        )
        post_kill, cleanup = make_cidfile_callbacks(cidfile)
        return wrapped, post_kill, [cleanup]

    return _ideator_wrap_factory


def _run_subprocess_mode(
    args: argparse.Namespace,
    *,
    log,  # noqa: ANN001 — _CtxAdapter
    client: StoreClient,
    bearer: str | None,
    stop: StopFlag,
) -> None:
    """Drive the subprocess-mode ideator loop end-to-end."""
    config = load_experiment_config(args.experiment_config)
    command = require_command(config, "ideation_command")
    try:
        exec_args = resolve_exec_args(args)
    except ValueError as exc:
        raise SystemExit(f"eden-ideator-host: {exc}") from exc
    env = _build_subprocess_env(args, exec_args, log)

    wrap_factory = None
    if exec_args.mode == "docker":
        wrap_factory = _build_docker_wrap_factory(
            args=args,
            exec_args=exec_args,
            command=command,
            env_keys=list(env.keys()),
        )

    subprocess_config = build_subprocess_config(
        command=command,
        cwd=Path(args.experiment_dir).resolve(),
        env=env,
        startup_deadline=args.ideation_startup_deadline,
        task_deadline=args.ideation_task_deadline,
        shutdown_deadline=args.ideation_shutdown_deadline,
        wrap_factory=wrap_factory,
        worker_id=args.worker_id,
        worker_credential=_credential_secret(bearer),
    )
    run_ideator_subprocess_loop(
        store=client,
        worker_id=args.worker_id,
        experiment_id=args.experiment_id,
        experiment_config=config,
        artifacts_dir=Path(args.artifacts_dir),
        subprocess_config=subprocess_config,
        poll_interval=args.poll_interval,
        stop=stop,
    )


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m eden_ideator_host``."""
    args = parse_args(argv)
    configure_logging(
        service="ideator-host",
        experiment_id=args.experiment_id,
        level=parse_log_level(args.log_level),
    )
    log = get_logger(__name__)
    stop = StopFlag()
    install_stop_handlers(stop)
    log.info("waiting_for_task_store")
    # The readiness probe accepts 200/401/403 ("server is up") so the
    # host can run before it has its per-worker credential. The
    # bootstrap below registers and persists the credential against
    # the admin bearer.
    wait_for_task_store(
        base_url=args.task_store_url,
        experiment_id=args.experiment_id,
        token=None,
        deadline_seconds=args.startup_timeout,
    )
    bearer = resolve_worker_bearer(
        args, worker_id=args.worker_id, labels={"role": "ideator"}
    )
    log.info("starting", worker_id=args.worker_id, mode=args.mode)
    # 12a-1f: clone-on-startup wiring for the git substrate. Only
    # runs in subprocess mode (scripted ideator never reads git);
    # the helper itself is also a no-op when --forgejo-url is not set.
    if args.mode == "subprocess" and args.repo_path is not None:
        _ensure_repo_clone(
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
            run_ideator_loop(
                store=client,
                worker_id=args.worker_id,
                base_commit_sha=args.base_commit_sha,
                ideas_per_ideation=args.ideas_per_ideation,
                poll_interval=args.poll_interval,
                stop=stop,
            )
        else:
            _run_subprocess_mode(
                args, log=log, client=client, bearer=bearer, stop=stop
            )
    log.info("ideator host exited")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
