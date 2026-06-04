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
        evaluation_schema TEXT
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
    CREATE TABLE idea (
        idea_id TEXT NOT NULL PRIMARY KEY,
        state TEXT NOT NULL,
        data TEXT NOT NULL
    )
    """,
    "CREATE INDEX idea_by_state ON idea(state)",
    """
    CREATE TABLE variant (
        variant_id TEXT NOT NULL PRIMARY KEY,
        status TEXT NOT NULL,
        data TEXT NOT NULL
    )
    """,
    "CREATE INDEX variant_by_status ON variant(status)",
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


_V2_STATEMENTS: list[str] = [
    # Worker registry (12a-1 wave 2).
    # `data` is the canonical wire-visible Worker JSON (no credential
    # hash); `credential_hash` is stored separately so reads against
    # `data` never need to redact the secret. Together they round-trip
    # the §6.2 fields plus the §6.3 credential.
    """
    CREATE TABLE worker (
        worker_id TEXT NOT NULL PRIMARY KEY,
        data TEXT NOT NULL,
        credential_hash TEXT NOT NULL
    )
    """,
    # Group registry. `data` carries the canonical Group JSON.
    # Membership is denormalized into `group_membership` only as an
    # implementation aid for future indexed queries; the JSON in
    # `data` remains the source of truth, exactly mirroring the
    # task / idea / variant pattern.
    """
    CREATE TABLE worker_group (
        group_id TEXT NOT NULL PRIMARY KEY,
        data TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE group_membership (
        group_id TEXT NOT NULL,
        member_id TEXT NOT NULL,
        position INTEGER NOT NULL,
        PRIMARY KEY (group_id, member_id),
        FOREIGN KEY (group_id) REFERENCES worker_group(group_id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX group_membership_by_member ON group_membership(member_id)",
]


def _apply_v2(conn: sqlite3.Connection) -> None:
    for stmt in _V2_STATEMENTS:
        conn.execute(stmt)


# 12a-2: the experiment row gains a `dispatch_mode` column carrying
# the JSON-serialized per-decision-type gate (`02-data-model.md` §2.5).
# Default value is the all-`auto` JSON literal so existing experiment
# rows pick up sensible behavior at the next migration.
_V3_STATEMENTS: list[str] = [
    "ALTER TABLE experiment ADD COLUMN dispatch_mode text NOT NULL DEFAULT "
    "'{\"ideation_creation\":\"auto\",\"execution_dispatch\":\"auto\","
    "\"evaluation_dispatch\":\"auto\",\"integration\":\"auto\"}'",
]


def _apply_v3(conn: sqlite3.Connection) -> None:
    for stmt in _V3_STATEMENTS:
        conn.execute(stmt)


# 12a-2 plan §5.2: partial unique indexes that enforce the §6.4
# at-most-one-live invariant at the DB layer. Without these, the
# guarantee depends on the single-process server serializing
# operations in-process — multi-replica deployments could in
# principle race past the in-memory check. The indexes are partial:
# they only cover ``live`` states (pending / claimed / submitted), so
# the same idea / variant can produce a fresh live task once the
# previous one terminalizes.
#
# Note on existing rows: pre-v4 stores may contain multiple live
# rows that would fail the new index. The plan's posture is
# greenfield-pre-external-user (AGENTS.md "Project Lifecycle"), so a
# failed migration on stale dev data is acceptable feedback rather
# than something to silently de-duplicate.
_V4_STATEMENTS: list[str] = [
    "CREATE UNIQUE INDEX task_live_execution_by_idea "
    "ON task(json_extract(data, '$.payload.idea_id')) "
    "WHERE kind = 'execution' "
    "AND state IN ('pending', 'claimed', 'submitted')",
    "CREATE UNIQUE INDEX task_live_evaluation_by_variant "
    "ON task(json_extract(data, '$.payload.variant_id')) "
    "WHERE kind = 'evaluation' "
    "AND state IN ('pending', 'claimed', 'submitted')",
]


def _apply_v4(conn: sqlite3.Connection) -> None:
    for stmt in _V4_STATEMENTS:
        conn.execute(stmt)


# 12a-3: the experiment row gains a `state` column carrying the
# lifecycle state per `02-data-model.md` §2.5, and a `created_at`
# column so termination policies that key off wall-time work without
# extra plumbing. Pre-12a-3 rows pick up `state='running'` (every
# unterminated experiment) and a sentinel `created_at` that signals
# "we don't actually know" — deployments that care can rewrite the
# row out-of-band. The `dispatch_mode` field is patched in place:
# any unknown extension keys persisted under `02-data-model.md` §2.4
# forward-compatibility are preserved, and the new `termination` key
# is added only when missing (using `json_patch`, an RFC 7396 merge
# that keys-in-target win over keys-in-source). A naive
# whole-column REWRITE would silently clobber forward-compat keys,
# violating the §2.4 tolerance contract.
_V5_STATEMENTS: list[str] = [
    "ALTER TABLE experiment ADD COLUMN state text NOT NULL DEFAULT 'running'",
    "ALTER TABLE experiment ADD COLUMN created_at text NOT NULL "
    "DEFAULT '1970-01-01T00:00:00Z'",
    # `json_patch(target, source)` returns the source merged ON TOP
    # of target — meaning keys present in both are taken from
    # `source`. To preserve target's existing values for the four
    # operational keys while ADDING the new `termination` key only
    # when missing, we patch in only `{"termination": "manual"}`.
    # Existing `termination` values (if a deployment somehow already
    # set one) and all other keys round-trip unchanged.
    "UPDATE experiment SET dispatch_mode = "
    "json_patch(dispatch_mode, '{\"termination\":\"manual\"}') "
    "WHERE json_extract(dispatch_mode, '$.termination') IS NULL",
]


def _apply_v5(conn: sqlite3.Connection) -> None:
    for stmt in _V5_STATEMENTS:
        conn.execute(stmt)


# 12b: the experiment row gains an `imported_from` column carrying the
# JSON-serialized `ImportProvenance` shape per `02-data-model.md` §2.5.
# NULL on rows produced by native creation; populated on rows produced
# by `import_checkpoint` (`10-checkpoints.md` §10). The column is
# `text NULL` so pre-12b rows keep the null sentinel; native creation
# in 12b+ also writes NULL.
_V6_STATEMENTS: list[str] = [
    "ALTER TABLE experiment ADD COLUMN imported_from text",
]


def _apply_v6(conn: sqlite3.Connection) -> None:
    for stmt in _V6_STATEMENTS:
        conn.execute(stmt)


# Issue #122: the experiment row gains a `base_commit_sha` column
# carrying the experiment seed commit per `02-data-model.md` §2.5 / §9.4.
# NULL on pre-#122 rows and on rows whose deployment did not supply a
# seed; populated at experiment init (native creation) or on
# `import_checkpoint` (round-trip). The orchestrator reads it to create
# the baseline variant.
_V7_STATEMENTS: list[str] = [
    "ALTER TABLE experiment ADD COLUMN base_commit_sha text",
]


def _apply_v7(conn: sqlite3.Connection) -> None:
    for stmt in _V7_STATEMENTS:
        conn.execute(stmt)


# Issue #166: the artifact-metadata store. `data` carries the canonical
# ArtifactMetadata JSON (`spec/v0/schemas/artifact-metadata.schema.json`);
# the bytes themselves live in a separate ArtifactBackend, not here. No
# event accompanies an artifact row (the artifact store is distinct from
# the event log — `08-storage.md` §5).
_V8_STATEMENTS: list[str] = [
    """
    CREATE TABLE artifact (
        opaque_id TEXT NOT NULL PRIMARY KEY,
        data TEXT NOT NULL
    )
    """,
]


def _apply_v8(conn: sqlite3.Connection) -> None:
    for stmt in _V8_STATEMENTS:
        conn.execute(stmt)


_MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [
    _apply_v1,
    _apply_v2,
    _apply_v3,
    _apply_v4,
    _apply_v5,
    _apply_v6,
    _apply_v7,
    _apply_v8,
]


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
