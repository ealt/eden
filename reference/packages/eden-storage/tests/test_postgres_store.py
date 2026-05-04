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

import os
import secrets
from collections.abc import Iterator

import pytest
from eden_contracts import EvaluationSchema
from eden_storage import InvalidPrecondition, PostgresStore

_DSN = os.environ.get("EDEN_TEST_POSTGRES_DSN") or None

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
    store = PostgresStore("exp-1", schema_dsn, evaluation_schema=evaluation)
    store.create_ideate_task("ideate-0001")
    store.close()

    # Reopen — must succeed and see the persisted task.
    reopened = PostgresStore("exp-1", schema_dsn, evaluation_schema=evaluation)
    try:
        task = reopened.read_task("ideate-0001")
        assert task is not None
        assert task.task_id == "ideate-0001"
    finally:
        reopened.close()


def test_reopen_with_different_experiment_id_rejected(schema_dsn: str) -> None:
    """Chapter 8 §4.2 — experiment_id is part of the database identity."""
    evaluation=EvaluationSchema.model_validate({"score": "real"})
    PostgresStore("exp-A", schema_dsn, evaluation_schema=evaluation).close()

    with pytest.raises(InvalidPrecondition):
        PostgresStore("exp-B", schema_dsn, evaluation_schema=evaluation)


def test_reopen_with_changed_evaluation_schema_rejected(schema_dsn: str) -> None:
    """Chapter 8 §4.2 — evaluation_schema MUST NOT change for the lifetime of an experiment."""
    PostgresStore(
        "exp-1",
        schema_dsn,
        evaluation_schema=EvaluationSchema.model_validate({"score": "real"}),
    ).close()

    with pytest.raises(InvalidPrecondition):
        PostgresStore(
            "exp-1",
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
    PostgresStore("exp-1", schema_dsn, evaluation_schema=evaluation).close()

    reopened = PostgresStore("exp-1", schema_dsn)
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
    store = PostgresStore("exp-1", schema_dsn, evaluation_schema=evaluation)
    store.create_ideate_task("ideate-0001")
    first_event_count = len(list(store.events()))
    store.close()

    reopened = PostgresStore("exp-1", schema_dsn, evaluation_schema=evaluation)
    try:
        # Create another task — exercising another event insert. If
        # the counter restarted from 1, the second store would
        # collide on `evt-000001` (which the first store already
        # used).
        reopened.create_ideate_task("ideate-0002")
        all_events = list(reopened.events())
        assert len(all_events) > first_event_count
    finally:
        reopened.close()
