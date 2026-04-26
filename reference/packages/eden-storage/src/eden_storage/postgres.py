"""Postgres-backed ``Store`` implementation.

Single-process, durable. Satisfies chapter 8 §3 via Postgres'
write-ahead log + per-operation transactions at SERIALIZABLE
isolation. The shared transition logic is in [`_base.py`](_base.py);
this module supplies the backend primitives that talk to Postgres
through ``psycopg`` v3.

Each public operation opens a ``BEGIN ISOLATION LEVEL SERIALIZABLE``
transaction, runs reads + validations (which may raise), stages
writes in a ``_Tx``, and calls ``_apply_commit`` to issue the SQL
statements. On normal context-manager exit the transaction commits;
on exception it rolls back so no partial state becomes observable
(chapter 8 §6.1–§6.3).

Serialization mirrors [`sqlite.py`](sqlite.py) byte-for-byte: every
``data`` column is a JSON-serialized model dump, decoded on read.
Two store instances pointing at the same database appear as a single
shared store (chapter 8 §3.2 read-after-write); concurrent
SERIALIZABLE transactions on different connections enforce the
chapter 8 §1.2 atomicity guarantee.
"""

from __future__ import annotations

import itertools
import json
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

import psycopg
from eden_contracts import (
    Event,
    MetricsSchema,
    Proposal,
    Task,
    TaskAdapter,
    Trial,
)

from . import _postgres_schema
from ._base import _StoreBase, _Tx
from .errors import InvalidPrecondition
from .submissions import (
    EvaluateSubmission,
    ImplementSubmission,
    PlanSubmission,
    Submission,
)


def _serialize_model(model: Any) -> str:
    return json.dumps(model.model_dump(mode="json", exclude_none=True))


def _submission_to_row(submission: Submission) -> tuple[str, str]:
    """Return ``(kind, json_data)`` for a submission. Mirrors sqlite.py."""
    if isinstance(submission, PlanSubmission):
        payload: dict[str, Any] = {
            "status": submission.status,
            "proposal_ids": list(submission.proposal_ids),
        }
        return ("plan", json.dumps(payload))
    if isinstance(submission, ImplementSubmission):
        payload = {
            "status": submission.status,
            "trial_id": submission.trial_id,
        }
        if submission.commit_sha is not None:
            payload["commit_sha"] = submission.commit_sha
        return ("implement", json.dumps(payload))
    if isinstance(submission, EvaluateSubmission):
        payload = {
            "status": submission.status,
            "trial_id": submission.trial_id,
        }
        if submission.metrics is not None:
            payload["metrics"] = submission.metrics
        if submission.artifacts_uri is not None:
            payload["artifacts_uri"] = submission.artifacts_uri
        return ("evaluate", json.dumps(payload))
    raise TypeError(f"unknown submission type {type(submission).__name__}")


def _submission_from_row(kind: str, data: str) -> Submission:
    payload = json.loads(data)
    if kind == "plan":
        return PlanSubmission(
            status=payload["status"],
            proposal_ids=tuple(payload.get("proposal_ids") or ()),
        )
    if kind == "implement":
        return ImplementSubmission(
            status=payload["status"],
            trial_id=payload["trial_id"],
            commit_sha=payload.get("commit_sha"),
        )
    if kind == "evaluate":
        return EvaluateSubmission(
            status=payload["status"],
            trial_id=payload["trial_id"],
            metrics=payload.get("metrics"),
            artifacts_uri=payload.get("artifacts_uri"),
        )
    raise ValueError(f"unknown submission kind {kind!r}")


class PostgresStore(_StoreBase):
    """Postgres-backed ``Store``. See module docstring for serialization strategy.

    The store either initializes a fresh database (when the
    ``experiment`` row is absent) or reopens an existing one (when
    present). On reopen, ``experiment_id`` MUST match the one
    recorded in the database; passing a different ``experiment_id``
    raises :class:`InvalidPrecondition`. ``metrics_schema`` MUST NOT
    be changed on reopen per chapter 8 §4.2 (immutability); passing a
    different schema raises.

    ``dsn`` is a libpq connection string (``postgresql://user:pw@host:5432/db``).
    A single connection backs the store; concurrent calls are
    serialized through ``_lock``, matching the single-connection
    posture of :class:`SqliteStore`.
    """

    def __init__(
        self,
        experiment_id: str,
        dsn: str,
        *,
        metrics_schema: MetricsSchema | None = None,
        now: Callable[[], datetime] | None = None,
        event_id_factory: Callable[[], str] | None = None,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        super().__init__(
            experiment_id,
            metrics_schema=metrics_schema,
            now=now,
            event_id_factory=event_id_factory,
            token_factory=token_factory,
        )
        self._dsn = dsn
        # autocommit=True + explicit BEGIN/COMMIT per op mirrors
        # sqlite.py's `isolation_level=None` posture. Without
        # autocommit, psycopg auto-starts a transaction on the
        # first command — and then `BEGIN ISOLATION LEVEL
        # SERIALIZABLE` errors with `ActiveSqlTransaction`. Manual
        # txn boundaries also let us pin SERIALIZABLE per operation.
        self._conn = psycopg.connect(dsn, autocommit=True)
        self._lock = threading.RLock()
        self._in_txn = False
        _postgres_schema.ensure_schema(self._conn)
        self._initialize_experiment(experiment_id, metrics_schema)
        # Resume the default event-id counter from the persisted seq —
        # same logic as sqlite.py.
        if event_id_factory is None:
            self._event_ids = itertools.count(self._next_event_seq())

    # ------------------------------------------------------------------
    # Setup + teardown
    # ------------------------------------------------------------------

    def _initialize_experiment(
        self, experiment_id: str, metrics_schema: MetricsSchema | None
    ) -> None:
        """Create or validate the single experiment row.

        Same semantics as :meth:`SqliteStore._initialize_experiment` —
        schema canonicalization (sort_keys) plus chapter 8 §4.2
        reopen-immutability.
        """
        schema_json: str | None = None
        if metrics_schema is not None:
            schema_json = json.dumps(
                metrics_schema.model_dump(mode="json"), sort_keys=True
            )
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT experiment_id, metrics_schema FROM experiment"
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    "INSERT INTO experiment(experiment_id, metrics_schema) "
                    "VALUES (%s, %s)",
                    (experiment_id, schema_json),
                )
                return
            stored_id, stored_schema = row
        if stored_id != experiment_id:
            raise InvalidPrecondition(
                f"database at {self._dsn!r} belongs to experiment "
                f"{stored_id!r}, not {experiment_id!r}"
            )
        stored_canonical: str | None = None
        if stored_schema is not None:
            stored_canonical = json.dumps(json.loads(stored_schema), sort_keys=True)
        if metrics_schema is None:
            if stored_schema is not None:
                self._metrics_schema = MetricsSchema.model_validate_json(stored_schema)
        elif stored_canonical != schema_json:
            raise InvalidPrecondition(
                "metrics_schema MUST NOT change for the lifetime of an "
                "experiment (08-storage.md §4.2); database at "
                f"{self._dsn!r} has a different schema recorded"
            )

    def _next_event_seq(self) -> int:
        """Return the next default event-id counter value.

        Counts the rows in ``event`` (matching sqlite.py's logic).
        Caller-supplied ``event_id_factory`` instances handle their
        own collision avoidance.
        """
        with self._conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(seq), 0) FROM event")
            row = cur.fetchone()
        return int((row[0] if row else 0) or 0) + 1

    def close(self) -> None:
        """Close the underlying connection."""
        self._conn.close()

    def __enter__(self) -> PostgresStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Atomic-operation context
    # ------------------------------------------------------------------

    @contextmanager
    def _atomic_operation(self) -> Iterator[None]:
        """Wrap every public operation in a SERIALIZABLE transaction.

        SERIALIZABLE is the strongest isolation Postgres provides;
        chapter 8 §1.2 needs at-most-one-success on concurrent
        ``claim`` invocations, and SERIALIZABLE delivers that without
        relying on advisory locks. On normal exit COMMIT; on
        exception ROLLBACK.

        ``_in_txn`` prevents re-entry: helpers called from a public
        method participate in the outer transaction.
        """
        with self._lock:
            if self._in_txn:
                yield
                return
            self._conn.execute(
                "BEGIN ISOLATION LEVEL SERIALIZABLE READ WRITE"
            )
            self._in_txn = True
            try:
                yield
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")
            finally:
                self._in_txn = False

    # ------------------------------------------------------------------
    # Read primitives
    # ------------------------------------------------------------------

    def _get_task(self, task_id: str) -> Task | None:
        with self._conn.cursor() as cur:
            cur.execute("SELECT data FROM task WHERE task_id = %s", (task_id,))
            row = cur.fetchone()
        if row is None:
            return None
        return TaskAdapter.validate_python(json.loads(row[0]))

    def _get_proposal(self, proposal_id: str) -> Proposal | None:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT data FROM proposal WHERE proposal_id = %s", (proposal_id,)
            )
            row = cur.fetchone()
        if row is None:
            return None
        return Proposal.model_validate_json(row[0])

    def _get_trial(self, trial_id: str) -> Trial | None:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT data FROM trial WHERE trial_id = %s", (trial_id,)
            )
            row = cur.fetchone()
        if row is None:
            return None
        return Trial.model_validate_json(row[0])

    def _get_submission(self, task_id: str) -> Submission | None:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT kind, data FROM submission WHERE task_id = %s",
                (task_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return _submission_from_row(row[0], row[1])

    def _iter_tasks(
        self, *, kind: str | None = None, state: str | None = None
    ) -> Iterable[Task]:
        with self._conn.cursor() as cur:
            if kind is None and state is None:
                cur.execute("SELECT data FROM task ORDER BY task_id")
            elif state is None:
                cur.execute(
                    "SELECT data FROM task WHERE kind = %s ORDER BY task_id",
                    (kind,),
                )
            elif kind is None:
                cur.execute(
                    "SELECT data FROM task WHERE state = %s ORDER BY task_id",
                    (state,),
                )
            else:
                cur.execute(
                    "SELECT data FROM task WHERE kind = %s AND state = %s "
                    "ORDER BY task_id",
                    (kind, state),
                )
            rows = cur.fetchall()
        return [TaskAdapter.validate_python(json.loads(row[0])) for row in rows]

    def _iter_proposals(self, *, state: str | None = None) -> Iterable[Proposal]:
        with self._conn.cursor() as cur:
            if state is None:
                cur.execute("SELECT data FROM proposal ORDER BY proposal_id")
            else:
                cur.execute(
                    "SELECT data FROM proposal WHERE state = %s ORDER BY proposal_id",
                    (state,),
                )
            rows = cur.fetchall()
        return [Proposal.model_validate_json(row[0]) for row in rows]

    def _iter_trials(self, *, status: str | None = None) -> Iterable[Trial]:
        with self._conn.cursor() as cur:
            if status is None:
                cur.execute("SELECT data FROM trial ORDER BY trial_id")
            else:
                cur.execute(
                    "SELECT data FROM trial WHERE status = %s ORDER BY trial_id",
                    (status,),
                )
            rows = cur.fetchall()
        return [Trial.model_validate_json(row[0]) for row in rows]

    def _iter_events(self) -> Iterable[Event]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT data FROM event ORDER BY seq")
            rows = cur.fetchall()
        return [Event.model_validate_json(row[0]) for row in rows]

    # ------------------------------------------------------------------
    # Commit
    # ------------------------------------------------------------------

    def _apply_commit(self, tx: _Tx) -> None:
        """Apply staged writes inside the already-open transaction."""
        for task_id, task in tx.tasks.items():
            self._upsert_task(task_id, task)
        for proposal_id, proposal in tx.proposals.items():
            self._upsert_proposal(proposal_id, proposal)
        for trial_id, trial in tx.trials.items():
            self._upsert_trial(trial_id, trial)
        for task_id, submission in tx.submissions.items():
            self._upsert_submission(task_id, submission)
        for task_id in tx.task_deletes_submission:
            with self._conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM submission WHERE task_id = %s", (task_id,)
                )
        for event in tx.events:
            self._insert_event(event)

    def _upsert_task(self, task_id: str, task: Task) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO task(task_id, kind, state, data) VALUES(%s, %s, %s, %s)
                ON CONFLICT(task_id) DO UPDATE SET
                    kind = EXCLUDED.kind,
                    state = EXCLUDED.state,
                    data = EXCLUDED.data
                """,
                (task_id, task.kind, task.state, _serialize_model(task)),
            )

    def _upsert_proposal(self, proposal_id: str, proposal: Proposal) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO proposal(proposal_id, state, data) VALUES(%s, %s, %s)
                ON CONFLICT(proposal_id) DO UPDATE SET
                    state = EXCLUDED.state,
                    data = EXCLUDED.data
                """,
                (proposal_id, proposal.state, _serialize_model(proposal)),
            )

    def _upsert_trial(self, trial_id: str, trial: Trial) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO trial(trial_id, status, data) VALUES(%s, %s, %s)
                ON CONFLICT(trial_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    data = EXCLUDED.data
                """,
                (trial_id, trial.status, _serialize_model(trial)),
            )

    def _upsert_submission(self, task_id: str, submission: Submission) -> None:
        kind, data = _submission_to_row(submission)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO submission(task_id, kind, data) VALUES(%s, %s, %s)
                ON CONFLICT(task_id) DO UPDATE SET
                    kind = EXCLUDED.kind,
                    data = EXCLUDED.data
                """,
                (task_id, kind, data),
            )

    def _insert_event(self, event: Event) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO event(event_id, type, occurred_at, experiment_id, data)
                VALUES(%s, %s, %s, %s, %s)
                """,
                (
                    event.event_id,
                    event.type,
                    event.occurred_at,
                    event.experiment_id,
                    _serialize_model(event),
                ),
            )
