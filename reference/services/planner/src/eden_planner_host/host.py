"""Planner worker host main loop."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from eden_dispatch import ScriptedPlanner
from eden_service_common import StopFlag, get_logger, make_plan_fn
from eden_storage import Store

log = get_logger(__name__)


def _now_iso() -> str:
    return (
        datetime.now(tz=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


def _proposal_id() -> str:
    return f"proposal-{uuid.uuid4().hex[:12]}"


def run_planner_loop(
    *,
    store: Store,
    worker_id: str,
    base_commit_sha: str,
    proposals_per_plan: int,
    poll_interval: float,
    stop: StopFlag,
) -> None:
    """Poll for pending plan tasks and drive each through the scripted profile.

    Returns only when ``stop`` is set. If no pending tasks are visible,
    waits ``poll_interval`` seconds between polls; drains bursts without
    sleeping.
    """
    planner = ScriptedPlanner(
        worker_id=worker_id,
        plan_fn=make_plan_fn(
            base_commit_sha=base_commit_sha,
            proposals_per_plan=proposals_per_plan,
        ),
        proposal_id_factory=_proposal_id,
        now=_now_iso,
    )
    while not stop.is_set():
        processed = planner.run_pending(store, stop=stop.is_set)
        if processed == 0 and stop.wait(poll_interval):
            return
