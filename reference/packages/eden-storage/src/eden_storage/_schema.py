"""SQLite schema + migrations for ``SqliteStore``.

One table per entity kind plus an ``event`` table that preserves
insertion order via ``AUTOINCREMENT`` row IDs. Each row stores the
canonical form of the object as a JSON blob in ``data``; indexed
columns (``kind``, ``state``, ``status``) are denormalized copies
used only for filtered queries. The JSON blob is the source of
truth — Pydantic's ``model_validate`` rehydrates it on read, so any
drift between the denormalized columns and the JSON would surface
at the next read.

Migrations are linear. ``schema_version`` holds the single highest
version applied. Adding a future migration means appending a new
``_MIGRATIONS`` entry and letting ``ensure_schema`` run it.

Primary-key collisions on inserts are mapped to store-level
``AlreadyExists`` / ``IllegalTransition`` errors by ``sqlite.py``;
this module only defines DDL + migration orchestration.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

_V1_STATEMENTS: list[str] = [
    """
    CREATE TABLE experiment (
        experiment_id TEXT NOT NULL PRIMARY KEY,
        metrics_schema TEXT
    )
    """,
    """
    CREATE TABLE task (
        task_id TEXT NOT NULL PRIMARY KEY,
        kind TEXT NOT NULL,
        state TEXT NOT NULL,
        data TEXT NOT NULL
    )
    """,
    "CREATE INDEX task_by_kind_state ON task(kind, state)",
    """
    CREATE TABLE submission (
        task_id TEXT NOT NULL PRIMARY KEY,
        kind TEXT NOT NULL,
        data TEXT NOT NULL,
        FOREIGN KEY(task_id) REFERENCES task(task_id)
    )
    """,
    """
    CREATE TABLE proposal (
        proposal_id TEXT NOT NULL PRIMARY KEY,
        state TEXT NOT NULL,
        data TEXT NOT NULL
    )
    """,
    "CREATE INDEX proposal_by_state ON proposal(state)",
    """
    CREATE TABLE trial (
        trial_id TEXT NOT NULL PRIMARY KEY,
        status TEXT NOT NULL,
        data TEXT NOT NULL
    )
    """,
    "CREATE INDEX trial_by_status ON trial(status)",
    """
    CREATE TABLE event (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        type TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        experiment_id TEXT NOT NULL,
        data TEXT NOT NULL
    )
    """,
]


def _apply_v1(conn: sqlite3.Connection) -> None:
    # Use execute() per statement so the outer BEGIN/COMMIT bounds
    # them into one transaction — `executescript` would issue an
    # implicit COMMIT and defeat the bounding.
    for stmt in _V1_STATEMENTS:
        conn.execute(stmt)


_MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [_apply_v1]


def current_version(conn: sqlite3.Connection) -> int:
    """Return the applied schema version, or 0 if uninitialized."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if row is None:
        return 0
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return row[0] or 0


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Apply every pending migration, in order, under one transaction.

    Idempotent: re-running on an up-to-date database is a no-op.

    The connection MUST be in ``isolation_level=None`` (manual
    transaction control) so the explicit ``BEGIN``/``COMMIT`` here
    takes effect. Pydantic-unrelated note: ``executescript`` in
    SQLite autocommits — wrapping it with ``BEGIN`` bounds it into a
    single transaction, so a partial DDL failure rolls back cleanly.
    """
    # Create the version table out-of-band so we can insert into it
    # without itself needing to be in _MIGRATIONS (which would be a
    # circularity).
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version "
        "(version INTEGER NOT NULL PRIMARY KEY)"
    )
    applied = current_version(conn)
    target = len(_MIGRATIONS)
    if applied >= target:
        return
    conn.execute("BEGIN")
    try:
        for version in range(applied + 1, target + 1):
            migration = _MIGRATIONS[version - 1]
            migration(conn)
            conn.execute(
                "INSERT INTO schema_version(version) VALUES (?)", (version,)
            )
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
