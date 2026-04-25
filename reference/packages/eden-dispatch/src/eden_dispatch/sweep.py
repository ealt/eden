"""Expired-claim sweeper.

A claim's ``expires_at`` is metadata on its own; nothing in the
reference stack acts on it without a sweeper. This module exposes
``sweep_expired_claims`` which the orchestrator service's loop calls
once per iteration to issue ``reclaim(task_id, "expired")`` against
any claimed task whose claim has expired. Per-task errors are
logged but do not abort the sweep.

This is the smallest cross-cutting addition that makes
``--claim-ttl-seconds`` operationally real for the Phase 9 web UI.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Protocol

from eden_storage import Store

logger = logging.getLogger(__name__)


class _ClaimedTask(Protocol):
    task_id: str

    @property
    def claim(self) -> object | None: ...


def sweep_expired_claims(store: Store, *, now: datetime) -> int:
    """Reclaim every claimed task whose ``expires_at`` is strictly before ``now``.

    Returns the count of tasks reclaimed. Tasks without an
    ``expires_at`` (or whose ``expires_at`` is at/after ``now``) are
    left alone. Per-task ``reclaim`` failures are logged and skipped.
    """
    reclaimed = 0
    for task in store.list_tasks(state="claimed"):
        claim = getattr(task, "claim", None)
        if claim is None:
            continue
        expires_at = getattr(claim, "expires_at", None)
        if expires_at is None:
            continue
        if isinstance(expires_at, str):
            try:
                deadline = datetime.fromisoformat(expires_at)
            except ValueError:
                logger.warning(
                    "skipping task %s: malformed expires_at %r",
                    task.task_id,
                    expires_at,
                )
                continue
        else:
            deadline = expires_at
        if deadline >= now:
            continue
        try:
            store.reclaim(task.task_id, "expired")
        except Exception:  # noqa: BLE001
            logger.exception("expired-claim reclaim failed for %s", task.task_id)
            continue
        reclaimed += 1
    return reclaimed
