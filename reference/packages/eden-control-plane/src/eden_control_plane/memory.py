"""In-memory `ControlPlaneStore` backend.

Thread-safe via a single coarse lock — every public method runs
under `with self._lock`, so composite mutations (acquire-over-expired,
add-to-group cycle check) commit atomically as a unit. Persists
nothing; suitable for unit tests and single-replica ephemeral
deployments.

Mirrors the structural Protocol in `eden_control_plane.store`. A
parallel Postgres backend lives in `postgres.py`.
"""

from __future__ import annotations

import copy
import secrets
import threading
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from eden_contracts import Group, Worker
from eden_storage.errors import (
    CycleDetected,
    InvalidPrecondition,
    NotFound,
    ReservedIdentifier,
)

from ._credentials import (
    DEPLOYMENT_SCOPE_SENTINEL,
    RESERVED_GROUP_NAMES,
    check_credential_hash,
    constant_time_dummy_verify,
    generate_credential_token,
    hash_credential,
    mint_opaque_id,
    validate_display_name,
    validate_group_name,
    validate_member_id,
    validate_worker_name,
)
from .errors import (
    LeaseExpired,
    LeaseHeldByOther,
    LeaseInstanceMismatch,
    LeaseNotHeld,
)
from .models import ExperimentLease, LastKnownState, RegisteredExperiment

__all__ = ["InMemoryControlPlaneStore"]


def _fmt(dt: datetime) -> str:
    """RFC 3339 UTC timestamp with millisecond precision and trailing Z."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _parse_ts(text: str) -> datetime:
    """Parse the RFC 3339 form `_fmt` emits back to a datetime."""
    return datetime.fromisoformat(text)


def _deep(obj: Any) -> Any:
    """Defensive deep copy so callers cannot mutate stored state in-place."""
    return copy.deepcopy(obj)


class InMemoryControlPlaneStore:
    """In-memory control-plane backend.

    All state lives in process-local dicts protected by a single
    `threading.Lock`. The `_clock` injection point lets tests advance
    wall-clock state deterministically for lease-expiration scenarios.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        # Experiment registry
        self._experiments: dict[str, dict[str, Any]] = {}
        # Lease records keyed by experiment_id (at most one per experiment)
        self._leases: dict[str, dict[str, Any]] = {}
        # Secondary index by lease_id for the renew/release path
        self._leases_by_lease_id: dict[str, str] = {}
        # Workers (deployment-scoped)
        self._workers: dict[str, Worker] = {}
        self._worker_credentials: dict[str, str] = {}
        # Groups (deployment-scoped)
        self._groups: dict[str, Group] = {}
        # The clock used for all `now` calls inside the store; tests
        # inject a deterministic clock to exercise expiration paths.
        self._clock = clock or (lambda: datetime.now(UTC))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _now(self) -> datetime:
        return self._clock()

    def _now_str(self) -> str:
        return _fmt(self._now())

    def _build_lease(self, row: dict[str, Any]) -> ExperimentLease:
        return ExperimentLease.model_validate(row)

    def _build_entry(self, row: dict[str, Any]) -> RegisteredExperiment:
        # Codex round 4 MAJOR: only attach a lease when it is still
        # active (`expires_at >= now`). Surfacing an expired-but-not-
        # garbage-collected lease row in `RegisteredExperiment.lease`
        # would let the web-ui (and any other client) treat the
        # experiment as actively-leased — disabling unregister, hiding
        # acquire/release affordances — even though the store-side
        # operations would happily succeed. Mirrors the §2.1
        # "current lease" semantics.
        lease_row = self._active_lease_for(row["experiment_id"])
        lease = self._build_lease(lease_row) if lease_row is not None else None
        return RegisteredExperiment.model_validate({**row, "lease": lease})

    def _active_lease_for(self, experiment_id: str) -> dict[str, Any] | None:
        """Return the lease row if active (`expires_at >= now`), else None."""
        lease = self._leases.get(experiment_id)
        if lease is None:
            return None
        if _parse_ts(lease["expires_at"]) >= self._now():
            return lease
        return None

    # ------------------------------------------------------------------
    # Experiment registry (chapter 11 §2)
    # ------------------------------------------------------------------

    def register_experiment(
        self, config_uri: str, *, name: str | None = None
    ) -> tuple[RegisteredExperiment, bool]:
        # Ids are system-minted, so every call creates a distinct
        # entry — `created` is always True. The validated display
        # name (if any) is stored alongside.
        validated_name = validate_display_name(name) if name is not None else None
        with self._lock:
            experiment_id = mint_opaque_id("exp")
            row: dict[str, Any] = {
                "experiment_id": experiment_id,
                "config_uri": config_uri,
                "created_at": self._now_str(),
                "last_known_state": "running",
            }
            if validated_name is not None:
                row["name"] = validated_name
            self._experiments[experiment_id] = row
            return self._build_entry(row), True

    def unregister_experiment(self, experiment_id: str) -> None:
        with self._lock:
            row = self._experiments.get(experiment_id)
            if row is None:
                raise NotFound(f"experiment {experiment_id!r}")
            if row["last_known_state"] != "terminated":
                raise InvalidPrecondition(
                    f"experiment {experiment_id!r} is still "
                    f"{row['last_known_state']!r}; terminate first"
                )
            if self._active_lease_for(experiment_id) is not None:
                raise InvalidPrecondition(
                    f"experiment {experiment_id!r} has an active lease; "
                    f"release or wait for expiry before unregistering"
                )
            # Drop any expired-lease row alongside the registry entry so
            # the (lease, registry) state is fully cleaned up.
            stale_lease = self._leases.pop(experiment_id, None)
            if stale_lease is not None:
                self._leases_by_lease_id.pop(stale_lease["lease_id"], None)
            del self._experiments[experiment_id]

    def list_experiments(
        self, *, name: str | None = None
    ) -> list[RegisteredExperiment]:
        with self._lock:
            entries = [
                self._build_entry(row)
                for _, row in sorted(self._experiments.items())
            ]
        return [e for e in entries if name is None or e.name == name]

    def read_experiment_metadata(
        self, experiment_id: str
    ) -> RegisteredExperiment:
        with self._lock:
            row = self._experiments.get(experiment_id)
            if row is None:
                raise NotFound(f"experiment {experiment_id!r}")
            return self._build_entry(row)

    def update_last_known_state(
        self, experiment_id: str, last_known_state: LastKnownState
    ) -> RegisteredExperiment:
        with self._lock:
            row = self._experiments.get(experiment_id)
            if row is None:
                raise NotFound(f"experiment {experiment_id!r}")
            row["last_known_state"] = last_known_state
            return self._build_entry(row)

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
        with self._lock:
            if experiment_id not in self._experiments:
                raise NotFound(f"experiment {experiment_id!r}")
            now = self._now()
            existing = self._leases.get(experiment_id)
            if existing is not None and _parse_ts(existing["expires_at"]) >= now:
                # Active lease: reject. Even when the caller's
                # (holder, holder_instance) matches, the chapter 11 §4.5
                # contract says acquire MUST succeed only on no-lease or
                # expired-lease — duplicate acquire by the same caller
                # is a client bug we surface.
                raise LeaseHeldByOther(
                    f"experiment {experiment_id!r} has an active lease "
                    f"held by worker_id={existing['holder']!r}"
                )
            # Replace any expired predecessor cleanly.
            if existing is not None:
                self._leases_by_lease_id.pop(existing["lease_id"], None)
            acquired_at_str = _fmt(now)
            expires_at_str = _fmt(now + timedelta(seconds=lease_duration_seconds))
            row = {
                "lease_id": "lease-" + secrets.token_hex(16),
                "experiment_id": experiment_id,
                "holder": holder,
                "holder_instance": holder_instance,
                "acquired_at": acquired_at_str,
                "expires_at": expires_at_str,
                "renewed_at": acquired_at_str,
            }
            self._leases[experiment_id] = row
            self._leases_by_lease_id[row["lease_id"]] = experiment_id
            return self._build_lease(row)

    def renew_lease(
        self,
        lease_id: str,
        holder_instance: str,
        *,
        lease_duration_seconds: int,
    ) -> ExperimentLease:
        with self._lock:
            experiment_id = self._leases_by_lease_id.get(lease_id)
            if experiment_id is None:
                raise LeaseNotHeld(
                    f"lease {lease_id!r} has been replaced or never existed"
                )
            row = self._leases.get(experiment_id)
            if row is None or row["lease_id"] != lease_id:
                raise LeaseNotHeld(
                    f"lease {lease_id!r} has been replaced by a fresh acquire"
                )
            if row["holder_instance"] != holder_instance:
                raise LeaseInstanceMismatch(
                    f"lease {lease_id!r} stored holder_instance does not "
                    f"match the caller's"
                )
            now = self._now()
            if _parse_ts(row["expires_at"]) < now:
                raise LeaseExpired(
                    f"lease {lease_id!r} expired at {row['expires_at']!r}; "
                    f"reacquire instead of renew"
                )
            row["expires_at"] = _fmt(now + timedelta(seconds=lease_duration_seconds))
            row["renewed_at"] = _fmt(now)
            return self._build_lease(row)

    def release_lease(self, lease_id: str, holder_instance: str) -> None:
        with self._lock:
            experiment_id = self._leases_by_lease_id.get(lease_id)
            if experiment_id is None:
                # Idempotent on already-released / never-existed lease.
                return
            row = self._leases.get(experiment_id)
            if row is None or row["lease_id"] != lease_id:
                return
            if row["holder_instance"] != holder_instance:
                raise LeaseInstanceMismatch(
                    f"lease {lease_id!r} stored holder_instance does not "
                    f"match the caller's"
                )
            del self._leases[experiment_id]
            self._leases_by_lease_id.pop(lease_id, None)

    def list_active_leases(self, holder: str) -> list[ExperimentLease]:
        with self._lock:
            now = self._now()
            out: list[ExperimentLease] = []
            for row in self._leases.values():
                if row["holder"] != holder:
                    continue
                if _parse_ts(row["expires_at"]) < now:
                    continue
                out.append(self._build_lease(row))
            return sorted(out, key=lambda lease: lease.experiment_id)

    def read_lease(self, lease_id: str) -> ExperimentLease:
        with self._lock:
            experiment_id = self._leases_by_lease_id.get(lease_id)
            if experiment_id is None:
                raise NotFound(f"lease {lease_id!r}")
            return self._build_lease(self._leases[experiment_id])

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
        validated_name = validate_worker_name(name) if name is not None else None
        with self._lock:
            worker_id = mint_opaque_id("wkr")
            token = generate_credential_token()
            data: dict[str, Any] = {
                "worker_id": worker_id,
                # Deployment-scoped workers have no per-experiment binding;
                # the model's `experiment_id` field is set to the deployment
                # sentinel to satisfy the §1.6 `exp_*` grammar while making
                # the deployment scope visible to clients per chapter 11 §6.
                "experiment_id": DEPLOYMENT_SCOPE_SENTINEL,
                "registered_at": self._now_str(),
            }
            if validated_name is not None:
                data["name"] = validated_name
            if registered_by is not None:
                data["registered_by"] = registered_by
            if labels:
                data["labels"] = dict(labels)
            worker = Worker.model_validate(data)
            self._workers[worker_id] = worker
            self._worker_credentials[worker_id] = hash_credential(token)
            return (_deep(worker), token)

    def reissue_credential(self, worker_id: str) -> str:
        with self._lock:
            if worker_id not in self._workers:
                raise NotFound(f"worker {worker_id!r}")
            token = generate_credential_token()
            self._worker_credentials[worker_id] = hash_credential(token)
            return token

    def verify_worker_credential(
        self, worker_id: str, registration_token: str
    ) -> bool:
        with self._lock:
            stored = self._worker_credentials.get(worker_id)
            if stored is None:
                constant_time_dummy_verify(registration_token)
                return False
            return check_credential_hash(registration_token, stored)

    def read_worker(self, worker_id: str) -> Worker:
        with self._lock:
            worker = self._workers.get(worker_id)
            if worker is None:
                raise NotFound(f"worker {worker_id!r}")
            return _deep(worker)

    def list_workers(self, *, name: str | None = None) -> list[Worker]:
        with self._lock:
            return [
                _deep(w)
                for _, w in sorted(self._workers.items())
                if name is None or w.name == name
            ]

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
        validated_name = (
            validate_group_name(name, allow_reserved=allow_reserved)
            if name is not None
            else None
        )
        members_list = list(members) if members is not None else []
        for m in members_list:
            validate_member_id(m)
        with self._lock:
            # §7.5 / §11 §6: a reserved group name is taken once it has been
            # minted — a second create (even via the privileged
            # `allow_reserved` admin path) is rejected so exactly one
            # `grp_*` ever carries the reserved name.
            if (
                allow_reserved
                and validated_name in RESERVED_GROUP_NAMES
                and any(g.name == validated_name for g in self._groups.values())
            ):
                raise ReservedIdentifier(
                    f"group name {validated_name!r} is reserved and already exists"
                )
            group_id = mint_opaque_id("grp")
            # Cycle check: the group plus its members MUST NOT close a
            # cycle through any pre-existing group. Easy here since
            # `group_id` is freshly minted; just check transitively that
            # none of the listed group-members point back at `group_id`.
            for m in members_list:
                if m in self._groups and self._would_close_cycle(m, group_id):
                    raise CycleDetected(
                        f"adding {m!r} to {group_id!r} closes a cycle"
                    )
            data: dict[str, Any] = {
                "group_id": group_id,
                "experiment_id": DEPLOYMENT_SCOPE_SENTINEL,
                "members": members_list,
                "created_at": self._now_str(),
            }
            if validated_name is not None:
                data["name"] = validated_name
            if created_by is not None:
                data["created_by"] = created_by
            group = Group.model_validate(data)
            self._groups[group_id] = group
            return _deep(group)

    def _would_close_cycle(self, start: str, banned: str) -> bool:
        """Return True iff a DFS from `start` reaches `banned` through groups."""
        visited: set[str] = set()
        stack = [start]
        while stack:
            cur = stack.pop()
            if cur in visited:
                continue
            visited.add(cur)
            group = self._groups.get(cur)
            if group is None:
                continue
            for m in group.members:
                if m == banned:
                    return True
                if m in self._groups and m not in visited:
                    stack.append(m)
        return False

    def add_to_group(self, group_id: str, member_id: str) -> Group:
        validate_member_id(member_id)
        with self._lock:
            group = self._groups.get(group_id)
            if group is None:
                raise NotFound(f"group {group_id!r}")
            if member_id in group.members:
                return _deep(group)
            # Cycle check: if `member_id` is a group that transitively
            # contains `group_id`, adding it would close a cycle.
            if member_id in self._groups and self._would_close_cycle(
                member_id, group_id
            ):
                raise CycleDetected(
                    f"adding {member_id!r} to {group_id!r} closes a cycle"
                )
            new_members = list(group.members) + [member_id]
            updated = group.model_copy(update={"members": new_members})
            self._groups[group_id] = updated
            return _deep(updated)

    def remove_from_group(self, group_id: str, member_id: str) -> Group:
        with self._lock:
            group = self._groups.get(group_id)
            if group is None:
                raise NotFound(f"group {group_id!r}")
            if member_id not in group.members:
                return _deep(group)
            new_members = [m for m in group.members if m != member_id]
            updated = group.model_copy(update={"members": new_members})
            self._groups[group_id] = updated
            return _deep(updated)

    def delete_group(self, group_id: str) -> None:
        with self._lock:
            if group_id not in self._groups:
                raise NotFound(f"group {group_id!r}")
            del self._groups[group_id]

    def read_group(self, group_id: str) -> Group:
        with self._lock:
            group = self._groups.get(group_id)
            if group is None:
                raise NotFound(f"group {group_id!r}")
            return _deep(group)

    def list_groups(self, *, name: str | None = None) -> list[Group]:
        with self._lock:
            return [
                _deep(g)
                for _, g in sorted(self._groups.items())
                if name is None or g.name == name
            ]

    def resolve_worker_in_group(self, worker_id: str, group_id: str) -> bool:
        with self._lock:
            visited: set[str] = set()
            stack = [group_id]
            while stack:
                cur = stack.pop()
                if cur in visited:
                    continue
                visited.add(cur)
                group = self._groups.get(cur)
                if group is None:
                    continue
                if worker_id in group.members:
                    return True
                for m in group.members:
                    if m in self._groups and m not in visited:
                        stack.append(m)
            return False
