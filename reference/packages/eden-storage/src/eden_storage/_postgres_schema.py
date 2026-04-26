"""Postgres schema + migrations for ``PostgresStore``.

Same shape as [`_schema.py`](_schema.py) (the SQLite version) with
type substitutions: ``TEXT`` → ``text``, ``AUTOINCREMENT`` →
``BIGINT GENERATED ALWAYS AS IDENTITY``. ``data`` stays as ``text``
(rather than ``jsonb``) so reads are byte-for-byte parallel to
``SqliteStore``; the JSON validation that ``jsonb`` would add at
write time isn't worth the dict-vs-string read-path divergence at
this stage. Migrating columns to ``jsonb`` is a future concern.

Migrations are linear; ``schema_version`` holds the single highest
version applied. Adding a future migration appends to
``_MIGRATIONS`` and lets ``ensure_schema`` run it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

_V1_STATEMENTS: list[str] = [
    """
    CREATE TABLE experiment (
        experiment_id text NOT NULL PRIMARY KEY,
        metrics_schema text
    )
    """,
    """
    CREATE TABLE task (
        task_id text NOT NULL PRIMARY KEY,
        kind text NOT NULL,
        state text NOT NULL,
        data text NOT NULL
    )
    """,
    "CREATE INDEX task_by_kind_state ON task(kind, state)",
    """
    CREATE TABLE submission (
        task_id text NOT NULL PRIMARY KEY,
        kind text NOT NULL,
        data text NOT NULL,
        FOREIGN KEY(task_id) REFERENCES task(task_id)
    )
    """,
    """
    CREATE TABLE proposal (
        proposal_id text NOT NULL PRIMARY KEY,
        state text NOT NULL,
        data text NOT NULL
    )
    """,
    "CREATE INDEX proposal_by_state ON proposal(state)",
    """
    CREATE TABLE trial (
        trial_id text NOT NULL PRIMARY KEY,
        status text NOT NULL,
        data text NOT NULL
    )
    """,
    "CREATE INDEX trial_by_status ON trial(status)",
    """
    CREATE TABLE event (
        seq BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        event_id text NOT NULL UNIQUE,
        type text NOT NULL,
        occurred_at text NOT NULL,
        experiment_id text NOT NULL,
        data text NOT NULL
    )
    """,
]


def _apply_v1(cur: Any) -> None:
    for stmt in _V1_STATEMENTS:
        cur.execute(stmt)


_MIGRATIONS: list[Callable[[Any], None]] = [_apply_v1]


def current_version(cur: Any) -> int:
    """Return the applied schema version, or 0 if uninitialized.

    Resolves ``schema_version`` against the active ``search_path`` —
    not hard-coded to ``public.schema_version`` — so a store opened
    against a non-default schema (e.g. the per-test schema in
    ``tests/conftest.py``) sees its own migrations table on reopen.
    Hard-coding ``public.schema_version`` would always probe the
    default schema and treat every reopen as a fresh database,
    re-running v1 migrations and failing on duplicate-table errors.
    """
    cur.execute("SELECT to_regclass('schema_version') IS NOT NULL")
    row = cur.fetchone()
    if not row or not row[0]:
        return 0
    cur.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version")
    row = cur.fetchone()
    return int(row[0] or 0)


def ensure_schema(conn: Any) -> None:
    """Apply every pending migration, in order, under one transaction.

    ``conn`` must be a psycopg ``Connection``. Migrations run inside
    an explicit BEGIN/COMMIT block so a partial DDL failure rolls
    back cleanly regardless of the connection's autocommit setting.
    Idempotent: re-running on an up-to-date database is a no-op.
    """
    with conn.cursor() as cur:
        # Bootstrap table outside the transaction so re-running is
        # cheap and the ``current_version`` probe never errors on a
        # missing relation.
        cur.execute(
            "CREATE TABLE IF NOT EXISTS schema_version "
            "(version INTEGER NOT NULL PRIMARY KEY)"
        )
        applied = current_version(cur)
        target = len(_MIGRATIONS)
        if applied >= target:
            return
        cur.execute("BEGIN")
        try:
            for version in range(applied + 1, target + 1):
                _MIGRATIONS[version - 1](cur)
                cur.execute(
                    "INSERT INTO schema_version(version) VALUES (%s)",
                    (version,),
                )
        except BaseException:
            cur.execute("ROLLBACK")
            raise
        cur.execute("COMMIT")
