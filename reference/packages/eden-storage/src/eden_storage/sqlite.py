"""SQLite-backed ``Store`` implementation.

Single-process, durable. Satisfies chapter 8 §3 (durability,
read-after-write, crash recovery) via SQLite's write-ahead log and
per-operation transactions. The shared transition logic is in
[`_base.py`](_base.py); this module supplies the backend primitives
that talk to SQLite.

Each public operation opens a ``BEGIN IMMEDIATE`` transaction, runs
reads + validations (which may raise), stages writes in a ``_Tx``,
and calls ``_apply_commit`` to issue the SQL statements. On normal
context-manager exit the transaction commits; on exception it rolls
back so no partial state becomes observable (chapter 8 §6.1–§6.3).

Serialization strategy:

- Pydantic models (``Task``, ``Idea``, ``Variant``, ``Event``) round-
  trip via ``model_dump(mode="json", exclude_none=True)`` →
  ``json.dumps`` → ``json.loads`` → ``model_validate``. The ``data``
  column is the source of truth; denormalized columns (``kind``,
  ``state``, ``status``) are only used for filtered queries and must
  stay consistent with the JSON.
- ``Submission`` is a frozen dataclass; serialized with an explicit
  per-kind schema (see ``_submission_to_row`` / ``_submission_from_row``).
- ``EvaluationSchema`` (Pydantic ``RootModel``) is persisted in the
  ``experiment`` table at first open; reopening the same database
  loads it back so evaluation validation survives restart.
"""

from __future__ import annotations

import itertools
import json
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from eden_contracts import (
    EvaluationSchema,
    Event,
    Experiment,
    Group,
    Idea,
    ImportProvenance,
    Task,
    TaskAdapter,
    Variant,
    Worker,
)

from . import _schema
from ._base import (
    _DEFAULT_DISPATCH_MODE,
    _DEFAULT_EXPERIMENT_STATE,
    _StoreBase,
    _Tx,
)
from .errors import InvalidPrecondition
from .submissions import (
    Submission,
    submission_from_payload,
    submission_to_payload,
)


def _serialize_model(model: Any) -> str:
    return json.dumps(model.model_dump(mode="json", exclude_none=True))


def _submission_to_row(submission: Submission) -> tuple[str, str]:
    """Return ``(kind, json_data)`` for a submission."""
    kind, payload = submission_to_payload(submission)
    return kind, json.dumps(payload)


def _submission_from_row(kind: str, data: str) -> Submission:
    return submission_from_payload(kind, json.loads(data))


class SqliteStore(_StoreBase):
    """SQLite-backed ``Store``. See module docstring for serialization strategy.

    The store either initializes a fresh database (when the
    ``experiment`` row is absent) or reopens an existing one (when
    present). On reopen, ``experiment_id`` MUST match the one
    recorded in the database; passing a different ``experiment_id``
    raises ``InvalidPrecondition``. ``evaluation_schema`` MUST NOT be
    changed on reopen per chapter 8 §4.2 (immutability); passing a
    different schema raises.

    ``path`` can be a filesystem path (``"/tmp/eden.db"``), a
    ``pathlib.Path``, or the special string ``":memory:"`` for an
    in-process SQLite database. The in-memory form is useful for
    tests that want SQL semantics without a file.
    """

    def __init__(
        self,
        experiment_id: str,
        path: str | Path,
        *,
        name: str | None = None,
        evaluation_schema: EvaluationSchema | None = None,
        now: Callable[[], datetime] | None = None,
        event_id_factory: Callable[[], str] | None = None,
        tree_resolver: Callable[[str], str | None] | None = None,
        base_commit_sha: str | None = None,
    ) -> None:
        super().__init__(
            experiment_id,
            name=name,
            evaluation_schema=evaluation_schema,
            now=now,
            event_id_factory=event_id_factory,
            tree_resolver=tree_resolver,
            base_commit_sha=base_commit_sha,
        )
        self._path = str(path)
        # isolation_level=None ⇒ manual BEGIN/COMMIT control. Without
        # this, Python's DB-API wrapper would open implicit transactions
        # at surprising points.
        self._conn = sqlite3.connect(
            self._path,
            isolation_level=None,
            check_same_thread=False,
        )
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        # Chapter 8 §3.1 requires a committed write to survive a crash
        # of the store's host. SQLite's WAL + `synchronous=NORMAL` is
        # documented to lose durability under power loss or system
        # crash; only `FULL` fsyncs on every commit. For a reference
        # backend we take the stricter setting — the cost is a fsync
        # per composite commit, which is acceptable for the workloads
        # a reference dispatch loop handles.
        self._conn.execute("PRAGMA synchronous = FULL")
        self._lock = threading.RLock()
        self._in_txn = False
        _schema.ensure_schema(self._conn)
        self._initialize_experiment(experiment_id, evaluation_schema)
        # The default event-id counter in _StoreBase starts at 1 every
        # time a store object is constructed, which on reopen would
        # produce duplicate event_id values and violate the UNIQUE
        # constraint. Resume the counter from the persisted event
        # count (via the `seq` AUTOINCREMENT column) so the fresh
        # store continues the same sequence — independent of the
        # event_id string format.
        if event_id_factory is None:
            self._event_ids = itertools.count(self._next_event_seq())

    # ------------------------------------------------------------------
    # Setup + teardown
    # ------------------------------------------------------------------

    def _initialize_experiment(
        self, experiment_id: str, evaluation_schema: EvaluationSchema | None
    ) -> None:
        """Create or validate the single experiment row.

        On first open inserts the experiment + evaluation_schema. On
        reopen, enforces that both match what was persisted — chapter
        8 §4.2 says the evaluation schema MUST NOT change during an
        experiment's lifetime.
        """
        row = self._conn.execute(
            "SELECT experiment_id, evaluation_schema, name FROM experiment"
        ).fetchone()
        # Canonicalize via sort_keys so a reopen with the same logical
        # metric map in a different insertion order is NOT treated as
        # a schema change. §4.2 pins semantic schema identity, not JSON
        # key order; literal string comparison would reject benign
        # dict-rebuild differences.
        schema_json: str | None = None
        if evaluation_schema is not None:
            schema_json = json.dumps(
                evaluation_schema.model_dump(mode="json"), sort_keys=True
            )
        if row is None:
            # 12a-3: stamp `created_at` at row creation so a future
            # `read_experiment` returns the actual experiment-created
            # timestamp rather than the v5 migration's 1970 sentinel
            # (which only applies to rows that pre-existed the v5
            # migration). `state` defaults to "running" via the column
            # DEFAULT; explicit pass-through here makes the contract
            # legible without relying on the DDL.
            created_at = self._ts()
            self._conn.execute(
                "INSERT INTO experiment(experiment_id, evaluation_schema, "
                "state, created_at, base_commit_sha, name) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    experiment_id,
                    schema_json,
                    _DEFAULT_EXPERIMENT_STATE,
                    created_at,
                    self._base_commit_sha,
                    self._experiment_name,
                ),
            )
            self._conn.commit()
            return
        stored_id, stored_schema, stored_name = row
        # On reopen the persisted name is authoritative — mirror the
        # evaluation_schema immutability posture (§4.2). A `name=` arg on
        # reopen is ignored; the stored value wins.
        self._experiment_name = stored_name
        if stored_id != experiment_id:
            raise InvalidPrecondition(
                f"database at {self._path!r} belongs to experiment "
                f"{stored_id!r}, not {experiment_id!r}"
            )
        # Canonicalize the stored JSON too: older databases persisted
        # under the literal-order code path may have any key ordering.
        stored_canonical: str | None = None
        if stored_schema is not None:
            stored_canonical = json.dumps(json.loads(stored_schema), sort_keys=True)
        if evaluation_schema is None:
            # No schema supplied: inherit whatever was persisted so
            # §4.3 enforcement survives restart. `None` stored + `None`
            # argument is also fine — metrics simply aren't validated.
            if stored_schema is not None:
                self._evaluation_schema = EvaluationSchema.model_validate_json(stored_schema)
        elif stored_canonical != schema_json:
            raise InvalidPrecondition(
                "evaluation_schema MUST NOT change for the lifetime of an "
                "experiment (08-storage.md §4.2); database at "
                f"{self._path!r} has a different schema recorded"
            )

    def _next_event_seq(self) -> int:
        """Return the next value for the default event-id counter.

        Uses the row count of persisted events (via the
        AUTOINCREMENT ``seq`` column, which equals row count under
        append-only usage) as the counter's resumption point. The
        format of the event_id string is irrelevant — we count rows,
        not parse labels. Caller-supplied ``event_id_factory``
        instances are responsible for their own collision avoidance
        on reopen.
        """
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM event"
        ).fetchone()
        return int(row[0] or 0) + 1

    def close(self) -> None:
        """Close the underlying SQLite connection.

        After close, the store cannot be used. Reopen by constructing
        a new ``SqliteStore`` against the same path.
        """
        self._conn.close()

    def __enter__(self) -> SqliteStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Atomic-operation context
    # ------------------------------------------------------------------

    @contextmanager
    def _atomic_operation(self) -> Iterator[None]:
        """Wrap every public operation in a SQLite transaction.

        ``BEGIN IMMEDIATE`` acquires a RESERVED lock up front, which
        is what chapter 8 §1.2 asks for ("at most one concurrent
        claim invocation MUST succeed"). On normal exit we COMMIT;
        on exception we ROLLBACK so no partial state is observable.

        The ``_in_txn`` flag prevents re-entry: helpers called from a
        public method (``_accept_*``) must not open another
        transaction.
        """
        with self._lock:
            if self._in_txn:
                # Already inside a public-method transaction; helpers
                # participate in the outer transaction rather than
                # opening a nested one.
                yield
                return
            self._conn.execute("BEGIN IMMEDIATE")
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
        row = self._conn.execute(
            "SELECT data FROM task WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            return None
        return TaskAdapter.validate_python(json.loads(row[0]))

    def _get_idea(self, idea_id: str) -> Idea | None:
        row = self._conn.execute(
            "SELECT data FROM idea WHERE idea_id = ?", (idea_id,)
        ).fetchone()
        if row is None:
            return None
        return Idea.model_validate_json(row[0])

    def _get_variant(self, variant_id: str) -> Variant | None:
        row = self._conn.execute(
            "SELECT data FROM variant WHERE variant_id = ?", (variant_id,)
        ).fetchone()
        if row is None:
            return None
        return Variant.model_validate_json(row[0])

    def _get_submission(self, task_id: str) -> Submission | None:
        row = self._conn.execute(
            "SELECT kind, data FROM submission WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            return None
        return _submission_from_row(row[0], row[1])

    def _iter_tasks(
        self, *, kind: str | None = None, state: str | None = None
    ) -> Iterable[Task]:
        sql = "SELECT data FROM task"
        clauses: list[str] = []
        params: list[Any] = []
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if state is not None:
            clauses.append("state = ?")
            params.append(state)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY task_id"
        return [
            TaskAdapter.validate_python(json.loads(row[0]))
            for row in self._conn.execute(sql, params)
        ]

    def _iter_ideas(self, *, state: str | None = None) -> Iterable[Idea]:
        if state is None:
            rows = self._conn.execute(
                "SELECT data FROM idea ORDER BY idea_id"
            )
        else:
            rows = self._conn.execute(
                "SELECT data FROM idea WHERE state = ? ORDER BY idea_id",
                (state,),
            )
        return [Idea.model_validate_json(row[0]) for row in rows]

    def _iter_variants(self, *, status: str | None = None) -> Iterable[Variant]:
        if status is None:
            rows = self._conn.execute(
                "SELECT data FROM variant ORDER BY variant_id"
            )
        else:
            rows = self._conn.execute(
                "SELECT data FROM variant WHERE status = ? ORDER BY variant_id",
                (status,),
            )
        return [Variant.model_validate_json(row[0]) for row in rows]

    def _iter_events(self) -> Iterable[Event]:
        rows = self._conn.execute("SELECT data FROM event ORDER BY seq")
        return [Event.model_validate_json(row[0]) for row in rows]

    def _get_worker(self, worker_id: str) -> Worker | None:
        row = self._conn.execute(
            "SELECT data FROM worker WHERE worker_id = ?", (worker_id,)
        ).fetchone()
        if row is None:
            return None
        return Worker.model_validate_json(row[0])

    def _get_worker_credential_hash(self, worker_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT credential_hash FROM worker WHERE worker_id = ?", (worker_id,)
        ).fetchone()
        if row is None:
            return None
        return row[0]

    def _iter_workers(self) -> Iterable[Worker]:
        rows = self._conn.execute("SELECT data FROM worker ORDER BY worker_id")
        return [Worker.model_validate_json(row[0]) for row in rows]

    def _get_group(self, group_id: str) -> Group | None:
        row = self._conn.execute(
            "SELECT data FROM worker_group WHERE group_id = ?", (group_id,)
        ).fetchone()
        if row is None:
            return None
        return Group.model_validate_json(row[0])

    def _iter_groups(self) -> Iterable[Group]:
        rows = self._conn.execute(
            "SELECT data FROM worker_group ORDER BY group_id"
        )
        return [Group.model_validate_json(row[0]) for row in rows]

    def _get_dispatch_mode(self) -> dict[str, str]:
        row = self._conn.execute(
            "SELECT dispatch_mode FROM experiment WHERE experiment_id = ?",
            (self._experiment_id,),
        ).fetchone()
        # The v3 migration sets a NOT NULL default, so a row that
        # exists always has a JSON blob here. Fallback to the in-code
        # default only as defense against a hand-edited database.
        if row is None or row[0] is None:
            return dict(_DEFAULT_DISPATCH_MODE)
        return dict(json.loads(row[0]))

    def _get_experiment(self) -> Experiment:
        row = self._conn.execute(
            "SELECT state, created_at, imported_from, base_commit_sha, name "
            "FROM experiment "
            "WHERE experiment_id = ?",
            (self._experiment_id,),
        ).fetchone()
        # The row is created in `_initialize_experiment` before this
        # method is ever called; a missing row would mean someone
        # deleted it out-of-band.
        if row is None:
            raise RuntimeError(
                f"experiment {self._experiment_id!r} row missing from store"
            )
        imported_from: ImportProvenance | None = None
        if row[2] is not None:
            imported_from = ImportProvenance.model_validate_json(row[2])
        # Omit optional fields when NULL — the `NotNone` validators on
        # Experiment.base_commit_sha (#122) / .name (#128) reject explicit
        # null for these optional fields.
        data: dict[str, object] = {
            "experiment_id": self._experiment_id,
            "state": row[0],
            "created_at": row[1],
            "imported_from": imported_from,
        }
        if row[3] is not None:
            data["base_commit_sha"] = row[3]
        if row[4] is not None:
            data["name"] = row[4]
        return Experiment.model_validate(data)

    # ------------------------------------------------------------------
    # Commit
    # ------------------------------------------------------------------

    def _apply_commit(self, tx: _Tx) -> None:
        """Apply every staged write under the already-open transaction.

        The outer ``_atomic_operation`` context manager COMMITs on
        normal exit; this method only stages rows.
        """
        for task_id, task in tx.tasks.items():
            self._upsert_task(task_id, task)
        for idea_id, idea in tx.ideas.items():
            self._upsert_idea(idea_id, idea)
        for variant_id, variant in tx.variants.items():
            self._upsert_variant(variant_id, variant)
        for task_id, submission in tx.submissions.items():
            self._upsert_submission(task_id, submission)
        for task_id in tx.task_deletes_submission:
            self._conn.execute(
                "DELETE FROM submission WHERE task_id = ?", (task_id,)
            )
        for worker_id, worker in tx.workers.items():
            self._upsert_worker(
                worker_id, worker, tx.worker_credentials.get(worker_id)
            )
        # Credential rotations on a worker that wasn't otherwise re-staged.
        for worker_id, credential_hash in tx.worker_credentials.items():
            if worker_id in tx.workers:
                continue
            self._update_worker_credential(worker_id, credential_hash)
        for worker_id in tx.worker_deletes:
            self._conn.execute(
                "DELETE FROM worker WHERE worker_id = ?", (worker_id,)
            )
        for group_id, group in tx.groups.items():
            self._upsert_group(group_id, group)
        for group_id in tx.group_deletes:
            self._conn.execute(
                "DELETE FROM worker_group WHERE group_id = ?", (group_id,)
            )
        if tx.dispatch_mode is not None:
            self._conn.execute(
                "UPDATE experiment SET dispatch_mode = ? WHERE experiment_id = ?",
                (
                    json.dumps(tx.dispatch_mode, sort_keys=True),
                    self._experiment_id,
                ),
            )
        if tx.experiment_state is not None:
            self._conn.execute(
                "UPDATE experiment SET state = ? WHERE experiment_id = ?",
                (tx.experiment_state, self._experiment_id),
            )
        if tx.imported_from_update is not None:
            (new_imported_from,) = tx.imported_from_update
            # exclude_none so the optional `source_experiment_id`
            # (issue #128) is OMITTED rather than serialized as explicit
            # null — the `NotNone` validator rejects explicit null on
            # read-back.
            serialized = (
                None
                if new_imported_from is None
                else json.dumps(
                    new_imported_from.model_dump(mode="json", exclude_none=True)
                )
            )
            self._conn.execute(
                "UPDATE experiment SET imported_from = ? WHERE experiment_id = ?",
                (serialized, self._experiment_id),
            )
        if tx.base_commit_sha_update is not None:
            (new_base_commit_sha,) = tx.base_commit_sha_update
            self._conn.execute(
                "UPDATE experiment SET base_commit_sha = ? WHERE experiment_id = ?",
                (new_base_commit_sha, self._experiment_id),
            )
            self._base_commit_sha = new_base_commit_sha
        for event in tx.events:
            self._insert_event(event)

    def _upsert_task(self, task_id: str, task: Task) -> None:
        self._conn.execute(
            """
            INSERT INTO task(task_id, kind, state, data) VALUES(?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                kind = excluded.kind,
                state = excluded.state,
                data = excluded.data
            """,
            (task_id, task.kind, task.state, _serialize_model(task)),
        )

    def _upsert_idea(self, idea_id: str, idea: Idea) -> None:
        self._conn.execute(
            """
            INSERT INTO idea(idea_id, state, data) VALUES(?, ?, ?)
            ON CONFLICT(idea_id) DO UPDATE SET
                state = excluded.state,
                data = excluded.data
            """,
            (idea_id, idea.state, _serialize_model(idea)),
        )

    def _upsert_variant(self, variant_id: str, variant: Variant) -> None:
        self._conn.execute(
            """
            INSERT INTO variant(variant_id, status, data) VALUES(?, ?, ?)
            ON CONFLICT(variant_id) DO UPDATE SET
                status = excluded.status,
                data = excluded.data
            """,
            (variant_id, variant.status, _serialize_model(variant)),
        )

    def _upsert_submission(self, task_id: str, submission: Submission) -> None:
        kind, data = _submission_to_row(submission)
        self._conn.execute(
            """
            INSERT INTO submission(task_id, kind, data) VALUES(?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                kind = excluded.kind,
                data = excluded.data
            """,
            (task_id, kind, data),
        )

    def _insert_event(self, event: Event) -> None:
        self._conn.execute(
            """
            INSERT INTO event(event_id, type, occurred_at, experiment_id, data)
            VALUES(?, ?, ?, ?, ?)
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
        # `register_worker` always stages the credential alongside the
        # worker row; `reissue_credential` stages a fresh hash plus the
        # unchanged Worker. The branch below preserves the prior hash
        # when the staged tx didn't supply one (defensive — the public
        # API never exercises that path today).
        if credential_hash is None:
            existing = self._conn.execute(
                "SELECT credential_hash FROM worker WHERE worker_id = ?",
                (worker_id,),
            ).fetchone()
            credential_hash = existing[0] if existing is not None else ""
        # `name` is denormalized into its own indexed column for fast
        # `list_workers(name=...)` filtering; the JSON `data` blob
        # remains the source of truth (issue #128).
        self._conn.execute(
            """
            INSERT INTO worker(worker_id, data, credential_hash, name)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(worker_id) DO UPDATE SET
                data = excluded.data,
                credential_hash = excluded.credential_hash,
                name = excluded.name
            """,
            (worker_id, _serialize_model(worker), credential_hash, worker.name),
        )

    def _update_worker_credential(
        self, worker_id: str, credential_hash: str
    ) -> None:
        # Used by reissue paths that do NOT also re-stage the Worker
        # row. `_StoreBase.reissue_credential` always re-stages the
        # row, so this branch is currently dead but kept for symmetry.
        self._conn.execute(
            "UPDATE worker SET credential_hash = ? WHERE worker_id = ?",
            (credential_hash, worker_id),
        )

    def _upsert_group(self, group_id: str, group: Group) -> None:
        # `name` is denormalized into its own indexed column for fast
        # `list_groups(name=...)` filtering; the JSON `data` blob
        # remains the source of truth (issue #128).
        self._conn.execute(
            """
            INSERT INTO worker_group(group_id, data, name) VALUES(?, ?, ?)
            ON CONFLICT(group_id) DO UPDATE SET
                data = excluded.data,
                name = excluded.name
            """,
            (group_id, _serialize_model(group), group.name),
        )
        # Refresh the denormalized membership index so future filtered
        # queries (e.g. "which groups contain member X") can run from
        # SQL. Membership semantics (transitive resolution, cycle
        # detection) live in `_base.py`; the table here is purely an
        # implementation aid.
        self._conn.execute(
            "DELETE FROM group_membership WHERE group_id = ?", (group_id,)
        )
        for position, member in enumerate(group.members):
            self._conn.execute(
                """
                INSERT INTO group_membership(group_id, member_id, position)
                VALUES(?, ?, ?)
                """,
                (group_id, member, position),
            )
