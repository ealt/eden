"""LeaseManager — chapter 11 §5 orchestrator interaction with control plane.

Encapsulates the lease lifecycle for one orchestrator replica:

- §5.2 startup duplicate-`worker_id` probe.
- Acquire leases for every registered experiment we can.
- Renew held leases at every iteration boundary.
- §5.3 self-fence: if the control plane is unreachable for more than
  `lease_duration_seconds`, drop all held leases so the holder MUST
  stop dispatching against any experiment.
- §5.5 release-after-drain: ordered release on graceful shutdown,
  driven by the caller after draining in-flight integrations.

Designed to slot inside the orchestrator's main loop: each iteration
calls `refresh()` to acquire/renew/expire leases against the control
plane, then iterates the `held_experiments` snapshot to drive the
per-experiment work loop.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from eden_control_plane import (
    ControlPlaneClient,
    ExperimentLease,
    LeaseError,
    LeaseExpired,
    LeaseHeldByOther,
    LeaseInstanceMismatch,
    LeaseNotHeld,
)
from eden_service_common import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

log = get_logger(__name__)


__all__ = [
    "DuplicateWorkerInstance",
    "LeaseManager",
    "LeaseSnapshot",
]


class DuplicateWorkerInstance(RuntimeError):
    """A second live replica is reusing this `worker_id`.

    Per chapter 11 §5.2, an orchestrator MUST refuse to start when its
    startup probe observes an active lease held by `self.worker_id`
    with a different `holder_instance`. Raised by
    `LeaseManager.startup_probe` so the CLI can exit non-zero.
    """


@dataclass
class LeaseSnapshot:
    """A point-in-time snapshot of one held lease.

    Tracks both the lease record returned by the control plane and the
    local `last_successful_renew` timestamp the partition self-fence
    consults.
    """

    lease: ExperimentLease
    last_successful_renew: datetime = field(
        default_factory=lambda: datetime.now(UTC)
    )

    @property
    def experiment_id(self) -> str:
        """Convenience accessor."""
        return self.lease.experiment_id

    @property
    def lease_id(self) -> str:
        """Convenience accessor."""
        return self.lease.lease_id


class LeaseManager:
    """Owner of one replica's per-experiment lease state.

    Constructor inputs:

    - `client` — `ControlPlaneClient` used for every wire call.
    - `worker_id` — this replica's deployment-scoped worker id (per
      chapter 11 §6). Held leases all carry `holder == worker_id`.
    - `lease_duration_seconds` — passed through on every acquire/renew
      so the control plane's `expires_at` is consistent with the
      manager's `lease_duration_seconds`.

    Optional:

    - `holder_instance` — per-process UUID supplied on acquire/renew
      for §4.7 fencing. Defaults to a fresh `uuid4().hex`.
    - `now` — clock injection for tests; defaults to
      `datetime.now(timezone.utc)`.
    """

    def __init__(
        self,
        client: ControlPlaneClient,
        *,
        worker_id: str,
        lease_duration_seconds: int = 30,
        holder_instance: str | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._worker_id = worker_id
        self._lease_duration_seconds = lease_duration_seconds
        self._holder_instance = holder_instance or uuid.uuid4().hex
        self._now = now or (lambda: datetime.now(UTC))
        # `held` is keyed by experiment_id so a per-iteration code path
        # can do `for exp in manager.held_experiments(): …` and have an
        # O(1) lookup of the underlying lease.
        self._held: dict[str, LeaseSnapshot] = {}
        # Per chapter 11 §5.5: once we observe drain-complete for an
        # experiment, skip re-acquiring its lease until the experiment
        # is unregistered (or the orchestrator restarts).
        self._drained_terminated: set[str] = set()
        # §5.3 self-fence accounting: when EVERY held lease's
        # `last_successful_renew` is older than `lease_duration_seconds`,
        # the manager is partitioned from the control plane and MUST drop
        # all leases.
        self._last_successful_control_plane_call: datetime = self._now()

    # ------------------------------------------------------------------
    # Properties (read-only view of state)
    # ------------------------------------------------------------------

    @property
    def worker_id(self) -> str:
        """The deployment-scoped worker_id this manager authenticates as."""
        return self._worker_id

    @property
    def holder_instance(self) -> str:
        """The per-process UUID this manager presents on every lease op."""
        return self._holder_instance

    def held_experiments(self) -> list[str]:
        """Return the experiment_ids the manager currently holds a lease for.

        Sorted for deterministic per-iteration order.
        """
        return sorted(self._held.keys())

    def is_held(self, experiment_id: str) -> bool:
        """Return True iff the manager holds an ACTIVE lease for `experiment_id`.

        Active = present in the held-set AND wall-clock `expires_at`
        has not passed. The per-iteration recheck in
        `multi_loop._run_one_experiment` calls this immediately
        before driving a per-experiment iteration; the expiry-aware
        path is load-bearing under §5.1 — if iteration N-1 blocked
        long enough that lease N's `expires_at` is now in the past,
        another replica may have already acquired it, and running
        N's iteration would violate the chapter 11 §5.1 lease-
        ownership invariant.
        """
        snap = self._held.get(experiment_id)
        if snap is None:
            return False
        try:
            expires_at = _parse_lease_timestamp(snap.lease.expires_at)
        except ValueError:
            # Defensive: corrupt timestamp → treat as not-held.
            return False
        return expires_at >= self._now()

    def lease_for(self, experiment_id: str) -> ExperimentLease | None:
        """Return the lease for `experiment_id` if held, else None."""
        snap = self._held.get(experiment_id)
        return snap.lease if snap is not None else None

    def drained_terminated(self) -> set[str]:
        """Return a copy of the drained-terminated skip set."""
        return set(self._drained_terminated)

    # ------------------------------------------------------------------
    # Startup probe (chapter 11 §5.2)
    # ------------------------------------------------------------------

    def startup_probe(self) -> None:
        """Detect a duplicate `worker_id` per chapter 11 §5.2.

        Calls `list_active_leases(holder=self.worker_id)`. Every
        returned lease whose `holder_instance != self.holder_instance`
        proves another live process is using the same `worker_id` —
        the spec MUST is to refuse to start. Raises
        :class:`DuplicateWorkerInstance` so the CLI can `sys.exit(2)`.

        If the control plane is reachable but has stale leases under
        OUR own `holder_instance` (e.g. a crash-recovery restart with
        a persisted UUID), the probe silently leaves them — those are
        ours and the natural expiration takes care of them. The
        manager's normal acquire path then re-uses or replaces them.
        """
        leases = self._client.list_active_leases(self._worker_id)
        self._last_successful_control_plane_call = self._now()
        offenders = [
            lease
            for lease in leases
            if lease.holder_instance != self._holder_instance
        ]
        if offenders:
            offender_ids = ", ".join(
                f"{lease.experiment_id}({lease.holder_instance[:8]}…)"
                for lease in offenders
            )
            raise DuplicateWorkerInstance(
                f"another live orchestrator is using worker_id="
                f"{self._worker_id!r}; observed active leases: {offender_ids}"
            )

    # ------------------------------------------------------------------
    # Per-iteration refresh (chapter 11 §5.3)
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Renew + acquire leases against the control plane.

        Called once per orchestrator iteration. Three phases:

        1. **Renew held leases.** Each renew that succeeds updates
           `last_successful_renew`. Any failure that is NOT
           transport-indeterminate (`LeaseNotHeld`, `LeaseExpired`,
           `LeaseInstanceMismatch`) drops the experiment from the
           held set — another replica has taken over.
        2. **Acquire leases for unleased experiments.** Iterates the
           control-plane registry and tries `acquire_lease` for
           anything not already held AND not in the
           `drained_terminated` skip set. `LeaseHeldByOther` is the
           expected steady-state outcome and is silently ignored.
        3. **Self-fence check (§5.3).** If the manager has not had a
           successful control-plane call in `lease_duration_seconds`,
           every held lease has effectively expired AND another
           replica has either acquired them or is about to —
           drop the local copies so the holder stops dispatching.

        Transport errors during any wire call are caught and logged;
        the held set is mutated only on errors the spec MUST treats
        as definitive (the four lease error codes), not on generic
        transport failure. The self-fence is the catch-all for
        sustained transport failure.
        """
        self._renew_held_leases()
        self._acquire_unleased_experiments()
        self._self_fence_check()

    def _renew_held_leases(self) -> None:
        """Phase 1 of refresh: renew each held lease via the control plane."""
        for experiment_id in list(self._held.keys()):
            snap = self._held[experiment_id]
            try:
                renewed = self._client.renew_lease(
                    snap.lease_id, self._holder_instance
                )
            except (LeaseNotHeld, LeaseExpired, LeaseInstanceMismatch) as exc:
                log.warning(
                    "lease_lost",
                    experiment_id=experiment_id,
                    lease_id=snap.lease_id,
                    error=type(exc).__name__,
                )
                del self._held[experiment_id]
                continue
            except LeaseError:
                # Unknown lease-class failure — defensive log + drop
                # the experiment so the orchestrator stops dispatching.
                log.exception("renew_lease_unknown_failure")
                del self._held[experiment_id]
                continue
            except Exception:  # noqa: BLE001 — transport / network
                log.warning(
                    "renew_lease_transport_failure",
                    experiment_id=experiment_id,
                )
                # Leave the snapshot in place; the self-fence below
                # eventually drops it if the failure persists.
                continue
            snap.lease = renewed
            snap.last_successful_renew = self._now()
            self._last_successful_control_plane_call = self._now()

    def _acquire_unleased_experiments(self) -> None:
        """Phase 2 of refresh: acquire any experiment not held and not drained."""
        try:
            experiments = self._client.list_experiments()
            self._last_successful_control_plane_call = self._now()
        except Exception:  # noqa: BLE001 — transport / network
            log.warning("list_experiments_transport_failure")
            return
        for entry in experiments:
            experiment_id = entry.experiment_id
            if experiment_id in self._held:
                continue
            if experiment_id in self._drained_terminated:
                continue
            try:
                lease = self._client.acquire_lease(
                    experiment_id, self._worker_id, self._holder_instance
                )
            except LeaseHeldByOther:
                # Steady state for a multi-replica deployment.
                continue
            except LeaseError:
                log.exception(
                    "acquire_lease_unknown_failure",
                    experiment_id=experiment_id,
                )
                continue
            except Exception:  # noqa: BLE001 — transport / network
                log.warning(
                    "acquire_lease_transport_failure",
                    experiment_id=experiment_id,
                )
                continue
            self._last_successful_control_plane_call = self._now()
            self._held[experiment_id] = LeaseSnapshot(
                lease=lease,
                last_successful_renew=self._now(),
            )
            log.info(
                "lease_acquired",
                experiment_id=experiment_id,
                lease_id=lease.lease_id,
            )

    def _self_fence_check(self) -> None:
        """Phase 3 of refresh: drop all held leases if §5.3 self-fence triggers."""
        partition_seconds = (
            self._now() - self._last_successful_control_plane_call
        ).total_seconds()
        if self._held and partition_seconds >= self._lease_duration_seconds:
            log.warning(
                "self_fence_triggered",
                partition_seconds=partition_seconds,
                lease_duration_seconds=self._lease_duration_seconds,
                experiments_dropped=sorted(self._held.keys()),
            )
            self._held.clear()

    # ------------------------------------------------------------------
    # Termination drain (chapter 11 §5.5)
    # ------------------------------------------------------------------

    def mark_drained_terminated(self, experiment_id: str) -> None:
        """Record that `experiment_id` has finished its integration drain.

        Called by the per-experiment work loop once it observes the
        experiment is `terminated` AND no `status="success"` variants
        without `variant_commit_sha` remain. The manager:

        - Releases the lease via `release_lease`.
        - Removes the experiment from the held set.
        - Adds it to the drained-terminated skip set so subsequent
          `refresh()` calls do NOT re-acquire it.

        Transport / lease errors during release are logged but not
        re-raised — the natural lease expiration is the backstop.
        """
        snap = self._held.pop(experiment_id, None)
        self._drained_terminated.add(experiment_id)
        if snap is None:
            return
        try:
            self._client.release_lease(snap.lease_id, self._holder_instance)
        except Exception:  # noqa: BLE001 — best-effort release
            log.warning(
                "release_after_drain_failed",
                experiment_id=experiment_id,
                lease_id=snap.lease_id,
            )

    # ------------------------------------------------------------------
    # Per-iteration release (chapter 11 §5.1 — bounded blackhole)
    # ------------------------------------------------------------------

    def release_for(self, experiment_id: str) -> None:
        """Release `experiment_id`'s lease without marking it drained.

        Codex round 4 MAJOR. Distinct from `mark_drained_terminated`:
        this path is for cases where the orchestrator currently holds
        a lease but discovered it cannot actually do task-store work
        for the experiment (e.g. per-experiment credential bootstrap
        failed in `multi_loop.run_multi_experiment_loop`'s factory
        call). Holding the lease through the renew cadence would
        blackhole the experiment until lease expiry; releasing it
        immediately lets another replica attempt.

        The experiment is NOT added to `_drained_terminated`, so the
        next `refresh()` MAY re-acquire — by then the transient
        failure may be resolved (operator dropped a fresh credential,
        admin token came online, etc.). Transport / lease errors
        during release are logged but not re-raised.
        """
        snap = self._held.pop(experiment_id, None)
        if snap is None:
            return
        try:
            self._client.release_lease(snap.lease_id, self._holder_instance)
        except Exception:  # noqa: BLE001 — best-effort
            log.warning(
                "release_for_failed",
                experiment_id=experiment_id,
                lease_id=snap.lease_id,
            )

    # ------------------------------------------------------------------
    # Shutdown (chapter 11 §5.5 second half — graceful release)
    # ------------------------------------------------------------------

    def release_all(self) -> None:
        """Release every held lease.

        Called from the orchestrator's shutdown path AFTER any
        in-flight per-experiment integration drain has completed.
        Failures are best-effort logged; the natural lease expiry
        is the backstop. Mutates `held` to empty.
        """
        for experiment_id in list(self._held.keys()):
            snap = self._held.pop(experiment_id)
            try:
                self._client.release_lease(
                    snap.lease_id, self._holder_instance
                )
            except Exception:  # noqa: BLE001 — best-effort
                log.warning(
                    "release_all_failed",
                    experiment_id=experiment_id,
                    lease_id=snap.lease_id,
                )

    # ------------------------------------------------------------------
    # Test hooks (not normative — exposed for unit tests only)
    # ------------------------------------------------------------------

    def _force_partition_marker(self, when: datetime) -> None:
        """Test helper: pin `last_successful_control_plane_call`."""
        self._last_successful_control_plane_call = when


def _parse_lease_timestamp(text: str) -> datetime:
    """Parse a chapter 11 §4.2 lease timestamp (RFC 3339 UTC with trailing Z).

    `ExperimentLease.expires_at` is a string in the
    `YYYY-MM-DDThh:mm:ss[.fff]Z` shape (mirrors
    `spec/v0/schemas/lease.schema.json`'s pattern). Python's
    `datetime.fromisoformat` accepts that shape directly in 3.11+;
    the wrapper exists so callers raise a typed `ValueError` on
    malformed input rather than dealing with `fromisoformat`'s
    error vocabulary.
    """
    return datetime.fromisoformat(text)


def utcnow() -> datetime:
    """Convenience wrapper around `datetime.now(timezone.utc)`."""
    return datetime.now(UTC)


def monotonic_seconds() -> float:
    """Monotonic seconds counter used by transport-loss tracking."""
    return time.monotonic()
