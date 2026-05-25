"""JSON-line structured logging for reference services.

Every service emits one JSON object per line to stdout. Fields:

- ``ts``: ISO-8601 timestamp (UTC).
- ``level``: ``debug`` / ``info`` / ``warning`` / ``error``.
- ``service``: service name (ideator-host, orchestrator, …).
- ``experiment_id``: experiment the service is attached to.
- ``message``: human-readable summary.
- Additional context keys merged in from structured kwargs.

Keeping all services on one format makes tailing multiple process
logs — the dominant debugging mode in Phase 8b — tractable.

File-handler persistence (issue #109). When ``EDEN_LOG_DIR`` is set
in the process environment, ``configure_logging`` ALSO installs a
``RotatingFileHandler`` writing to
``${EDEN_LOG_DIR}/<service>.jsonl`` using the same JSON-line
formatter as stdout. Rotation thresholds: ``EDEN_LOG_MAX_BYTES``
(default 50 MB) × ``EDEN_LOG_BACKUP_COUNT`` (default 5). The file
handler is append-mode and flushes after every record (the default
``StreamHandler.emit`` behavior), so a SIGKILL'd process loses at
most the in-flight write. Out-of-band file persistence survives
``compose down -v`` (the directory is a host bind-mount under
``${EDEN_EXPERIMENT_DATA_ROOT}/logs/`` in the reference compose
deployment); see ``docs/observability.md`` §2.5.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

_DEFAULT_LOG_MAX_BYTES = 50 * 1024 * 1024
_DEFAULT_LOG_BACKUP_COUNT = 5

_SERVICE_NAME: str = "unknown"
_EXPERIMENT_ID: str = "unknown"


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:  # noqa: D401
        body: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            "level": record.levelname.lower(),
            "service": _SERVICE_NAME,
            "experiment_id": _EXPERIMENT_ID,
            "message": record.getMessage(),
        }
        # Merge structured kwargs from LoggerAdapter's extra.
        extras = getattr(record, "ctx", None)
        if isinstance(extras, dict):
            for k, v in extras.items():
                if k not in body:
                    body[k] = v
        # Merge plain ``extra=`` kwargs too — some call sites use the
        # standard logging API directly (subprocess_runner's stderr
        # forwarder is one) and would otherwise lose every field they
        # set. Standard LogRecord attributes are blacklisted to avoid
        # dumping internal state.
        _LOG_RECORD_RESERVED = {
            "name", "msg", "args", "levelname", "levelno", "pathname",
            "filename", "module", "exc_info", "exc_text", "stack_info",
            "lineno", "funcName", "created", "msecs", "relativeCreated",
            "thread", "threadName", "processName", "process",
            "ctx", "taskName", "message", "asctime",
        }
        for k, v in record.__dict__.items():
            if k in _LOG_RECORD_RESERVED or k.startswith("_"):
                continue
            if k in body:
                continue
            body[k] = v
        if record.exc_info:
            body["exc"] = self.formatException(record.exc_info)
        return json.dumps(body, sort_keys=False, default=str)


class _CtxAdapter(logging.LoggerAdapter):
    """LoggerAdapter that routes kwargs into a ``ctx`` dict on the record."""

    def process(
        self, msg: Any, kwargs: Any
    ) -> tuple[Any, dict[str, Any]]:
        extra = kwargs.setdefault("extra", {})
        ctx = dict(self.extra) if self.extra else {}
        # Pull out any non-logging kwargs as context.
        recognized = {"exc_info", "stack_info", "stacklevel", "extra"}
        leftover = {k: v for k, v in list(kwargs.items()) if k not in recognized}
        for k in leftover:
            kwargs.pop(k, None)
        ctx.update(leftover)
        extra["ctx"] = ctx
        return msg, kwargs


def _read_positive_int_env(name: str, default: int) -> int:
    """Read ``name`` from the environment, parsed as a positive int.

    Falls back to ``default`` when unset, empty, non-integer, or
    ``< 1``. Misconfiguration MUST NOT crash logger setup — a service
    that can't initialize logging can't surface the configuration
    error, so the safe path is to log a warning post-init (see
    ``configure_logging`` below).
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 1 else default


def _build_file_handler(service: str) -> logging.Handler | None:
    """Build the per-service ``RotatingFileHandler`` when configured.

    Returns ``None`` when ``EDEN_LOG_DIR`` is unset or when the
    directory can't be created (e.g. permission denied). Failures
    here MUST NOT prevent stdout logging from coming up.
    """
    log_dir = os.environ.get("EDEN_LOG_DIR", "").strip()
    if not log_dir:
        return None
    max_bytes = _read_positive_int_env(
        "EDEN_LOG_MAX_BYTES", _DEFAULT_LOG_MAX_BYTES
    )
    backup_count = _read_positive_int_env(
        "EDEN_LOG_BACKUP_COUNT", _DEFAULT_LOG_BACKUP_COUNT
    )
    try:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        # Append mode (`"a"`) is the RotatingFileHandler default;
        # called out for clarity. Encoding is fixed at utf-8 so
        # JSON-line ascii-escape-by-default output stays portable
        # across hosts.
        handler = RotatingFileHandler(
            filename=str(Path(log_dir) / f"{service}.jsonl"),
            mode="a",
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
            delay=False,
        )
    except OSError:
        return None
    handler.setFormatter(_JsonFormatter())
    handler._eden_service_handler = True  # type: ignore[attr-defined]
    return handler


def configure_logging(
    *, service: str, experiment_id: str, level: int = logging.INFO
) -> None:
    """Install the JSON formatter on the root logger.

    Idempotent — safe to call more than once; subsequent calls update
    the service/experiment names and level but don't stack handlers.

    When ``EDEN_LOG_DIR`` is set in the environment, additionally
    install a per-service ``RotatingFileHandler`` writing to
    ``${EDEN_LOG_DIR}/<service>.jsonl`` (see module docstring).
    """
    global _SERVICE_NAME, _EXPERIMENT_ID
    _SERVICE_NAME = service
    _EXPERIMENT_ID = experiment_id
    root = logging.getLogger()
    root.setLevel(level)
    # Remove our prior handlers if present; preserve any external ones.
    for h in list(root.handlers):
        if getattr(h, "_eden_service_handler", False):
            root.removeHandler(h)
            # Close the descriptor so re-init doesn't leak file
            # handles when the destination dir changed (e.g. test
            # fixtures pointing at a fresh tmp_path).
            with contextlib.suppress(Exception):
                h.close()
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(_JsonFormatter())
    stdout_handler._eden_service_handler = True  # type: ignore[attr-defined]
    root.addHandler(stdout_handler)
    file_handler = _build_file_handler(service)
    if file_handler is not None:
        root.addHandler(file_handler)


def get_logger(name: str, **ctx: Any) -> _CtxAdapter:
    """Return a context-aware logger adapter."""
    return _CtxAdapter(logging.getLogger(name), ctx)
