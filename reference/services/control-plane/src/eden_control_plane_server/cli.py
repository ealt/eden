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
    app = make_app(
        store,
        admin_token=admin_token,
        lease_duration_seconds=args.lease_duration_seconds,
    )
    log.info(
        "control_plane_starting",
        extra={
            "store_url": args.store_url,
            "host": args.host,
            "port": args.port,
            "auth_enabled": admin_token is not None,
            "lease_duration_seconds": args.lease_duration_seconds,
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
