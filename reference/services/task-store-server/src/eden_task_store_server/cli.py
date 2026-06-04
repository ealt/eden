"""Argparse + uvicorn runner for the task-store-server.

Run as ``python -m eden_task_store_server …``.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Any

import uvicorn
from eden_service_common import configure_logging, get_logger, parse_log_level
from eden_storage import PostgresStore, SqliteStore

from .app import build_app, build_store, load_experiment_config, provision_readonly


# slop-allow: argparse builder; one add_argument per CLI flag with no
# branching (CC=1). Flat flag manifest is most readable (audit L-C).
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI args for the task-store-server."""
    parser = argparse.ArgumentParser(
        prog="eden-task-store-server",
        description="EDEN reference task-store server (uvicorn + eden-wire).",
    )
    parser.add_argument(
        "--store-url",
        default=None,
        help=(
            "Store URL: ':memory:' (in-memory), 'sqlite:///<path>' "
            "(SQLite), 'postgresql://…' (Postgres), or a bare "
            "filesystem path (treated as SQLite for compatibility)."
        ),
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help=(
            "Deprecated alias for --store-url, kept for one phase. "
            "If set, treated as a SQLite path (or ':memory:' for "
            "in-memory)."
        ),
    )
    parser.add_argument(
        "--experiment-id",
        required=True,
        help="Experiment identifier served by this process.",
    )
    parser.add_argument(
        "--experiment-config",
        required=True,
        help="YAML experiment-config.schema.json file for evaluation_schema seeding.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="TCP port to bind (0 = ephemeral; printed on startup).",
    )
    parser.add_argument(
        "--repo-path",
        default=None,
        help=(
            "Optional path to a bare git repo used for the §3.3 "
            "non-no-op variant tree-identity check. When unset, the "
            "Store falls back to a SHA-equality fast path "
            "(commit_sha == parent_commits[k] always rejected); when "
            "set, the Store additionally compares real git trees so "
            "an empty commit on parent (same tree, different SHA) is "
            "also rejected for SHAs already present in the local "
            "bare repo. The resolver is intentionally I/O-free: it "
            "does not fetch from any remote (a fetch inside the per-"
            "operation Store transaction would block every other "
            "task-store request behind a slow / unreachable Forgejo). "
            "Operators who want the deeper check should pre-populate "
            "the bare repo via setup-experiment or external tooling; "
            "the executor's pre-submit `_is_no_op_variant` check is "
            "the canonical enforcement point and runs against the "
            "executor's own controlled clone."
        ),
    )
    parser.add_argument(
        "--base-commit-sha",
        default=os.environ.get("EDEN_BASE_COMMIT_SHA"),
        help=(
            "The experiment seed commit on main (02-data-model.md §2.5). "
            "Recorded on the experiment object at first creation so the "
            "orchestrator can elevate the seed to a kind=='baseline' variant "
            "(§9.4). Defaults to the EDEN_BASE_COMMIT_SHA environment variable. "
            "Consulted only at first experiment-row creation; a reopen keeps "
            "the persisted value. When unset, the experiment carries no seed "
            "and never acquires a baseline."
        ),
    )
    parser.add_argument(
        "--admin-token",
        default=None,
        help=(
            "Deployment admin token for the §13 normative auth middleware. "
            "When set, every /v0/ request MUST carry "
            "Authorization: Bearer <principal>:<secret>; admin: matches this "
            "token, worker: bearers verify against the Store. Pre-12a-1 the "
            "flag was --shared-token (reference-only); that scheme has been "
            "removed."
        ),
    )
    parser.add_argument(
        "--subscribe-timeout",
        type=float,
        default=30.0,
        help="Long-poll window for GET /events/subscribe (default: 30s).",
    )
    parser.add_argument(
        "--artifacts-dir",
        default=None,
        help=(
            "Directory containing artifact files (e.g. idea content). "
            "When set, the reference-only artifact-serving route at "
            "/_reference/experiments/<id>/artifacts/<path> serves "
            "files under this directory (≤ 1 MiB) with safe-delivery "
            "headers. When unset, the route returns 503. See 12a-1f §D.2."
        ),
    )
    parser.add_argument(
        "--artifact-blob-dir",
        default=None,
        help=(
            "Server-PRIVATE, writable directory for the §16 artifact blob "
            "backend (07-wire-protocol.md §16; issue #166). Distinct from "
            "--artifacts-dir (which backs the read-only legacy /_reference "
            "serve route). When set, deposits persist durably here; when "
            "unset, the deposit endpoint falls back to a NON-DURABLE "
            "in-memory backend (a warning is logged). Issue #166."
        ),
    )
    parser.add_argument(
        "--max-artifact-bytes",
        type=int,
        default=None,
        help=(
            "Maximum size (bytes) of a single artifact deposited via "
            "POST /v0/experiments/<id>/artifacts (07-wire-protocol.md "
            "§16.1). Enforced during the multipart stream; over-cap "
            "uploads get 413 eden://error/payload-too-large. Distinct "
            "from the 1 MiB inline-render cap. Defaults to 100 MiB when "
            "unset. Issue #166."
        ),
    )
    parser.add_argument(
        "--readonly-password",
        default=None,
        help=(
            "Password for the eden_readonly Postgres role (also read "
            "from $EDEN_READONLY_PASSWORD). When set AND the backend "
            "is Postgres, the server provisions the role at startup "
            "via REVOKE-then-GRANT (idempotent; rotates the password "
            "on re-run). Non-Postgres backends warn and continue. See "
            "12a-1f §D.3."
        ),
    )
    parser.add_argument(
        "--checkpoint-import-credentials-dir",
        default=os.environ.get("EDEN_CHECKPOINT_IMPORT_CREDENTIALS_DIR"),
        help=(
            "Directory where the checkpoint-import handler persists the "
            "fresh worker bearers minted per `10-checkpoints.md` §8 "
            "step 4 (one `<worker_id>.token` per imported worker). The "
            "reference Compose deployment bind-mounts this directory "
            "into the worker hosts' per-host credentials volumes so "
            "bearers are already in place at host startup — no manual "
            "`reissue_credential` round-trip from the operator. When "
            "unset (the in-process / TestClient default), tokens are "
            "still minted (§8 is normative) but the wire response just "
            "carries a warning that they were not persisted. Also read "
            "from $EDEN_CHECKPOINT_IMPORT_CREDENTIALS_DIR."
        ),
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["debug", "info", "warning", "error"],
        help="Log level (default: info).",
    )
    return parser.parse_args(argv)


class _ListeningAnnouncer:
    """uvicorn lifespan hook that prints the bound host/port to stdout.

    `--port 0` asks the kernel for an ephemeral port; the orchestrator
    / test harness reads the announcement to learn which port was
    actually assigned without having to scrape the log formatter.
    """

    def __init__(self) -> None:
        self.announced = threading.Event()

    def __call__(self, server: uvicorn.Server) -> None:  # noqa: D401
        # uvicorn Server records bound sockets on server.servers[*].sockets
        # after the serve-loop starts; poll in a background thread until
        # one is available, then print.
        def _wait_and_announce() -> None:
            for _ in range(200):
                if server.started and server.servers:
                    sockets = server.servers[0].sockets
                    if sockets:
                        sockname = sockets[0].getsockname()
                        host, port = sockname[0], sockname[1]
                        sys.stdout.write(
                            f"EDEN_TASK_STORE_LISTENING host={host} port={port}\n"
                        )
                        sys.stdout.flush()
                        self.announced.set()
                        return
                threading.Event().wait(0.05)

        t = threading.Thread(target=_wait_and_announce, daemon=True)
        t.start()


def _resolve_store_url(args: argparse.Namespace, log: Any) -> str:
    """Resolve the store URL from ``--store-url`` / the deprecated ``--db-path``.

    Exactly one MUST be supplied; passing both (or neither) is a hard error.
    """
    if args.store_url is None and args.db_path is None:
        raise SystemExit("--store-url is required (or pass --db-path).")
    if args.store_url is not None and args.db_path is not None:
        raise SystemExit("Pass either --store-url or --db-path, not both.")
    if args.store_url is None:
        log.warning("--db-path is deprecated; use --store-url instead")
        assert args.db_path is not None  # guarded by the SystemExit checks above
        return str(args.db_path)
    return str(args.store_url)


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m eden_task_store_server``."""
    args = parse_args(argv)
    configure_logging(
        service="task-store-server",
        experiment_id=args.experiment_id,
        level=parse_log_level(args.log_level),
    )
    log = get_logger(__name__)
    config = load_experiment_config(args.experiment_config)
    store_url = _resolve_store_url(args, log)
    store = build_store(
        store_url=store_url,
        experiment_id=args.experiment_id,
        config=config,
        repo_path=args.repo_path,
        base_commit_sha=args.base_commit_sha or None,
    )
    try:
        # 12a-1f: provision the eden_readonly Postgres role if a
        # password was supplied. Runs before build_app so the
        # provisioning is part of the startup-time atomic unit; a
        # failure here surfaces before uvicorn binds.
        readonly_password = args.readonly_password or os.environ.get(
            "EDEN_READONLY_PASSWORD"
        )
        if readonly_password:
            provision_readonly(store, password=readonly_password)
        artifacts_dir = Path(args.artifacts_dir) if args.artifacts_dir else None
        # 12b: pass the experiment-config text + repo path through to
        # the checkpoint endpoints so exports carry real bytes rather
        # than the wave-4 zero-byte placeholders. Both are optional —
        # test deployments without a paired bare repo leave repo-path
        # unset and the route emits an empty bundle.
        try:
            experiment_config_text = Path(args.experiment_config).read_text(
                encoding="utf-8"
            )
        except OSError:
            experiment_config_text = None
        credentials_dir = (
            Path(args.checkpoint_import_credentials_dir)
            if args.checkpoint_import_credentials_dir
            else None
        )
        artifact_blob_dir = (
            Path(args.artifact_blob_dir) if args.artifact_blob_dir else None
        )
        if artifact_blob_dir is None:
            log.warning(
                "no --artifact-blob-dir set: the §16 artifact deposit "
                "endpoint will use a NON-DURABLE in-memory backend; "
                "deposited bytes are lost on restart (issue #166)"
            )
        app = build_app(
            store=store,
            admin_token=args.admin_token,
            subscribe_timeout=args.subscribe_timeout,
            artifacts_dir=artifacts_dir,
            artifact_blob_dir=artifact_blob_dir,
            max_artifact_bytes=args.max_artifact_bytes,
            checkpoint_experiment_config=experiment_config_text,
            checkpoint_repo_path=args.repo_path,
            checkpoint_import_credentials_dir=credentials_dir,
        )
        uv_config = uvicorn.Config(
            app,
            host=args.host,
            port=args.port,
            log_config=None,
            log_level=args.log_level,
        )
        server = uvicorn.Server(uv_config)
        announcer = _ListeningAnnouncer()
        announcer(server)

        def _stop(*_: Any) -> None:
            log.info("received signal, initiating graceful shutdown")
            server.should_exit = True

        for sig_name in ("SIGTERM", "SIGINT"):
            sig = getattr(signal, sig_name, None)
            if sig is not None:
                with contextlib.suppress(ValueError, OSError):
                    signal.signal(sig, _stop)

        log.info(
            "starting uvicorn",
            host=args.host,
            port=args.port,
            store_url=store_url,
        )
        server.run()
        log.info("uvicorn exited")
    finally:
        if isinstance(store, (SqliteStore, PostgresStore)):
            store.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
