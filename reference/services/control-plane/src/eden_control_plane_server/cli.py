"""Argparse + uvicorn runner for the control-plane server.

Run as `python -m eden_control_plane_server …`.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading

import uvicorn
from eden_service_common import configure_logging, get_logger, parse_log_level

from .app import build_store, make_app
from .state_sync import StateSyncPoller, make_task_store_reader


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI args for the control-plane server."""
    parser = argparse.ArgumentParser(
        prog="eden-control-plane-server",
        description=(
            "EDEN reference control-plane server "
            "(uvicorn + eden-control-plane)."
        ),
    )
    parser.add_argument(
        "--store-url",
        required=True,
        help=(
            "Control-plane store URL: ':memory:' (in-memory) or "
            "'postgresql://…' (Postgres)."
        ),
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="Bind host (default 127.0.0.1)."
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8081,
        help="Bind port (default 8081; pass 0 for an ephemeral port).",
    )
    parser.add_argument(
        "--admin-token",
        default=None,
        help=(
            "Deployment admin token (or read from EDEN_ADMIN_TOKEN env). "
            "When unset, auth is disabled (test posture, NOT spec-conformant)."
        ),
    )
    parser.add_argument(
        "--lease-duration-seconds",
        type=int,
        default=30,
        help="Lease duration (chapter 11 §4.3 default: 30s).",
    )
    parser.add_argument(
        "--task-store-url",
        default=None,
        help=(
            "Optional task-store-server base URL (e.g. "
            "'http://task-store-server:8080'). When set, the control "
            "plane starts the chapter 11 §3 state-sync poller that "
            "mirrors per-experiment `experiment.state` into the "
            "registry's `last_known_state` projection. When unset, "
            "the poller is disabled and `last_known_state` is "
            "whatever the most recent register / update call wrote."
        ),
    )
    parser.add_argument(
        "--state-sync-interval-seconds",
        type=float,
        default=30.0,
        help=(
            "State-sync polling interval (chapter 11 §3.2 default: 30s)."
        ),
    )
    parser.add_argument(
        "--state-sync-failure-threshold",
        type=int,
        default=10,
        help=(
            "Consecutive read-failure count before chapter 11 §3.4 "
            "stale-warning kicks in on `read_experiment_metadata` "
            "responses (default: 10)."
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

    Mirrors `task-store-server`'s pattern so launchers can read
    `EDEN_CONTROL_PLANE_LISTENING host=… port=…` on `--port 0`.
    """

    def __init__(self) -> None:
        self.announced = threading.Event()

    def __call__(self, server: uvicorn.Server) -> None:
        def _wait_and_announce() -> None:
            for _ in range(200):
                if server.started and server.servers:
                    sockets = server.servers[0].sockets
                    if sockets:
                        sockname = sockets[0].getsockname()
                        host, port = sockname[0], sockname[1]
                        sys.stdout.write(
                            f"EDEN_CONTROL_PLANE_LISTENING host={host} port={port}\n"
                        )
                        sys.stdout.flush()
                        self.announced.set()
                        return
                threading.Event().wait(0.05)

        t = threading.Thread(target=_wait_and_announce, daemon=True)
        t.start()


def main(argv: list[str] | None = None) -> int:
    """Entry point for `python -m eden_control_plane_server`."""
    args = parse_args(argv)
    configure_logging(
        service="control-plane",
        experiment_id="<deployment>",
        level=parse_log_level(args.log_level),
    )
    log = get_logger(__name__)
    admin_token = args.admin_token or os.environ.get("EDEN_ADMIN_TOKEN")
    store = build_store(args.store_url)

    poller: StateSyncPoller | None = None
    if args.task_store_url is not None:
        admin_bearer = f"admin:{admin_token}" if admin_token else None
        poller = StateSyncPoller(
            store,
            state_reader=make_task_store_reader(
                args.task_store_url, admin_bearer=admin_bearer
            ),
            interval_seconds=args.state_sync_interval_seconds,
            failure_threshold=args.state_sync_failure_threshold,
        )

    app = make_app(
        store,
        admin_token=admin_token,
        lease_duration_seconds=args.lease_duration_seconds,
        state_poller=poller,
    )

    if poller is not None:
        @app.on_event("startup")
        async def _start_poller() -> None:
            poller.start()

        @app.on_event("shutdown")
        async def _stop_poller() -> None:
            poller.stop()

    log.info(
        "control_plane_starting",
        extra={
            "store_url": args.store_url,
            "host": args.host,
            "port": args.port,
            "auth_enabled": admin_token is not None,
            "lease_duration_seconds": args.lease_duration_seconds,
            "state_sync_enabled": poller is not None,
        },
    )
    config = uvicorn.Config(
        app=app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        log_config=None,
    )
    server = uvicorn.Server(config=config)
    announcer = _ListeningAnnouncer()
    announcer(server)
    server.run()
    return 0
