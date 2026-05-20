"""Background state-sync poller (chapter 11 §3).

Mirrors each registered experiment's authoritative `experiment.state`
from the task-store-server into the control plane's
`last_known_state` projection. Pull-based; eventually consistent
within one polling interval (default 30s).

Operator-visible warning (§3.4): when a per-experiment consecutive-
failure counter exceeds a threshold (default 10), subsequent
`read_experiment_metadata` responses include a `warnings` entry
naming the staleness. Reset on the next successful read.

Threading: one daemon thread per `StateSyncPoller` instance, started
by `start()` and stopped by `stop()`. The poller does NOT auto-start
on construction — the caller (FastAPI lifespan, test harness) owns
the lifecycle so unit tests can drive `tick()` synchronously.

The §3.3 on-demand refresh — `acquire_lease` triggers a one-shot
state read before returning — is implemented in the server's
acquire-lease route handler; the poller's `refresh_one(...)` method
exposes the same atomic refresh for that path.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from eden_control_plane import ControlPlaneStore
from eden_service_common import get_logger

log = get_logger(__name__)

__all__ = [
    "ExperimentStateReader",
    "StateSyncPoller",
    "WarningsTracker",
]


# Signature the poller calls to read `experiment.state` for one
# experiment_id. Returns the string ("running" | "terminated") on
# success or raises any Exception on transport / storage failure.
ExperimentStateReader = Callable[[str], str]


@dataclass
class _FailureRecord:
    """Per-experiment consecutive-failure accounting for §3.4 warnings."""

    consecutive_failures: int = 0
    last_failure_at_unix: float | None = None
    last_success_at_unix: float | None = None


@dataclass
class WarningsTracker:
    """Tracks consecutive-failure counts per experiment.

    The control plane's `read_experiment_metadata` route consults the
    tracker via `warnings_for(experiment_id)` to inject the §3.4
    warning into the response when the threshold is exceeded.
    """

    failure_threshold: int = 10
    _records: dict[str, _FailureRecord] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record_success(self, experiment_id: str) -> None:
        """Reset the failure counter for `experiment_id`."""
        with self._lock:
            rec = self._records.setdefault(experiment_id, _FailureRecord())
            rec.consecutive_failures = 0
            rec.last_success_at_unix = time.time()

    def record_failure(self, experiment_id: str) -> None:
        """Increment the failure counter for `experiment_id`."""
        with self._lock:
            rec = self._records.setdefault(experiment_id, _FailureRecord())
            rec.consecutive_failures += 1
            rec.last_failure_at_unix = time.time()

    def warnings_for(self, experiment_id: str) -> list[str]:
        """Return the §3.4 `warnings` array for `experiment_id`.

        Empty list when no warning is active. The format is
        operator-readable text; clients SHOULD forward unchanged.
        """
        with self._lock:
            rec = self._records.get(experiment_id)
            if rec is None or rec.consecutive_failures < self.failure_threshold:
                return []
            ts = (
                _fmt_unix(rec.last_success_at_unix)
                if rec.last_success_at_unix is not None
                else "<never>"
            )
            return [
                f"state-sync-stale: {rec.consecutive_failures} consecutive "
                f"failures; last successful read at {ts}"
            ]


def _fmt_unix(ts: float) -> str:
    """Format a unix timestamp as the RFC 3339 UTC form."""
    from datetime import UTC, datetime

    return (
        datetime.fromtimestamp(ts, tz=UTC)
        .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
        + "Z"
    )


class StateSyncPoller:
    """Periodic poller that mirrors per-experiment state into the registry.

    Constructor:

    - `store` — the `ControlPlaneStore` whose `last_known_state` we
      mirror into.
    - `state_reader` — a callable that returns the authoritative state
      ("running" | "terminated") for a given experiment_id. Production
      passes a closure that talks to the task-store-server's
      `read_experiment` wire op; tests pass a fake reader for
      deterministic scenarios.
    - `interval_seconds` — polling interval (chapter 11 §3.2 default 30).
    - `failure_threshold` — consecutive-failure count before the §3.4
      warning kicks in (default 10).

    Lifecycle: `start()` spawns the daemon thread; `stop()` signals
    shutdown and joins (with a small bounded wait). The poller is
    re-entrant on `tick()` — tests can drive ticks synchronously
    without ever calling `start()`.
    """

    def __init__(
        self,
        store: ControlPlaneStore,
        *,
        state_reader: ExperimentStateReader,
        interval_seconds: float = 30.0,
        failure_threshold: int = 10,
    ) -> None:
        self._store = store
        self._read_state = state_reader
        self._interval = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.warnings = WarningsTracker(failure_threshold=failure_threshold)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the daemon polling thread. Idempotent on already-started."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        thread = threading.Thread(
            target=self._run, name="control-plane-state-sync", daemon=True
        )
        thread.start()
        self._thread = thread

    def stop(self, *, timeout: float = 5.0) -> None:
        """Signal shutdown + join the daemon thread. Idempotent."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 — defensive at thread boundary
                log.exception("state_sync_tick_unhandled_failure")
            if self._stop.wait(self._interval):
                return

    # ------------------------------------------------------------------
    # Public sync surface (tick + on-demand refresh)
    # ------------------------------------------------------------------

    def tick(self) -> None:
        """Run one polling pass over every registered experiment."""
        entries = self._store.list_experiments()
        for entry in entries:
            self.refresh_one(entry.experiment_id)

    def refresh_one(self, experiment_id: str) -> str | None:
        """Read the authoritative state for `experiment_id` + mirror it.

        Returns the new `last_known_state` value on success, or None
        on read failure. Used by the §3.3 on-demand refresh path in
        `acquire_lease` (the server-side handler calls this AFTER
        the lease commit so a freshly-leased experiment has
        up-to-date state).
        """
        try:
            state = self._read_state(experiment_id)
        except Exception:  # noqa: BLE001 — record + return None
            self.warnings.record_failure(experiment_id)
            log.warning("state_sync_read_failed", experiment_id=experiment_id)
            return None
        # Normalize: the authoritative state is the string the reader
        # returned. Reject unknown values so a buggy reader cannot
        # poison the registry.
        if state not in ("running", "terminated"):
            self.warnings.record_failure(experiment_id)
            log.warning(
                "state_sync_unknown_state",
                experiment_id=experiment_id,
                state=state,
            )
            return None
        # Codex round 3 MAJOR: record_success MUST run only after the
        # store-side write commits. Otherwise a sequence of read-success +
        # store-write-fail resets the counter and suppresses the §3.4
        # stale-warning even though `last_known_state` is increasingly
        # stale.
        try:
            self._store.update_last_known_state(experiment_id, state)
        except Exception:  # noqa: BLE001 — treat as a sync failure
            self.warnings.record_failure(experiment_id)
            log.warning(
                "state_sync_store_update_failed",
                experiment_id=experiment_id,
            )
            return None
        self.warnings.record_success(experiment_id)
        return state

    # ------------------------------------------------------------------
    # Test hooks
    # ------------------------------------------------------------------

    def _is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


def make_task_store_reader(
    task_store_url: str,
    *,
    admin_bearer: str | None = None,
    timeout: float = 10.0,
) -> ExperimentStateReader:
    """Build an `ExperimentStateReader` backed by the task-store-server.

    Uses `eden_wire.StoreClient.read_experiment` to fetch the
    authoritative `experiment.state`. The reader constructs (and
    closes) a fresh client per call — chapter 11 §3.2 only requires
    eventual consistency, and a single-replica control plane's
    polling rate is too low for connection pooling to matter.
    """
    from eden_wire import StoreClient

    def _reader(experiment_id: str) -> str:
        with StoreClient(
            task_store_url, experiment_id, bearer=admin_bearer, timeout=timeout
        ) as client:
            experiment = client.read_experiment()
            return experiment.state

    return _reader
