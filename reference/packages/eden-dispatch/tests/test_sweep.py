"""Unit tests for ``eden_dispatch.sweep.sweep_expired_claims``.

The sweeper is the operational counterpart to the ``expires_at``
metadata that the web-ui service sets on every UI claim. Without
something invoking ``reclaim(task_id, "expired")``, TTL is just a
field. This module pins the behavior in isolation; the orchestrator
service's loop wires it into the polling cadence.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from eden_contracts import MetricsSchema, ReclaimCause
from eden_dispatch import (
    InMemoryStore,
    sweep_expired_claims,
)


def _make_store() -> InMemoryStore:
    return InMemoryStore(
        experiment_id="exp-sweep",
        metrics_schema=MetricsSchema({"loss": "real"}),
    )


def _claim_with_expiry(
    store: InMemoryStore,
    task_id: str,
    *,
    worker_id: str,
    expires_at: datetime | None,
) -> None:
    store.create_plan_task(task_id)
    store.claim(task_id, worker_id=worker_id, expires_at=expires_at)


def test_expired_claim_is_reclaimed() -> None:
    store = _make_store()
    now = datetime(2026, 4, 24, 12, 0, tzinfo=UTC)
    expired = now - timedelta(seconds=1)
    _claim_with_expiry(store, "t-1", worker_id="ui-1", expires_at=expired)

    count = sweep_expired_claims(store, now=now)

    assert count == 1
    task = store.read_task("t-1")
    assert task.state == "pending"
    reclaimed_events = [e for e in store.events() if e.type == "task.reclaimed"]
    assert len(reclaimed_events) == 1
    assert reclaimed_events[0].data["cause"] == "expired"
    assert reclaimed_events[0].data["task_id"] == "t-1"


def test_unexpired_claim_left_alone() -> None:
    store = _make_store()
    now = datetime(2026, 4, 24, 12, 0, tzinfo=UTC)
    fresh = now + timedelta(seconds=60)
    _claim_with_expiry(store, "t-1", worker_id="ui-1", expires_at=fresh)

    count = sweep_expired_claims(store, now=now)

    assert count == 0
    task = store.read_task("t-1")
    assert task.state == "claimed"


def test_claim_without_expires_at_left_alone() -> None:
    store = _make_store()
    now = datetime(2026, 4, 24, 12, 0, tzinfo=UTC)
    _claim_with_expiry(store, "t-1", worker_id="ui-1", expires_at=None)

    count = sweep_expired_claims(store, now=now)

    assert count == 0
    assert store.read_task("t-1").state == "claimed"


def test_at_deadline_left_alone() -> None:
    """``expires_at == now`` is not yet expired; only strictly-before is."""
    store = _make_store()
    now = datetime(2026, 4, 24, 12, 0, tzinfo=UTC)
    _claim_with_expiry(store, "t-1", worker_id="ui-1", expires_at=now)

    count = sweep_expired_claims(store, now=now)

    assert count == 0
    assert store.read_task("t-1").state == "claimed"


def test_per_task_failure_does_not_abort_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure on one task must not stop the others from being swept."""
    store = _make_store()
    now = datetime(2026, 4, 24, 12, 0, tzinfo=UTC)
    expired = now - timedelta(seconds=1)
    _claim_with_expiry(store, "t-good", worker_id="ui-1", expires_at=expired)
    _claim_with_expiry(store, "t-bad", worker_id="ui-1", expires_at=expired)

    real_reclaim = store.reclaim

    def flaky_reclaim(task_id: str, cause: ReclaimCause) -> None:
        if task_id == "t-bad":
            raise RuntimeError("simulated reclaim failure")
        real_reclaim(task_id, cause)

    monkeypatch.setattr(store, "reclaim", flaky_reclaim)

    count = sweep_expired_claims(store, now=now)

    assert count == 1
    assert store.read_task("t-good").state == "pending"
    # The bad task remains claimed because the reclaim raised.
    assert store.read_task("t-bad").state == "claimed"


def test_only_claimed_tasks_are_inspected() -> None:
    """A pending task with no claim is not inspected (state filter honored)."""
    store = _make_store()
    now = datetime(2026, 4, 24, 12, 0, tzinfo=UTC)
    store.create_plan_task("t-pending")
    expired = now - timedelta(seconds=1)
    _claim_with_expiry(store, "t-claimed", worker_id="ui-1", expires_at=expired)

    count = sweep_expired_claims(store, now=now)

    assert count == 1
    assert store.read_task("t-pending").state == "pending"
    assert store.read_task("t-claimed").state == "pending"
