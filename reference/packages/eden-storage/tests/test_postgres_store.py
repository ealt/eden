"""Postgres-specific tests for ``PostgresStore``.

The parametrized conformance suite in the rest of this directory
runs every public-API scenario against all three backends (memory,
sqlite, postgres). This file holds **postgres-only** tests for
behavior that's specific to the Postgres backend: reopen-on-an-
existing-non-default-schema, migration idempotency, and DSN
handling. Skipped without ``EDEN_TEST_POSTGRES_DSN``; CI's
``python-test-postgres`` job sets it.
"""

from __future__ import annotations

import json
import os
import secrets
from collections.abc import Iterator

import pytest
from eden_contracts import EvaluationSchema, Variant, mint_opaque_id
from eden_storage import InvalidPrecondition, PostgresStore
from eden_storage._postgres_views import (
    _COMMON_COLUMN_EXPRS,
    VARIANT_UNPACKED_VIEW,
)

_DSN = os.environ.get("EDEN_TEST_POSTGRES_DSN") or None

# Opaque identity ids for the postgres-only tests (issue #128). The same
# `_EXP1` flows into a test's source + reopen so the §4.2 identity check
# sees a match; `_EXP_A` / `_EXP_B` are distinct for the mismatch test.
_EXP1 = mint_opaque_id("exp")
_EXP_A = mint_opaque_id("exp")
_EXP_B = mint_opaque_id("exp")
_WKR_EXEC = mint_opaque_id("wkr")
_WKR_EVAL = mint_opaque_id("wkr")

pytestmark = pytest.mark.skipif(
    _DSN is None,
    reason="EDEN_TEST_POSTGRES_DSN not set",
)


@pytest.fixture
def schema_dsn() -> Iterator[str]:
    """Yield a DSN scoped to a fresh schema; drop it on teardown.

    Mirrors the per-test schema isolation in ``conftest.py`` so
    these tests can construct ``PostgresStore`` directly without
    going through the conformance-suite factory.
    """
    import psycopg
    from psycopg import sql

    schema = f"test_pg_{secrets.token_hex(8)}"
    assert _DSN is not None
    with psycopg.connect(_DSN, autocommit=True) as setup:
        setup.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
        )

    sep = "&" if "?" in _DSN else "?"
    scoped = f"{_DSN}{sep}options=-c%20search_path%3D{schema}"
    try:
        yield scoped
    finally:
        with psycopg.connect(_DSN, autocommit=True) as drop:
            drop.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema)
                )
            )


def test_reopen_on_non_default_schema_does_not_re_run_migrations(
    schema_dsn: str,
) -> None:
    """A second open against the same non-default schema must be a no-op.

    Regression: before search-path-aware ``current_version``,
    reopening an existing PostgresStore on a non-`public` schema
    re-ran v1 migrations and failed on duplicate-table.
    """
    evaluation=EvaluationSchema.model_validate({"score": "real"})
    store = PostgresStore(_EXP1, schema_dsn, evaluation_schema=evaluation)
    store.create_ideation_task("ideation-0001")
    store.close()

    # Reopen — must succeed and see the persisted task.
    reopened = PostgresStore(_EXP1, schema_dsn, evaluation_schema=evaluation)
    try:
        task = reopened.read_task("ideation-0001")
        assert task is not None
        assert task.task_id == "ideation-0001"
    finally:
        reopened.close()


def test_reopen_with_different_experiment_id_rejected(schema_dsn: str) -> None:
    """Chapter 8 §4.2 — experiment_id is part of the database identity."""
    evaluation=EvaluationSchema.model_validate({"score": "real"})
    PostgresStore(_EXP_A, schema_dsn, evaluation_schema=evaluation).close()

    with pytest.raises(InvalidPrecondition):
        PostgresStore(_EXP_B, schema_dsn, evaluation_schema=evaluation)


def test_reopen_with_changed_evaluation_schema_rejected(schema_dsn: str) -> None:
    """Chapter 8 §4.2 — evaluation_schema MUST NOT change for the lifetime of an experiment."""
    PostgresStore(
        _EXP1,
        schema_dsn,
        evaluation_schema=EvaluationSchema.model_validate({"score": "real"}),
    ).close()

    with pytest.raises(InvalidPrecondition):
        PostgresStore(
            _EXP1,
            schema_dsn,
            evaluation_schema=EvaluationSchema.model_validate(
                {"score": "real", "extra": "integer"}
            ),
        )


def test_reopen_inherits_persisted_evaluation_schema(schema_dsn: str) -> None:
    """Reopen with no schema arg inherits whatever was persisted.

    Validates inheritance by exercising evaluation validation after
    reopen — `validate_evaluation` returns a no-op for `None` schemas,
    so a reopened store that *failed* to inherit the schema would
    accept `{"unknown": 1.0}`. With inheritance, an unknown metric
    name raises `InvalidPrecondition`.
    """
    evaluation=EvaluationSchema.model_validate({"score": "real"})
    PostgresStore(_EXP1, schema_dsn, evaluation_schema=evaluation).close()

    reopened = PostgresStore(_EXP1, schema_dsn)
    try:
        # The persisted schema knows only `score`; an unknown
        # metric name should raise.
        with pytest.raises(InvalidPrecondition):
            reopened.validate_evaluation({"unknown": 1.0})
        # The valid metric still passes.
        reopened.validate_evaluation({"score": 0.5})
    finally:
        reopened.close()


def test_event_id_counter_resumes_across_reopen(schema_dsn: str) -> None:
    """The default event-id counter resumes from MAX(seq) + 1 on reopen.

    Without resumption, two stores on the same database would emit
    duplicate ``event_id`` values and violate the UNIQUE constraint.
    """
    evaluation=EvaluationSchema.model_validate({"score": "real"})
    store = PostgresStore(_EXP1, schema_dsn, evaluation_schema=evaluation)
    store.create_ideation_task("ideation-0001")
    first_event_count = len(list(store.events()))
    store.close()

    reopened = PostgresStore(_EXP1, schema_dsn, evaluation_schema=evaluation)
    try:
        # Create another task — exercising another event insert. If
        # the counter restarted from 1, the second store would
        # collide on `evt-000001` (which the first store already
        # used).
        reopened.create_ideation_task("ideation-0002")
        all_events = list(reopened.events())
        assert len(all_events) > first_event_count
    finally:
        reopened.close()


# --------------------------------------------------------------------------
# Issue #124 — `variant_unpacked` Adminer-convenience view.
# --------------------------------------------------------------------------


def _insert_variant_row(conn: object, variant: Variant) -> None:
    """Insert a Variant directly into the `variant` table.

    Bypasses the state machine (we're testing the view, not the
    lifecycle): the view's contract is "what's in `data` comes out as
    typed columns" regardless of how the row got there.
    """
    payload = json.dumps(variant.model_dump(mode="json", exclude_none=True))
    with conn.cursor() as cur:  # type: ignore[attr-defined]
        cur.execute(
            "INSERT INTO variant(variant_id, status, data) VALUES (%s, %s, %s)",
            (variant.variant_id, variant.status, payload),
        )


def test_variant_unpacked_view_unpacks_common_columns(schema_dsn: str) -> None:
    """The view exposes every public Variant field as a scalar column."""
    schema = EvaluationSchema.model_validate({"score": "real"})
    store = PostgresStore(_EXP1, schema_dsn, evaluation_schema=schema)
    try:
        variant = Variant(
            variant_id="var-001",
            experiment_id=_EXP1,
            idea_id="idea-001",
            status="success",
            parent_commits=["a" * 40],
            branch="work/idea-001",
            commit_sha="b" * 40,
            variant_commit_sha="c" * 40,
            artifacts_uri="file:///tmp/artifacts/var-001",
            description="canonical test variant",
            evaluation={"score": 0.875},
            started_at="2026-01-01T00:00:00Z",
            completed_at="2026-01-01T00:05:00Z",
            executed_by=_WKR_EXEC,
            evaluated_by=_WKR_EVAL,
        )
        _insert_variant_row(store._conn, variant)

        with store._conn.cursor() as cur:
            cur.execute(f"SELECT * FROM {VARIANT_UNPACKED_VIEW} WHERE variant_id = %s",
                        (variant.variant_id,))
            description = cur.description
            assert description is not None
            cols: list[str] = [str(d[0]) for d in description]
            fetched = cur.fetchone()
            assert fetched is not None
            row = dict(zip(cols, fetched, strict=True))

        assert row["variant_id"] == "var-001"
        assert row["status"] == "success"
        assert row["experiment_id"] == _EXP1
        assert row["idea_id"] == "idea-001"
        assert row["branch"] == "work/idea-001"
        assert row["commit_sha"] == "b" * 40
        assert row["variant_commit_sha"] == "c" * 40
        assert row["artifacts_uri"] == "file:///tmp/artifacts/var-001"
        assert row["description"] == "canonical test variant"
        assert row["executed_by"] == _WKR_EXEC
        assert row["evaluated_by"] == _WKR_EVAL
        assert row["started_at"] == "2026-01-01T00:00:00Z"
        assert row["completed_at"] == "2026-01-01T00:05:00Z"
        # parent_commits and evaluation are JSONB sub-trees.
        assert row["parent_commits"] == ["a" * 40]
        assert row["evaluation"] == {"score": 0.875}
    finally:
        store.close()


def test_variant_unpacked_view_unpacks_metric_columns_with_types(
    schema_dsn: str,
) -> None:
    """Per-metric columns are generated from `evaluation_schema` with declared types."""
    schema = EvaluationSchema.model_validate(
        {
            "correctness": "real",
            "effort_minutes": "integer",
            "notes": "text",
        }
    )
    store = PostgresStore(_EXP1, schema_dsn, evaluation_schema=schema)
    try:
        variant = Variant(
            variant_id="var-001",
            experiment_id=_EXP1,
            idea_id="idea-001",
            status="success",
            parent_commits=["a" * 40],
            evaluation={
                "correctness": 0.875,
                "effort_minutes": 17,
                "notes": "edge-case OK",
            },
            started_at="2026-01-01T00:00:00Z",
        )
        _insert_variant_row(store._conn, variant)

        with store._conn.cursor() as cur:
            cur.execute(
                f'SELECT correctness, effort_minutes, notes '
                f"FROM {VARIANT_UNPACKED_VIEW} WHERE variant_id = %s",
                (variant.variant_id,),
            )
            description = cur.description
            assert description is not None
            cols = [str(d[0]) for d in description]
            assert cols == ["correctness", "effort_minutes", "notes"]
            fetched = cur.fetchone()
            assert fetched is not None
            correctness, effort_minutes, notes = fetched

        # Python types reflect Postgres types: float / int / str.
        assert isinstance(correctness, float)
        assert correctness == pytest.approx(0.875)
        assert isinstance(effort_minutes, int)
        assert effort_minutes == 17
        assert isinstance(notes, str)
        assert notes == "edge-case OK"

        # information_schema confirms the declared Postgres types.
        with store._conn.cursor() as cur:
            cur.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name = %s "
                "AND column_name IN ('correctness', 'effort_minutes', 'notes')",
                (VARIANT_UNPACKED_VIEW,),
            )
            type_by_col = dict(cur.fetchall())
        assert type_by_col["correctness"] == "double precision"
        assert type_by_col["effort_minutes"] == "integer"
        assert type_by_col["notes"] == "text"
    finally:
        store.close()


def test_variant_unpacked_view_without_evaluation_schema(schema_dsn: str) -> None:
    """When no schema is declared, the view still exists with only the common columns."""
    store = PostgresStore(_EXP1, schema_dsn)
    try:
        with store._conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = %s ORDER BY ordinal_position",
                (VARIANT_UNPACKED_VIEW,),
            )
            cols = [r[0] for r in cur.fetchall()]
        # Exactly the common columns, in order.
        assert cols == [alias for alias, _ in _COMMON_COLUMN_EXPRS]
    finally:
        store.close()


def test_variant_unpacked_view_is_replaced_on_reopen(schema_dsn: str) -> None:
    """Reopening with a different schema (rare; only valid pre-variant) replaces the view.

    The CREATE OR REPLACE VIEW invocation in PostgresStore.__init__ is
    intentionally idempotent + replaceable: the view tracks
    `evaluation_schema`. Walks the back-door path of dropping the
    experiment row so the second open re-INSERTs with a different schema.
    """
    schema_v1 = EvaluationSchema.model_validate({"score": "real"})
    PostgresStore(_EXP1, schema_dsn, evaluation_schema=schema_v1).close()

    # Reset the experiment row so __init__ accepts a different schema.
    import psycopg

    with psycopg.connect(schema_dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM experiment")

    schema_v2 = EvaluationSchema.model_validate(
        {"accuracy": "real", "label": "text"}
    )
    store = PostgresStore(_EXP1, schema_dsn, evaluation_schema=schema_v2)
    try:
        with store._conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = %s",
                (VARIANT_UNPACKED_VIEW,),
            )
            cols = {r[0] for r in cur.fetchall()}
        assert "score" not in cols
        assert {"accuracy", "label"} <= cols
    finally:
        store.close()


