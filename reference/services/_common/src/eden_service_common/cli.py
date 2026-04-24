"""Shared CLI argument helpers.

Every reference service takes the same first four flags. Extracting
them here keeps the flag spellings and help text consistent across
services.
"""

from __future__ import annotations

import argparse
import logging

_LOG_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def add_common_arguments(
    parser: argparse.ArgumentParser, *, require_task_store_url: bool = True
) -> None:
    """Register ``--task-store-url``, ``--experiment-id``, ``--shared-token``, ``--log-level``."""
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
        "--shared-token",
        default=None,
        help=(
            "Reference-only shared bearer token. If set, passed as "
            "'Authorization: Bearer <token>' on every request (see "
            "07-wire-protocol.md §12)."
        ),
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=list(_LOG_LEVELS),
        help="Log level (default: info).",
    )


def parse_log_level(value: str) -> int:
    """Map a ``--log-level`` string to the ``logging`` module's integer level."""
    return _LOG_LEVELS[value]
