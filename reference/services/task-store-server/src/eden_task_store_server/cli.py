"""Argparse + uvicorn runner for the task-store-server.

Run as ``python -m eden_task_store_server …``.
"""

from __future__ import annotations

import argparse
import contextlib
import signal
import sys
import threading
from typing import Any

import uvicorn
from eden_service_common import configure_logging, get_logger, parse_log_level
from eden_storage import SqliteStore

from .app import build_app, build_store, load_experiment_config


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI args for the task-store-server."""
    parser = argparse.ArgumentParser(
        prog="eden-task-store-server",
        description="EDEN reference task-store server (uvicorn + eden-wire).",
    )
    parser.add_argument(
        "--db-path",
        required=True,
        help="SQLite database path, or ':memory:' for an InMemoryStore.",
    )
    parser.add_argument(
        "--experiment-id",
        required=True,
        help="Experiment identifier served by this process.",
    )
    parser.add_argument(
        "--experiment-config",
        required=True,
        help="YAML experiment-config.schema.json file for metrics_schema seeding.",
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
        "--shared-token",
        default=None,
        help=(
            "Optional bearer token for the reference-only auth middleware "
            "(07-wire-protocol.md §12)."
        ),
    )
    parser.add_argument(
        "--subscribe-timeout",
        type=float,
        default=30.0,
        help="Long-poll window for GET /events/subscribe (default: 30s).",
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
    store = build_store(
        db_path=args.db_path,
        experiment_id=args.experiment_id,
        config=config,
    )
    try:
        app = build_app(
            store=store,
            shared_token=args.shared_token,
            subscribe_timeout=args.subscribe_timeout,
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
            db_path=args.db_path,
        )
        server.run()
        log.info("uvicorn exited")
    finally:
        if isinstance(store, SqliteStore):
            store.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
