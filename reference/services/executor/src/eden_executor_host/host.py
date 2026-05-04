"""Executor worker host main loop."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from eden_dispatch import ScriptedExecutor
from eden_service_common import StopFlag, get_logger, make_implement_fn
from eden_storage import Store

log = get_logger(__name__)


def _now_iso() -> str:
    return (
        datetime.now(tz=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


def _variant_id() -> str:
    return f"variant-{uuid.uuid4().hex[:12]}"


def run_executor_loop(
    *,
    store: Store,
    worker_id: str,
    repo_path: str,
    fail_every: int | None,
    poll_interval: float,
    stop: StopFlag,
) -> None:
    """Poll for pending execute tasks and drive each through the scripted profile.

    Returns only when ``stop`` is set.
    """
    impl = ScriptedExecutor(
        worker_id=worker_id,
        implement_fn=make_implement_fn(
            repo_path=repo_path, fail_every=fail_every
        ),
        trial_id_factory=_variant_id,
        now=_now_iso,
    )
    while not stop.is_set():
        processed = impl.run_pending(store, stop=stop.is_set)
        if processed == 0 and stop.wait(poll_interval):
            return
