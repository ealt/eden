"""`ControlPlaneStore` — structural Protocol for control-plane storage.

Mirrors `eden_storage.Store`'s Protocol pattern. A conforming backend
implements the experiment registry + lease + deployment-scoped
worker/group registry operations from `spec/v0/11-control-plane.md`
§§2, 4, 6. Backends do not subclass the Protocol; matching the method
signatures is enough.

Reference backends:

- `eden_control_plane.memory.InMemoryControlPlaneStore` — thread-safe
  in-memory backend; suitable for unit tests and single-replica
  ephemeral deployments.
- `eden_control_plane.postgres.PostgresControlPlaneStore` — Postgres-
  backed; the production-shaped backend per chapter 11 §4 / plan §3.4
  Option A.

Operations:

- **Experiment registry** (§2): `register_experiment`, `unregister_experiment`,
  `list_experiments`, `read_experiment_metadata`, `update_last_known_state`.
- **Leases** (§4): `acquire_lease`, `renew_lease`, `release_lease`,
  `list_active_leases`, `read_lease`.
- **Deployment-scoped workers** (§6): `register_worker`, `reissue_credential`,
  `verify_worker_credential`, `read_worker`, `list_workers`.
- **Deployment-scoped groups** (§6): `register_group`, `add_to_group`,
  `remove_from_group`, `delete_group`, `read_group`, `list_groups`,
  `resolve_worker_in_group`.

Every operation is atomic under chapter 11 §4.6: composite state
changes (e.g. `acquire_lease` replacing an expired lease record)
commit together or not at all. The Protocol does not cover constructor
signatures — they vary per backend.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from eden_contracts import Group, Worker

from .models import ExperimentLease, LastKnownState, RegisteredExperiment

__all__ = ["ControlPlaneStore"]


class ControlPlaneStore(Protocol):
    """Structural interface every control-plane backend satisfies.

    A backend MUST raise the error types from
    `eden_storage.errors` (re-used so the wire dispatch in
    `eden_control_plane.errors` can map them consistently with the
    per-experiment surface) plus the lease errors from
    `eden_control_plane.errors`:

    - `NotFound` — referenced entity does not exist.
    - `AlreadyExists` — insert collided with an existing id (reserved
      for backend-internal integrity; ids are system-minted so caller
      collisions no longer occur).
    - `InvalidPrecondition` — referenced entity not in required state
      (`unregister_experiment` while the experiment is still running
      OR an active lease exists), or an ill-formed display `name`
      (until `eden_storage.errors.InvalidName` lands — see
      `_credentials.validate_display_name`).
    - `ReservedIdentifier` — `register_worker` / `register_group`
      against a reserved NAME (`admin` / `system` / `internal` for
      workers; `admins` / `orchestrators` for groups).
    - `CycleDetected` — group mutation would introduce a cycle.
    - `LeaseHeldByOther` — `acquire_lease` against a still-active lease.
    - `LeaseNotHeld` — `renew_lease` / `release_lease` against a
      `lease_id` that has been replaced.
    - `LeaseExpired` — `renew_lease` against an expired-but-not-yet-
      replaced lease.
    - `LeaseInstanceMismatch` — body `holder_instance` does not match
      the stored value per §4.7.
    """

    # ------------------------------------------------------------------
    # Experiment registry (chapter 11 §2)
    # ------------------------------------------------------------------

    def register_experiment(
        self, config_uri: str, *, name: str | None = None
    ) -> tuple[RegisteredExperiment, bool]:
        """Mint a fresh `exp_*` and create its registry entry.

        The id is system-minted (`mint_opaque_id("exp")`); the caller
        does NOT supply one (chapter 11 §2 / data-model §1.6). `name`
        is an optional operator-supplied display label (data-model
        §1.7); an ill-formed name MUST be rejected. New entries have
        `created_at = now`, `last_known_state = "running"`,
        `lease = None`.

        Because the id is minted, every call creates a distinct entry —
        there is no idempotent re-registration by id (the pre-rename
        caller-supplied-id idempotency is retired). The returned
        `created` flag is therefore always `True`; the tuple shape is
        retained so the wire layer can keep emitting `201` uniformly.
        """
        ...

    def unregister_experiment(self, experiment_id: str) -> None:
        """Remove the registry entry for `experiment_id`.

        MUST raise `InvalidPrecondition` when `last_known_state !=
        "terminated"` OR an active lease exists. MUST raise
        `NotFound` for an unknown experiment_id.
        """
        ...

    def list_experiments(
        self, *, name: str | None = None
    ) -> list[RegisteredExperiment]:
        """Return every registered experiment, sorted by `experiment_id`.

        When `name` is supplied, filter to entries whose display `name`
        matches it exactly (case-sensitive). Mirrors the `name=` filter
        on `list_workers` / `list_groups`.
        """
        ...

    def read_experiment_metadata(self, experiment_id: str) -> RegisteredExperiment:
        """Return one registry entry. Raises `NotFound` when unknown."""
        ...

    def update_last_known_state(
        self, experiment_id: str, last_known_state: LastKnownState
    ) -> RegisteredExperiment:
        """Atomically write `last_known_state` for `experiment_id`.

        Used by the state-sync poller (chapter 11 §3.2) and by
        `acquire_lease`'s on-demand refresh (§3.3). Returns the
        updated registry entry. Raises `NotFound` when unknown.
        """
        ...

    # ------------------------------------------------------------------
    # Leases (chapter 11 §4)
    # ------------------------------------------------------------------

    def acquire_lease(
        self,
        experiment_id: str,
        holder: str,
        holder_instance: str,
        *,
        lease_duration_seconds: int,
    ) -> ExperimentLease:
        """Acquire (or replace-expired) the lease for `experiment_id`.

        Succeeds when no lease exists OR the existing lease's
        `expires_at < now` (the replace-over-expired path).
        Raises `LeaseHeldByOther` (409) when an active lease exists
        with a different holder/holder_instance. Raises `NotFound`
        when the experiment is not registered.

        The new lease's `acquired_at` is the backend's `now`;
        `expires_at = acquired_at + lease_duration_seconds`;
        `renewed_at = acquired_at`. `lease_id` is opaque (the
        reference impl uses a hex token).

        Concurrency: the backend MUST serialize concurrent
        `acquire_lease` calls against the same experiment such that
        exactly one wins (chapter 11 §4.6).
        """
        ...

    def renew_lease(
        self,
        lease_id: str,
        holder_instance: str,
        *,
        lease_duration_seconds: int,
    ) -> ExperimentLease:
        """Extend the lease's `expires_at` to `now + lease_duration_seconds`.

        Raises `LeaseNotHeld` when the stored `lease_id` no longer
        matches (replacement happened). Raises `LeaseExpired` when
        the lease exists but `expires_at < now` and no replacement
        has happened yet. Raises `LeaseInstanceMismatch` when the
        stored `holder_instance` differs from the caller's
        (chapter 11 §4.7).
        """
        ...

    def release_lease(self, lease_id: str, holder_instance: str) -> None:
        """Delete the lease record.

        Idempotent on already-released lease (a `lease_id` not found
        in storage MUST return without raising). Raises
        `LeaseInstanceMismatch` on stored `holder_instance` mismatch.
        """
        ...

    def list_active_leases(self, holder: str) -> list[ExperimentLease]:
        """Return every active lease whose `holder == holder`.

        "Active" means `expires_at >= now`. Used by the orchestrator's
        chapter 11 §5.2 startup duplicate-`worker_id` probe.
        """
        ...

    def read_lease(self, lease_id: str) -> ExperimentLease:
        """Return one lease record. Raises `NotFound` when unknown.

        Intended for diagnostic / admin endpoints; not part of the
        normative §15.2 wire surface (no GET /leases/{L} endpoint
        exists). Backends MAY omit this method only if they expose
        no diagnostic surface; the reference backends implement it.
        """
        ...

    # ------------------------------------------------------------------
    # Deployment-scoped workers (chapter 11 §6)
    # ------------------------------------------------------------------

    def register_worker(
        self,
        name: str | None = None,
        *,
        labels: dict[str, str] | None = None,
        registered_by: str | None = None,
    ) -> tuple[Worker, str | None]:
        """Mint a fresh `wkr_*` at the deployment scope.

        The id is system-minted (`mint_opaque_id("wkr")`); the caller
        supplies only an optional display `name`. Returns
        `(worker, registration_token)` — every call mints a fresh
        worker and a fresh plaintext token (no id-based idempotency;
        names MAY collide). Raises `ReservedIdentifier` when `name` is
        one of the reserved worker names (`admin` / `system` /
        `internal`); an ill-formed `name` is rejected by the
        display-name validator.
        """
        ...

    def reissue_credential(self, worker_id: str) -> str:
        """Mint a fresh credential; invalidates the prior one.

        Returns the new plaintext token. Raises `NotFound` if
        `worker_id` is not registered.
        """
        ...

    def verify_worker_credential(
        self, worker_id: str, registration_token: str
    ) -> bool:
        """Return True iff `registration_token` matches the stored hash.

        Constant-time defence on the unknown-worker branch so an
        attacker cannot distinguish "no such worker" from "wrong
        secret" via timing.
        """
        ...

    def read_worker(self, worker_id: str) -> Worker:
        """Return the wire-visible Worker, or raise `NotFound`."""
        ...

    def list_workers(self, *, name: str | None = None) -> list[Worker]:
        """Return registered workers (sorted by `worker_id`).

        When `name` is supplied, return only workers whose display
        `name` matches exactly (case-sensitive); 0..N results. Default
        (None) returns all workers.
        """
        ...

    # ------------------------------------------------------------------
    # Deployment-scoped groups (chapter 11 §6)
    # ------------------------------------------------------------------

    def register_group(
        self,
        name: str | None = None,
        *,
        members: Iterable[str] | None = None,
        created_by: str | None = None,
        allow_reserved: bool = False,
    ) -> Group:
        """Mint a fresh `grp_*` with optional initial members.

        The id is system-minted (`mint_opaque_id("grp")`); the caller
        supplies only an optional display `name`. Every call mints a
        fresh group (no id-based idempotency). Raises
        `ReservedIdentifier` when `name` is a reserved group name
        (`admins` / `orchestrators`) UNLESS `allow_reserved=True` —
        the privileged setup-experiment seam that seeds the reserved
        groups. Raises `CycleDetected` if initial members would
        introduce a cycle. Members MUST resolve to real `wkr_*` /
        `grp_*` ids (member-grammar validated).
        """
        ...

    def add_to_group(self, group_id: str, member_id: str) -> Group:
        """Add `member_id` to `group_id`. Idempotent on duplicate add."""
        ...

    def remove_from_group(self, group_id: str, member_id: str) -> Group:
        """Remove `member_id`. Idempotent on non-member."""
        ...

    def delete_group(self, group_id: str) -> None:
        """Delete the group. Raises `NotFound` when unknown."""
        ...

    def read_group(self, group_id: str) -> Group:
        """Return one group. Raises `NotFound` when unknown."""
        ...

    def list_groups(self, *, name: str | None = None) -> list[Group]:
        """Return groups (sorted by `group_id`).

        When `name` is supplied, return only groups whose display
        `name` matches exactly (case-sensitive); 0..N results. Default
        (None) returns all groups.
        """
        ...

    def resolve_worker_in_group(self, worker_id: str, group_id: str) -> bool:
        """Return True iff `worker_id` is a transitive member of `group_id`.

        Handles nested-group membership (chapter 02 §7.2).
        """
        ...
