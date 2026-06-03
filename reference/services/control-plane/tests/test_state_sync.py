"""Tests for the chapter 11 §3 state-sync poller.

Drives the poller synchronously via `tick()` + `refresh_one()` so
the failure-threshold + on-demand-refresh semantics can be exercised
without spawning the background thread.
"""
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

import pytest
from eden_control_plane import InMemoryControlPlaneStore
from eden_control_plane_server.state_sync import StateSyncPoller


@pytest.fixture
def store() -> InMemoryControlPlaneStore:
    """Store with two registered experiments.

    Since #128 ``register_experiment`` MINTS the opaque ``exp_*`` id;
    the minted ids are stashed on the store as ``exp1`` / ``exp2``
    attributes so the tests can reference them without restructuring
    every signature.
    """
    s = InMemoryControlPlaneStore()
    entry1, _ = s.register_experiment("https://x.test/a.yaml")
    entry2, _ = s.register_experiment("https://x.test/b.yaml")
    # Stash the minted ids for the tests (attribute access on the
    # in-memory store fixture is test-only ergonomics).
    s.exp1 = entry1.experiment_id  # type: ignore[attr-defined]
    s.exp2 = entry2.experiment_id  # type: ignore[attr-defined]
    return s


# ---------------------------------------------------------------------
# tick() mirrors authoritative state
# ---------------------------------------------------------------------


def test_tick_mirrors_running_state(
    store: InMemoryControlPlaneStore,
) -> None:
    states = {store.exp1: "running", store.exp2: "running"}
    poller = StateSyncPoller(store, state_reader=lambda eid: states[eid])
    poller.tick()
    assert store.read_experiment_metadata(store.exp1).last_known_state == "running"
    assert store.read_experiment_metadata(store.exp2).last_known_state == "running"


def test_tick_mirrors_transition_to_terminated(
    store: InMemoryControlPlaneStore,
) -> None:
    states = {store.exp1: "running", store.exp2: "terminated"}
    poller = StateSyncPoller(store, state_reader=lambda eid: states[eid])
    poller.tick()
    assert (
        store.read_experiment_metadata(store.exp2).last_known_state == "terminated"
    )


# ---------------------------------------------------------------------
# Failure handling + §3.4 warnings
# ---------------------------------------------------------------------


def test_read_failure_does_not_overwrite_state(
    store: InMemoryControlPlaneStore,
) -> None:
    """A reader exception MUST leave the registry's last_known_state intact."""
    # Seed exp-1 as terminated so we can tell the registry wasn't
    # silently reset to running.
    store.update_last_known_state(store.exp1, "terminated")

    def _reader(_eid: str) -> str:
        raise RuntimeError("simulated transport failure")

    poller = StateSyncPoller(store, state_reader=_reader, failure_threshold=10)
    poller.tick()
    assert (
        store.read_experiment_metadata(store.exp1).last_known_state == "terminated"
    )


def test_unknown_state_is_rejected(
    store: InMemoryControlPlaneStore,
) -> None:
    """A reader returning a non-spec value MUST be treated as failure."""

    def _reader(_eid: str) -> str:
        return "garbage"

    poller = StateSyncPoller(store, state_reader=_reader, failure_threshold=10)
    poller.tick()
    # The registry's `last_known_state` is unchanged (still "running"
    # from registration).
    assert store.read_experiment_metadata(store.exp1).last_known_state == "running"
    # And the failure counter incremented.
    assert store.exp1 in {eid for eid in (poller.warnings._records or {})}  # noqa: SLF001


def test_warning_kicks_in_after_threshold(
    store: InMemoryControlPlaneStore,
) -> None:
    """§3.4: warnings_for() empty below threshold, populated at + above."""

    def _reader(_eid: str) -> str:
        raise RuntimeError("always-fails")

    poller = StateSyncPoller(store, state_reader=_reader, failure_threshold=3)
    # First two failures: no warning.
    poller.refresh_one(store.exp1)
    poller.refresh_one(store.exp1)
    assert poller.warnings.warnings_for(store.exp1) == []
    # Third failure crosses the threshold.
    poller.refresh_one(store.exp1)
    warnings = poller.warnings.warnings_for(store.exp1)
    assert len(warnings) == 1
    assert "state-sync-stale" in warnings[0]
    assert "3 consecutive failures" in warnings[0]


def test_warning_clears_on_success(
    store: InMemoryControlPlaneStore,
) -> None:
    """A successful read MUST reset the failure counter to zero."""
    call_count = {"n": 0}

    def _reader(eid: str) -> str:
        call_count["n"] += 1
        if call_count["n"] <= 3:
            raise RuntimeError("transient failure")
        return "running"

    poller = StateSyncPoller(store, state_reader=_reader, failure_threshold=3)
    for _ in range(3):
        poller.refresh_one(store.exp1)
    assert poller.warnings.warnings_for(store.exp1)  # warning active

    # Next read succeeds → counter resets → warning clears.
    poller.refresh_one(store.exp1)
    assert poller.warnings.warnings_for(store.exp1) == []


# ---------------------------------------------------------------------
# refresh_one() — §3.3 on-demand
# ---------------------------------------------------------------------


def test_refresh_one_returns_state_on_success(
    store: InMemoryControlPlaneStore,
) -> None:
    poller = StateSyncPoller(
        store, state_reader=lambda _eid: "terminated"
    )
    result = poller.refresh_one(store.exp1)
    assert result == "terminated"
    assert (
        store.read_experiment_metadata(store.exp1).last_known_state == "terminated"
    )


def test_refresh_one_returns_none_on_failure(
    store: InMemoryControlPlaneStore,
) -> None:
    def _reader(_eid: str) -> str:
        raise RuntimeError("boom")

    poller = StateSyncPoller(store, state_reader=_reader)
    assert poller.refresh_one(store.exp1) is None


# ---------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------


def test_start_stop_lifecycle(
    store: InMemoryControlPlaneStore,
) -> None:
    """Daemon thread starts + stops cleanly."""
    poller = StateSyncPoller(
        store,
        state_reader=lambda _eid: "running",
        interval_seconds=0.01,
    )
    assert not poller._is_running()  # noqa: SLF001
    poller.start()
    assert poller._is_running()  # noqa: SLF001
    poller.stop(timeout=1.0)
    assert not poller._is_running()  # noqa: SLF001


def test_start_is_idempotent(
    store: InMemoryControlPlaneStore,
) -> None:
    poller = StateSyncPoller(
        store, state_reader=lambda _eid: "running", interval_seconds=0.1
    )
    poller.start()
    poller.start()  # MUST NOT spawn a second thread
    assert poller._is_running()  # noqa: SLF001
    poller.stop(timeout=1.0)
