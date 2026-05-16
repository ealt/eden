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
        evaluation_schema text
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
    CREATE TABLE idea (
        idea_id text NOT NULL PRIMARY KEY,
        state text NOT NULL,
        data text NOT NULL
    )
    """,
    "CREATE INDEX idea_by_state ON idea(state)",
    """
    CREATE TABLE variant (
        variant_id text NOT NULL PRIMARY KEY,
        status text NOT NULL,
        data text NOT NULL
    )
    """,
    "CREATE INDEX variant_by_status ON variant(status)",
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


_V2_STATEMENTS: list[str] = [
    """
    CREATE TABLE worker (
        worker_id text NOT NULL PRIMARY KEY,
        data text NOT NULL,
        credential_hash text NOT NULL
    )
    """,
    """
    CREATE TABLE worker_group (
        group_id text NOT NULL PRIMARY KEY,
        data text NOT NULL
    )
    """,
    """
    CREATE TABLE group_membership (
        group_id text NOT NULL,
        member_id text NOT NULL,
        position integer NOT NULL,
        PRIMARY KEY (group_id, member_id),
        FOREIGN KEY (group_id) REFERENCES worker_group(group_id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX group_membership_by_member ON group_membership(member_id)",
]


def _apply_v2(cur: Any) -> None:
    for stmt in _V2_STATEMENTS:
        cur.execute(stmt)


# 12a-2: parallels the SQLite v3 migration. The column is stored as
# ``text`` (not ``jsonb``) to keep round-trips byte-for-byte parallel
# with ``SqliteStore``; ``08-storage.md`` §3 doesn't constrain on-disk
# typing, only observable contracts.
_V3_STATEMENTS: list[str] = [
    "ALTER TABLE experiment ADD COLUMN dispatch_mode text NOT NULL DEFAULT "
    "'{\"ideation_creation\":\"auto\",\"execution_dispatch\":\"auto\","
    "\"evaluation_dispatch\":\"auto\",\"integration\":\"auto\"}'",
]


def _apply_v3(cur: Any) -> None:
    for stmt in _V3_STATEMENTS:
        cur.execute(stmt)


# 12a-2 plan §5.2: partial unique indexes that enforce the §6.4
# at-most-one-live invariant at the DB layer. See the parallel
# SQLite v4 migration in `_schema.py` for the full rationale.
#
# Postgres uses the `->>` operator on ``data::json`` (the column is
# ``text``, not ``jsonb``, per the chunk-10b parity decision in
# ``_postgres_schema.py``'s header). Because the column is text, we
# cast to ``json`` inline so ``->>`` works.
_V4_STATEMENTS: list[str] = [
    "CREATE UNIQUE INDEX task_live_execution_by_idea "
    "ON task(((data::json -> 'payload' ->> 'idea_id'))) "
    "WHERE kind = 'execution' "
    "AND state IN ('pending', 'claimed', 'submitted')",
    "CREATE UNIQUE INDEX task_live_evaluation_by_variant "
    "ON task(((data::json -> 'payload' ->> 'variant_id'))) "
    "WHERE kind = 'evaluation' "
    "AND state IN ('pending', 'claimed', 'submitted')",
]


def _apply_v4(cur: Any) -> None:
    for stmt in _V4_STATEMENTS:
        cur.execute(stmt)


_MIGRATIONS: list[Callable[[Any], None]] = [
    _apply_v1,
    _apply_v2,
    _apply_v3,
    _apply_v4,
]


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
