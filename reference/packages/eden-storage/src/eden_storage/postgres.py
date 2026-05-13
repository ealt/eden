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
    EvaluationSchema,
    Event,
    Group,
    Idea,
    Task,
    TaskAdapter,
    Variant,
    Worker,
)

from . import _postgres_schema
from ._base import _StoreBase, _Tx
from .errors import InvalidPrecondition
from .submissions import (
    EvaluationSubmission,
    IdeaSubmission,
    Submission,
    VariantSubmission,
)


def _serialize_model(model: Any) -> str:
    return json.dumps(model.model_dump(mode="json", exclude_none=True))


def _submission_to_row(submission: Submission) -> tuple[str, str]:
    """Return ``(kind, json_data)`` for a submission. Mirrors sqlite.py."""
    if isinstance(submission, IdeaSubmission):
        payload: dict[str, Any] = {
            "status": submission.status,
            "idea_ids": list(submission.idea_ids),
        }
        return ("ideation", json.dumps(payload))
    if isinstance(submission, VariantSubmission):
        payload = {
            "status": submission.status,
            "variant_id": submission.variant_id,
        }
        if submission.commit_sha is not None:
            payload["commit_sha"] = submission.commit_sha
        return ("execution", json.dumps(payload))
    if isinstance(submission, EvaluationSubmission):
        payload = {
            "status": submission.status,
            "variant_id": submission.variant_id,
        }
        if submission.evaluation is not None:
            payload["evaluation"] = submission.evaluation
        if submission.artifacts_uri is not None:
            payload["artifacts_uri"] = submission.artifacts_uri
        return ("evaluation", json.dumps(payload))
    raise TypeError(f"unknown submission type {type(submission).__name__}")


def _submission_from_row(kind: str, data: str) -> Submission:
    payload = json.loads(data)
    if kind == "ideation":
        return IdeaSubmission(
            status=payload["status"],
            idea_ids=tuple(payload.get("idea_ids") or ()),
        )
    if kind == "execution":
        return VariantSubmission(
            status=payload["status"],
            variant_id=payload["variant_id"],
            commit_sha=payload.get("commit_sha"),
        )
    if kind == "evaluation":
        return EvaluationSubmission(
            status=payload["status"],
            variant_id=payload["variant_id"],
            evaluation=payload.get("evaluation"),
            artifacts_uri=payload.get("artifacts_uri"),
        )
    raise ValueError(f"unknown submission kind {kind!r}")


class PostgresStore(_StoreBase):
    """Postgres-backed ``Store``. See module docstring for serialization strategy.

    The store either initializes a fresh database (when the
    ``experiment`` row is absent) or reopens an existing one (when
    present). On reopen, ``experiment_id`` MUST match the one
    recorded in the database; passing a different ``experiment_id``
    raises :class:`InvalidPrecondition`. ``evaluation_schema`` MUST NOT
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
        evaluation_schema: EvaluationSchema | None = None,
        now: Callable[[], datetime] | None = None,
        event_id_factory: Callable[[], str] | None = None,
    ) -> None:
        super().__init__(
            experiment_id,
            evaluation_schema=evaluation_schema,
            now=now,
            event_id_factory=event_id_factory,
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
        self._initialize_experiment(experiment_id, evaluation_schema)
        # Resume the default event-id counter from the persisted seq —
        # same logic as sqlite.py.
        if event_id_factory is None:
            self._event_ids = itertools.count(self._next_event_seq())

    # ------------------------------------------------------------------
    # Setup + teardown
    # ------------------------------------------------------------------

    def _initialize_experiment(
        self, experiment_id: str, evaluation_schema: EvaluationSchema | None
    ) -> None:
        """Create or validate the single experiment row.

        Same semantics as :meth:`SqliteStore._initialize_experiment` —
        schema canonicalization (sort_keys) plus chapter 8 §4.2
        reopen-immutability.
        """
        schema_json: str | None = None
        if evaluation_schema is not None:
            schema_json = json.dumps(
                evaluation_schema.model_dump(mode="json"), sort_keys=True
            )
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT experiment_id, evaluation_schema FROM experiment"
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    "INSERT INTO experiment(experiment_id, evaluation_schema) "
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
        if evaluation_schema is None:
            if stored_schema is not None:
                self._evaluation_schema = EvaluationSchema.model_validate_json(stored_schema)
        elif stored_canonical != schema_json:
            raise InvalidPrecondition(
                "evaluation_schema MUST NOT change for the lifetime of an "
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

    def _get_idea(self, idea_id: str) -> Idea | None:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT data FROM idea WHERE idea_id = %s", (idea_id,)
            )
            row = cur.fetchone()
        if row is None:
            return None
        return Idea.model_validate_json(row[0])

    def _get_variant(self, variant_id: str) -> Variant | None:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT data FROM variant WHERE variant_id = %s", (variant_id,)
            )
            row = cur.fetchone()
        if row is None:
            return None
        return Variant.model_validate_json(row[0])

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

    def _iter_ideas(self, *, state: str | None = None) -> Iterable[Idea]:
        with self._conn.cursor() as cur:
            if state is None:
                cur.execute("SELECT data FROM idea ORDER BY idea_id")
            else:
                cur.execute(
                    "SELECT data FROM idea WHERE state = %s ORDER BY idea_id",
                    (state,),
                )
            rows = cur.fetchall()
        return [Idea.model_validate_json(row[0]) for row in rows]

    def _iter_variants(self, *, status: str | None = None) -> Iterable[Variant]:
        with self._conn.cursor() as cur:
            if status is None:
                cur.execute("SELECT data FROM variant ORDER BY variant_id")
            else:
                cur.execute(
                    "SELECT data FROM variant WHERE status = %s ORDER BY variant_id",
                    (status,),
                )
            rows = cur.fetchall()
        return [Variant.model_validate_json(row[0]) for row in rows]

    def _iter_events(self) -> Iterable[Event]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT data FROM event ORDER BY seq")
            rows = cur.fetchall()
        return [Event.model_validate_json(row[0]) for row in rows]

    def _get_worker(self, worker_id: str) -> Worker | None:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT data FROM worker WHERE worker_id = %s", (worker_id,)
            )
            row = cur.fetchone()
        if row is None:
            return None
        return Worker.model_validate_json(row[0])

    def _get_worker_credential_hash(self, worker_id: str) -> str | None:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT credential_hash FROM worker WHERE worker_id = %s",
                (worker_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return row[0]

    def _iter_workers(self) -> Iterable[Worker]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT data FROM worker ORDER BY worker_id")
            rows = cur.fetchall()
        return [Worker.model_validate_json(row[0]) for row in rows]

    def _get_group(self, group_id: str) -> Group | None:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT data FROM worker_group WHERE group_id = %s", (group_id,)
            )
            row = cur.fetchone()
        if row is None:
            return None
        return Group.model_validate_json(row[0])

    def _iter_groups(self) -> Iterable[Group]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT data FROM worker_group ORDER BY group_id")
            rows = cur.fetchall()
        return [Group.model_validate_json(row[0]) for row in rows]

    # ------------------------------------------------------------------
    # Commit
    # ------------------------------------------------------------------

    def _apply_commit(self, tx: _Tx) -> None:
        """Apply staged writes inside the already-open transaction."""
        for task_id, task in tx.tasks.items():
            self._upsert_task(task_id, task)
        for idea_id, idea in tx.ideas.items():
            self._upsert_idea(idea_id, idea)
        for variant_id, variant in tx.variants.items():
            self._upsert_variant(variant_id, variant)
        for task_id, submission in tx.submissions.items():
            self._upsert_submission(task_id, submission)
        for task_id in tx.task_deletes_submission:
            with self._conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM submission WHERE task_id = %s", (task_id,)
                )
        for worker_id, worker in tx.workers.items():
            self._upsert_worker(
                worker_id, worker, tx.worker_credentials.get(worker_id)
            )
        for worker_id, credential_hash in tx.worker_credentials.items():
            if worker_id in tx.workers:
                continue
            self._update_worker_credential(worker_id, credential_hash)
        for worker_id in tx.worker_deletes:
            with self._conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM worker WHERE worker_id = %s", (worker_id,)
                )
        for group_id, group in tx.groups.items():
            self._upsert_group(group_id, group)
        for group_id in tx.group_deletes:
            with self._conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM worker_group WHERE group_id = %s", (group_id,)
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

    def _upsert_idea(self, idea_id: str, idea: Idea) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO idea(idea_id, state, data) VALUES(%s, %s, %s)
                ON CONFLICT(idea_id) DO UPDATE SET
                    state = EXCLUDED.state,
                    data = EXCLUDED.data
                """,
                (idea_id, idea.state, _serialize_model(idea)),
            )

    def _upsert_variant(self, variant_id: str, variant: Variant) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO variant(variant_id, status, data) VALUES(%s, %s, %s)
                ON CONFLICT(variant_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    data = EXCLUDED.data
                """,
                (variant_id, variant.status, _serialize_model(variant)),
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

    def _upsert_worker(
        self, worker_id: str, worker: Worker, credential_hash: str | None
    ) -> None:
        if credential_hash is None:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT credential_hash FROM worker WHERE worker_id = %s",
                    (worker_id,),
                )
                row = cur.fetchone()
            credential_hash = row[0] if row is not None else ""
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO worker(worker_id, data, credential_hash)
                VALUES(%s, %s, %s)
                ON CONFLICT(worker_id) DO UPDATE SET
                    data = EXCLUDED.data,
                    credential_hash = EXCLUDED.credential_hash
                """,
                (worker_id, _serialize_model(worker), credential_hash),
            )

    def _update_worker_credential(
        self, worker_id: str, credential_hash: str
    ) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE worker SET credential_hash = %s WHERE worker_id = %s",
                (credential_hash, worker_id),
            )

    def _upsert_group(self, group_id: str, group: Group) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO worker_group(group_id, data) VALUES(%s, %s)
                ON CONFLICT(group_id) DO UPDATE SET data = EXCLUDED.data
                """,
                (group_id, _serialize_model(group)),
            )
            cur.execute(
                "DELETE FROM group_membership WHERE group_id = %s", (group_id,)
            )
            for position, member in enumerate(group.members):
                cur.execute(
                    """
                    INSERT INTO group_membership(group_id, member_id, position)
                    VALUES(%s, %s, %s)
                    """,
                    (group_id, member, position),
                )
