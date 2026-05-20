"""Postgres-backed `ControlPlaneStore`.

A single Postgres connection backs the store; concurrent calls are
serialized through `threading.RLock` plus per-operation SERIALIZABLE
transactions. The schema lives in tables `control_plane_experiments`,
`control_plane_leases`, `control_plane_workers`, `control_plane_groups`,
`control_plane_group_members` — see `_DDL` below. Migrations are
linear and idempotent (CREATE TABLE IF NOT EXISTS).

Lease atomicity (chapter 11 §4.6): `acquire_lease` uses an
`INSERT … ON CONFLICT (experiment_id) DO UPDATE … WHERE
control_plane_leases.expires_at < EXCLUDED.acquired_at` shape so the
"acquire or replace-expired" decision is one statement under
SERIALIZABLE. The PRIMARY KEY on `experiment_id` enforces the
"at most one lease per experiment" invariant.

Holder-instance fencing (chapter 11 §4.7): every renew/release UPDATE
includes `WHERE holder_instance = %s`; affected-rows == 0 means
mismatch.
"""

from __future__ import annotations

import json
import secrets
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from eden_contracts import Group, Worker
from eden_storage.errors import (
    AlreadyExists,
    CycleDetected,
    InvalidPrecondition,
    NotFound,
)
from psycopg import sql

from ._credentials import (
    check_credential_hash,
    constant_time_dummy_verify,
    generate_credential_token,
    hash_credential,
    validate_registry_id,
)
from .errors import (
    LeaseExpired,
    LeaseHeldByOther,
    LeaseInstanceMismatch,
    LeaseNotHeld,
)
from .models import ExperimentLease, LastKnownState, RegisteredExperiment

__all__ = ["PostgresControlPlaneStore"]


_DDL: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS control_plane_experiments (
        experiment_id text PRIMARY KEY,
        config_uri text NOT NULL,
        created_at timestamptz NOT NULL,
        last_known_state text NOT NULL
            CHECK (last_known_state IN ('running', 'terminated'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS control_plane_leases (
        experiment_id text PRIMARY KEY
            REFERENCES control_plane_experiments(experiment_id)
            ON DELETE CASCADE,
        lease_id text NOT NULL UNIQUE,
        holder text NOT NULL,
        holder_instance text NOT NULL,
        acquired_at timestamptz NOT NULL,
        expires_at timestamptz NOT NULL,
        renewed_at timestamptz NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_leases_expires ON control_plane_leases (expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_leases_holder ON control_plane_leases (holder)",
    """
    CREATE TABLE IF NOT EXISTS control_plane_workers (
        worker_id text PRIMARY KEY,
        registered_at timestamptz NOT NULL,
        registered_by text,
        labels jsonb,
        credential_hash text NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS control_plane_groups (
        group_id text PRIMARY KEY,
        created_at timestamptz NOT NULL,
        created_by text
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS control_plane_group_members (
        group_id text NOT NULL
            REFERENCES control_plane_groups(group_id)
            ON DELETE CASCADE,
        member_id text NOT NULL,
        PRIMARY KEY (group_id, member_id)
    )
    """,
]


def _utc(dt: datetime) -> datetime:
    """Coerce a datetime to a tz-aware UTC datetime."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _fmt(dt: datetime) -> str:
    """RFC 3339 UTC timestamp with millisecond precision and trailing Z."""
    dt = _utc(dt)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class PostgresControlPlaneStore:
    """Postgres-backed control-plane backend.

    A single connection in autocommit mode backs the store; each
    public op opens an explicit `BEGIN ISOLATION LEVEL SERIALIZABLE
    READ WRITE` and COMMITs or ROLLBACKs at the end. Mirrors the
    posture of `eden_storage.PostgresStore`.
    """

    def __init__(
        self,
        dsn: str,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._dsn = dsn
        # autocommit=True + explicit BEGIN/COMMIT per op so we can
        # pin SERIALIZABLE per operation.
        self._conn = psycopg.connect(dsn, autocommit=True)
        self._lock = threading.RLock()
        self._in_txn = False
        self._clock = clock or (lambda: datetime.now(UTC))
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self._conn.cursor() as cur:
            for stmt in _DDL:
                # SQL() wraps a runtime str into a Composable so psycopg's
                # type stub accepts it (the bare-str overload only accepts
                # LiteralString, which iterated list items don't satisfy).
                cur.execute(sql.SQL(stmt))  # type: ignore[arg-type]

    def close(self) -> None:
        """Close the underlying connection."""
        self._conn.close()

    def __enter__(self) -> PostgresControlPlaneStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Atomic-operation context
    # ------------------------------------------------------------------

    @contextmanager
    def _atomic(self) -> Iterator[None]:
        """SERIALIZABLE per-operation transaction. Re-entry safe."""
        with self._lock:
            if self._in_txn:
                yield
                return
            with self._conn.cursor() as cur:
                cur.execute("BEGIN ISOLATION LEVEL SERIALIZABLE READ WRITE")
            self._in_txn = True
            try:
                yield
            except BaseException:
                with self._conn.cursor() as cur:
                    cur.execute("ROLLBACK")
                raise
            else:
                with self._conn.cursor() as cur:
                    cur.execute("COMMIT")
            finally:
                self._in_txn = False

    def _now(self) -> datetime:
        return _utc(self._clock())

    # ------------------------------------------------------------------
    # Row construction
    # ------------------------------------------------------------------

    def _row_to_lease(self, row: tuple[Any, ...]) -> ExperimentLease:
        (
            lease_id,
            experiment_id,
            holder,
            holder_instance,
            acquired_at,
            expires_at,
            renewed_at,
        ) = row
        return ExperimentLease.model_validate(
            {
                "lease_id": lease_id,
                "experiment_id": experiment_id,
                "holder": holder,
                "holder_instance": holder_instance,
                "acquired_at": _fmt(acquired_at),
                "expires_at": _fmt(expires_at),
                "renewed_at": _fmt(renewed_at),
            }
        )

    def _row_to_entry(
        self, row: tuple[Any, ...], lease: ExperimentLease | None
    ) -> RegisteredExperiment:
        experiment_id, config_uri, created_at, last_known_state = row
        return RegisteredExperiment.model_validate(
            {
                "experiment_id": experiment_id,
                "config_uri": config_uri,
                "created_at": _fmt(created_at),
                "last_known_state": last_known_state,
                "lease": lease,
            }
        )

    def _lease_for_experiment(
        self, cur: psycopg.Cursor[Any], experiment_id: str
    ) -> ExperimentLease | None:
        # Codex round 4 MAJOR: filter on `expires_at >= now` so an
        # expired-but-not-garbage-collected row never surfaces in
        # `RegisteredExperiment.lease`. See memory.py `_build_entry`
        # for the matching invariant — both backends MUST behave
        # identically per chapter 11 §2.1.
        cur.execute(
            """
            SELECT lease_id, experiment_id, holder, holder_instance,
                   acquired_at, expires_at, renewed_at
              FROM control_plane_leases
             WHERE experiment_id = %s AND expires_at >= %s
            """,
            (experiment_id, self._now()),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_lease(row)

    # ------------------------------------------------------------------
    # Experiment registry (chapter 11 §2)
    # ------------------------------------------------------------------

    def register_experiment(
        self, experiment_id: str, config_uri: str
    ) -> tuple[RegisteredExperiment, bool]:
        # Atomic insert-or-observe under `SERIALIZABLE` so concurrent
        # callers observe exactly one `created=True`.
        with self._atomic(), self._conn.cursor() as cur:
            cur.execute(
                "SELECT experiment_id, config_uri, created_at, last_known_state "
                "FROM control_plane_experiments WHERE experiment_id = %s",
                (experiment_id,),
            )
            existing = cur.fetchone()
            if existing is not None:
                if existing[1] != config_uri:
                    raise AlreadyExists(
                        f"experiment {experiment_id!r} is registered with a "
                        f"different config_uri (stored {existing[1]!r}, "
                        f"requested {config_uri!r})"
                    )
                lease = self._lease_for_experiment(cur, experiment_id)
                return self._row_to_entry(existing, lease), False
            cur.execute(
                "INSERT INTO control_plane_experiments "
                "(experiment_id, config_uri, created_at, last_known_state) "
                "VALUES (%s, %s, %s, 'running') "
                "RETURNING experiment_id, config_uri, created_at, last_known_state",
                (experiment_id, config_uri, self._now()),
            )
            row = cur.fetchone()
            assert row is not None
            return self._row_to_entry(row, None), True

    def unregister_experiment(self, experiment_id: str) -> None:
        with self._atomic(), self._conn.cursor() as cur:
            cur.execute(
                "SELECT last_known_state FROM control_plane_experiments "
                "WHERE experiment_id = %s",
                (experiment_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise NotFound(f"experiment {experiment_id!r}")
            if row[0] != "terminated":
                raise InvalidPrecondition(
                    f"experiment {experiment_id!r} is still {row[0]!r}; "
                    f"terminate first"
                )
            now = self._now()
            cur.execute(
                "SELECT lease_id FROM control_plane_leases "
                "WHERE experiment_id = %s AND expires_at >= %s",
                (experiment_id, now),
            )
            if cur.fetchone() is not None:
                raise InvalidPrecondition(
                    f"experiment {experiment_id!r} has an active lease; "
                    f"release or wait for expiry before unregistering"
                )
            cur.execute(
                "DELETE FROM control_plane_experiments WHERE experiment_id = %s",
                (experiment_id,),
            )
            # The FK cascade drops any expired lease row alongside.

    def list_experiments(self) -> list[RegisteredExperiment]:
        with self._atomic(), self._conn.cursor() as cur:
            cur.execute(
                "SELECT experiment_id, config_uri, created_at, last_known_state "
                "FROM control_plane_experiments ORDER BY experiment_id"
            )
            rows = cur.fetchall()
            out: list[RegisteredExperiment] = []
            for row in rows:
                lease = self._lease_for_experiment(cur, row[0])
                out.append(self._row_to_entry(row, lease))
            return out

    def read_experiment_metadata(
        self, experiment_id: str
    ) -> RegisteredExperiment:
        with self._atomic(), self._conn.cursor() as cur:
            cur.execute(
                "SELECT experiment_id, config_uri, created_at, last_known_state "
                "FROM control_plane_experiments WHERE experiment_id = %s",
                (experiment_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise NotFound(f"experiment {experiment_id!r}")
            lease = self._lease_for_experiment(cur, experiment_id)
            return self._row_to_entry(row, lease)

    def update_last_known_state(
        self, experiment_id: str, last_known_state: LastKnownState
    ) -> RegisteredExperiment:
        with self._atomic(), self._conn.cursor() as cur:
            cur.execute(
                "UPDATE control_plane_experiments "
                "SET last_known_state = %s "
                "WHERE experiment_id = %s "
                "RETURNING experiment_id, config_uri, created_at, last_known_state",
                (last_known_state, experiment_id),
            )
            row = cur.fetchone()
            if row is None:
                raise NotFound(f"experiment {experiment_id!r}")
            lease = self._lease_for_experiment(cur, experiment_id)
            return self._row_to_entry(row, lease)

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
        with self._atomic(), self._conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM control_plane_experiments WHERE experiment_id = %s",
                (experiment_id,),
            )
            if cur.fetchone() is None:
                raise NotFound(f"experiment {experiment_id!r}")
            now = self._now()
            expires = now + timedelta(seconds=lease_duration_seconds)
            new_lease_id = "lease-" + secrets.token_hex(16)
            # Atomic "acquire or replace-expired". The
            # ON CONFLICT clause replaces the existing row only when
            # its expires_at < the new acquired_at; otherwise the
            # statement no-ops and we read the existing row.
            cur.execute(
                """
                INSERT INTO control_plane_leases
                  (experiment_id, lease_id, holder, holder_instance,
                   acquired_at, expires_at, renewed_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (experiment_id) DO UPDATE
                  SET lease_id = EXCLUDED.lease_id,
                      holder = EXCLUDED.holder,
                      holder_instance = EXCLUDED.holder_instance,
                      acquired_at = EXCLUDED.acquired_at,
                      expires_at = EXCLUDED.expires_at,
                      renewed_at = EXCLUDED.renewed_at
                  WHERE control_plane_leases.expires_at < EXCLUDED.acquired_at
                RETURNING lease_id, experiment_id, holder, holder_instance,
                          acquired_at, expires_at, renewed_at
                """,
                (
                    experiment_id,
                    new_lease_id,
                    holder,
                    holder_instance,
                    now,
                    expires,
                    now,
                ),
            )
            row = cur.fetchone()
            if row is None:
                # The DO UPDATE WHERE clause filtered the conflict —
                # active lease exists.
                cur.execute(
                    "SELECT holder FROM control_plane_leases "
                    "WHERE experiment_id = %s",
                    (experiment_id,),
                )
                active = cur.fetchone()
                holder_msg = active[0] if active else "<unknown>"
                raise LeaseHeldByOther(
                    f"experiment {experiment_id!r} has an active lease "
                    f"held by worker_id={holder_msg!r}"
                )
            return self._row_to_lease(row)

    def renew_lease(
        self,
        lease_id: str,
        holder_instance: str,
        *,
        lease_duration_seconds: int,
    ) -> ExperimentLease:
        with self._atomic(), self._conn.cursor() as cur:
            cur.execute(
                "SELECT holder_instance, expires_at FROM control_plane_leases "
                "WHERE lease_id = %s FOR UPDATE",
                (lease_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise LeaseNotHeld(
                    f"lease {lease_id!r} has been replaced or never existed"
                )
            stored_holder_instance, stored_expires = row
            if stored_holder_instance != holder_instance:
                raise LeaseInstanceMismatch(
                    f"lease {lease_id!r} stored holder_instance does not "
                    f"match the caller's"
                )
            now = self._now()
            if _utc(stored_expires) < now:
                raise LeaseExpired(
                    f"lease {lease_id!r} expired at {stored_expires}; "
                    f"reacquire instead of renew"
                )
            new_expires = now + timedelta(seconds=lease_duration_seconds)
            cur.execute(
                "UPDATE control_plane_leases "
                "SET expires_at = %s, renewed_at = %s "
                "WHERE lease_id = %s "
                "RETURNING lease_id, experiment_id, holder, holder_instance, "
                "          acquired_at, expires_at, renewed_at",
                (new_expires, now, lease_id),
            )
            updated = cur.fetchone()
            assert updated is not None
            return self._row_to_lease(updated)

    def release_lease(self, lease_id: str, holder_instance: str) -> None:
        with self._atomic(), self._conn.cursor() as cur:
            cur.execute(
                "SELECT holder_instance FROM control_plane_leases "
                "WHERE lease_id = %s FOR UPDATE",
                (lease_id,),
            )
            row = cur.fetchone()
            if row is None:
                return  # idempotent
            if row[0] != holder_instance:
                raise LeaseInstanceMismatch(
                    f"lease {lease_id!r} stored holder_instance does not "
                    f"match the caller's"
                )
            cur.execute(
                "DELETE FROM control_plane_leases WHERE lease_id = %s",
                (lease_id,),
            )

    def list_active_leases(self, holder: str) -> list[ExperimentLease]:
        with self._atomic(), self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT lease_id, experiment_id, holder, holder_instance,
                       acquired_at, expires_at, renewed_at
                  FROM control_plane_leases
                 WHERE holder = %s AND expires_at >= %s
                 ORDER BY experiment_id
                """,
                (holder, self._now()),
            )
            return [self._row_to_lease(r) for r in cur.fetchall()]

    def read_lease(self, lease_id: str) -> ExperimentLease:
        with self._atomic(), self._conn.cursor() as cur:
            cur.execute(
                "SELECT lease_id, experiment_id, holder, holder_instance, "
                "       acquired_at, expires_at, renewed_at "
                "FROM control_plane_leases WHERE lease_id = %s",
                (lease_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise NotFound(f"lease {lease_id!r}")
            return self._row_to_lease(row)

    # ------------------------------------------------------------------
    # Deployment-scoped workers (chapter 11 §6)
    # ------------------------------------------------------------------

    def register_worker(
        self,
        worker_id: str,
        *,
        labels: dict[str, str] | None = None,
        registered_by: str | None = None,
    ) -> tuple[Worker, str | None]:
        validate_registry_id(worker_id, kind="worker")
        with self._atomic(), self._conn.cursor() as cur:
            cur.execute(
                "SELECT registered_at, registered_by, labels FROM control_plane_workers "
                "WHERE worker_id = %s",
                (worker_id,),
            )
            existing = cur.fetchone()
            if existing is not None:
                return (self._build_worker(worker_id, existing), None)
            cur.execute(
                "SELECT 1 FROM control_plane_groups WHERE group_id = %s",
                (worker_id,),
            )
            if cur.fetchone() is not None:
                raise AlreadyExists(
                    f"id {worker_id!r} is already registered as a group; "
                    f"namespaces MUST be disjoint per chapter 02 §7.1"
                )
            token = generate_credential_token()
            now = self._now()
            cur.execute(
                "INSERT INTO control_plane_workers "
                "(worker_id, registered_at, registered_by, labels, credential_hash) "
                "VALUES (%s, %s, %s, %s, %s)",
                (
                    worker_id,
                    now,
                    registered_by,
                    json.dumps(labels) if labels else None,
                    hash_credential(token),
                ),
            )
            worker = Worker.model_validate(
                {
                    "worker_id": worker_id,
                    "experiment_id": "<deployment>",
                    "registered_at": _fmt(now),
                    **({"registered_by": registered_by} if registered_by else {}),
                    **({"labels": dict(labels)} if labels else {}),
                }
            )
            return (worker, token)

    def _build_worker(
        self, worker_id: str, row: tuple[Any, ...]
    ) -> Worker:
        registered_at, registered_by, labels = row
        data: dict[str, Any] = {
            "worker_id": worker_id,
            "experiment_id": "<deployment>",
            "registered_at": _fmt(registered_at),
        }
        if registered_by is not None:
            data["registered_by"] = registered_by
        if labels is not None:
            data["labels"] = labels
        return Worker.model_validate(data)

    def reissue_credential(self, worker_id: str) -> str:
        with self._atomic(), self._conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM control_plane_workers WHERE worker_id = %s",
                (worker_id,),
            )
            if cur.fetchone() is None:
                raise NotFound(f"worker {worker_id!r}")
            token = generate_credential_token()
            cur.execute(
                "UPDATE control_plane_workers SET credential_hash = %s "
                "WHERE worker_id = %s",
                (hash_credential(token), worker_id),
            )
            return token

    def verify_worker_credential(
        self, worker_id: str, registration_token: str
    ) -> bool:
        with self._atomic(), self._conn.cursor() as cur:
            cur.execute(
                "SELECT credential_hash FROM control_plane_workers "
                "WHERE worker_id = %s",
                (worker_id,),
            )
            row = cur.fetchone()
        if row is None:
            constant_time_dummy_verify(registration_token)
            return False
        return check_credential_hash(registration_token, row[0])

    def read_worker(self, worker_id: str) -> Worker:
        with self._atomic(), self._conn.cursor() as cur:
            cur.execute(
                "SELECT registered_at, registered_by, labels "
                "FROM control_plane_workers WHERE worker_id = %s",
                (worker_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise NotFound(f"worker {worker_id!r}")
            return self._build_worker(worker_id, row)

    def list_workers(self) -> list[Worker]:
        with self._atomic(), self._conn.cursor() as cur:
            cur.execute(
                "SELECT worker_id, registered_at, registered_by, labels "
                "FROM control_plane_workers ORDER BY worker_id"
            )
            return [
                self._build_worker(row[0], row[1:])
                for row in cur.fetchall()
            ]

    # ------------------------------------------------------------------
    # Deployment-scoped groups (chapter 11 §6)
    # ------------------------------------------------------------------

    def register_group(
        self,
        group_id: str,
        *,
        members: Iterable[str] | None = None,
    ) -> Group:
        validate_registry_id(group_id, kind="group")
        members_list = list(members) if members is not None else []
        for m in members_list:
            validate_registry_id(m, kind="member")
        with self._atomic(), self._conn.cursor() as cur:
            cur.execute(
                "SELECT created_at FROM control_plane_groups WHERE group_id = %s",
                (group_id,),
            )
            existing = cur.fetchone()
            if existing is not None:
                return self._load_group(cur, group_id, existing[0])
            cur.execute(
                "SELECT 1 FROM control_plane_workers WHERE worker_id = %s",
                (group_id,),
            )
            if cur.fetchone() is not None:
                raise AlreadyExists(
                    f"id {group_id!r} is already registered as a worker; "
                    f"namespaces MUST be disjoint per chapter 02 §7.1"
                )
            for m in members_list:
                if self._is_existing_group(cur, m) and self._would_close_cycle(
                    cur, m, group_id
                ):
                    raise CycleDetected(
                        f"adding {m!r} to {group_id!r} closes a cycle"
                    )
            now = self._now()
            cur.execute(
                "INSERT INTO control_plane_groups (group_id, created_at) "
                "VALUES (%s, %s)",
                (group_id, now),
            )
            for m in members_list:
                cur.execute(
                    "INSERT INTO control_plane_group_members "
                    "(group_id, member_id) VALUES (%s, %s)",
                    (group_id, m),
                )
            return self._load_group(cur, group_id, now)

    def _is_existing_group(
        self, cur: psycopg.Cursor[Any], group_id: str
    ) -> bool:
        cur.execute(
            "SELECT 1 FROM control_plane_groups WHERE group_id = %s",
            (group_id,),
        )
        return cur.fetchone() is not None

    def _would_close_cycle(
        self, cur: psycopg.Cursor[Any], start: str, banned: str
    ) -> bool:
        visited: set[str] = set()
        stack = [start]
        while stack:
            cur_node = stack.pop()
            if cur_node in visited:
                continue
            visited.add(cur_node)
            cur.execute(
                "SELECT member_id FROM control_plane_group_members "
                "WHERE group_id = %s",
                (cur_node,),
            )
            for (member_id,) in cur.fetchall():
                if member_id == banned:
                    return True
                if (
                    self._is_existing_group(cur, member_id)
                    and member_id not in visited
                ):
                    stack.append(member_id)
        return False

    def _load_group(
        self,
        cur: psycopg.Cursor[Any],
        group_id: str,
        created_at: datetime,
    ) -> Group:
        cur.execute(
            "SELECT member_id FROM control_plane_group_members "
            "WHERE group_id = %s ORDER BY member_id",
            (group_id,),
        )
        members = [row[0] for row in cur.fetchall()]
        return Group.model_validate(
            {
                "group_id": group_id,
                "experiment_id": "<deployment>",
                "members": members,
                "created_at": _fmt(created_at),
            }
        )

    def add_to_group(self, group_id: str, member_id: str) -> Group:
        validate_registry_id(member_id, kind="member")
        with self._atomic(), self._conn.cursor() as cur:
            cur.execute(
                "SELECT created_at FROM control_plane_groups WHERE group_id = %s",
                (group_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise NotFound(f"group {group_id!r}")
            cur.execute(
                "SELECT 1 FROM control_plane_group_members "
                "WHERE group_id = %s AND member_id = %s",
                (group_id, member_id),
            )
            if cur.fetchone() is not None:
                return self._load_group(cur, group_id, row[0])
            if self._is_existing_group(cur, member_id) and self._would_close_cycle(
                cur, member_id, group_id
            ):
                raise CycleDetected(
                    f"adding {member_id!r} to {group_id!r} closes a cycle"
                )
            cur.execute(
                "INSERT INTO control_plane_group_members "
                "(group_id, member_id) VALUES (%s, %s)",
                (group_id, member_id),
            )
            return self._load_group(cur, group_id, row[0])

    def remove_from_group(self, group_id: str, member_id: str) -> Group:
        with self._atomic(), self._conn.cursor() as cur:
            cur.execute(
                "SELECT created_at FROM control_plane_groups WHERE group_id = %s",
                (group_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise NotFound(f"group {group_id!r}")
            cur.execute(
                "DELETE FROM control_plane_group_members "
                "WHERE group_id = %s AND member_id = %s",
                (group_id, member_id),
            )
            return self._load_group(cur, group_id, row[0])

    def delete_group(self, group_id: str) -> None:
        with self._atomic(), self._conn.cursor() as cur:
            cur.execute(
                "DELETE FROM control_plane_groups WHERE group_id = %s",
                (group_id,),
            )
            if cur.rowcount == 0:
                raise NotFound(f"group {group_id!r}")

    def read_group(self, group_id: str) -> Group:
        with self._atomic(), self._conn.cursor() as cur:
            cur.execute(
                "SELECT created_at FROM control_plane_groups WHERE group_id = %s",
                (group_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise NotFound(f"group {group_id!r}")
            return self._load_group(cur, group_id, row[0])

    def list_groups(self) -> list[Group]:
        with self._atomic(), self._conn.cursor() as cur:
            cur.execute(
                "SELECT group_id, created_at FROM control_plane_groups "
                "ORDER BY group_id"
            )
            rows = cur.fetchall()
            return [self._load_group(cur, gid, created_at) for gid, created_at in rows]

    def resolve_worker_in_group(self, worker_id: str, group_id: str) -> bool:
        with self._atomic(), self._conn.cursor() as cur:
            visited: set[str] = set()
            stack = [group_id]
            while stack:
                cur_node = stack.pop()
                if cur_node in visited:
                    continue
                visited.add(cur_node)
                cur.execute(
                    "SELECT member_id FROM control_plane_group_members "
                    "WHERE group_id = %s",
                    (cur_node,),
                )
                for (member_id,) in cur.fetchall():
                    if member_id == worker_id:
                        return True
                    if (
                        self._is_existing_group(cur, member_id)
                        and member_id not in visited
                    ):
                        stack.append(member_id)
            return False
