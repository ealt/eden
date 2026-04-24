"""JSON-line structured logging for reference services.

Every service emits one JSON object per line to stdout. Fields:

- ``ts``: ISO-8601 timestamp (UTC).
- ``level``: ``debug`` / ``info`` / ``warning`` / ``error``.
- ``service``: service name (planner-host, orchestrator, …).
- ``experiment_id``: experiment the service is attached to.
- ``message``: human-readable summary.
- Additional context keys merged in from structured kwargs.

Keeping all services on one format makes tailing multiple process
logs — the dominant debugging mode in Phase 8b — tractable.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

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


def configure_logging(
    *, service: str, experiment_id: str, level: int = logging.INFO
) -> None:
    """Install the JSON formatter on the root logger.

    Idempotent — safe to call more than once; subsequent calls update
    the service/experiment names and level but don't stack handlers.
    """
    global _SERVICE_NAME, _EXPERIMENT_ID
    _SERVICE_NAME = service
    _EXPERIMENT_ID = experiment_id
    root = logging.getLogger()
    root.setLevel(level)
    # Remove our prior handler if present; preserve any external ones.
    for h in list(root.handlers):
        if getattr(h, "_eden_service_handler", False):
            root.removeHandler(h)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    handler._eden_service_handler = True  # type: ignore[attr-defined]
    root.addHandler(handler)


def get_logger(name: str, **ctx: Any) -> _CtxAdapter:
    """Return a context-aware logger adapter."""
    return _CtxAdapter(logging.getLogger(name), ctx)
