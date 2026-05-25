"""Adminer-convenience Postgres views (issue #124).

The ``variant`` table stores each row as a JSON-text blob in the ``data``
column (parity with :class:`SqliteStore` — see
[`_postgres_schema.py`](_postgres_schema.py)). That shape is fine for the
``Store`` Protocol but awkward for operator exploration in Adminer or psql:
every metric pull is `(data::jsonb -> 'evaluation' ->> 'X')::T`.

This module installs a Postgres **view** named ``variant_unpacked`` that
unpacks the JSON blob into typed scalar columns — common ``Variant`` fields
plus one column per metric in the experiment's ``evaluation_schema`` with the
schema-declared type (``integer`` / ``real`` / ``text``). The view exists
alongside the base ``variant`` table; the underlying storage is unchanged.

The view is recreated on every store open (``CREATE OR REPLACE VIEW``) so a
change in ``evaluation_schema`` — rare but permitted before any variant is
recorded — picks up the new columns. The view is Postgres-only; the
InMemory and SQLite backends ignore it.
"""

from __future__ import annotations

from typing import Any

from eden_contracts import EvaluationSchema

VARIANT_UNPACKED_VIEW = "variant_unpacked"
"""Name of the operator-convenience view over the ``variant`` table."""

_METRIC_PG_TYPE: dict[str, str] = {
    "integer": "integer",
    "real": "double precision",
    "text": "text",
}
"""Map :class:`EvaluationSchema` metric types to Postgres column types.

``real`` widens to ``double precision`` so a metric like ``correctness:
0.123456789`` round-trips without single-precision truncation. The view
column type is what Adminer and psql display; storage is unchanged.
"""

_COMMON_COLUMN_EXPRS: list[tuple[str, str]] = [
    # (column_alias, SELECT expression). The expressions assume the
    # current row is `variant` — `variant_id` and `status` come from
    # real columns; everything else is unpacked from `data::jsonb`.
    ("variant_id", "variant_id"),
    ("status", "status"),
    ("experiment_id", "data::jsonb ->> 'experiment_id'"),
    ("idea_id", "data::jsonb ->> 'idea_id'"),
    ("branch", "data::jsonb ->> 'branch'"),
    ("commit_sha", "data::jsonb ->> 'commit_sha'"),
    ("variant_commit_sha", "data::jsonb ->> 'variant_commit_sha'"),
    ("parent_commits", "data::jsonb -> 'parent_commits'"),
    ("artifacts_uri", "data::jsonb ->> 'artifacts_uri'"),
    ("executor_artifacts_uri", "data::jsonb ->> 'executor_artifacts_uri'"),
    ("description", "data::jsonb ->> 'description'"),
    ("executed_by", "data::jsonb ->> 'executed_by'"),
    ("evaluated_by", "data::jsonb ->> 'evaluated_by'"),
    ("started_at", "data::jsonb ->> 'started_at'"),
    ("completed_at", "data::jsonb ->> 'completed_at'"),
    ("evaluation", "data::jsonb -> 'evaluation'"),
]
"""Common ``Variant`` columns the view always exposes.

Mirrors the public fields on :class:`eden_contracts.Variant`. Adding a
new field to :class:`Variant` requires extending this list; the test
``test_variant_unpacked_columns_cover_variant_fields`` enforces parity.
"""


def ensure_variant_unpacked_view(
    conn: Any, evaluation_schema: EvaluationSchema | None
) -> None:
    """Create or replace the ``variant_unpacked`` view on the given connection.

    ``conn`` MUST be a psycopg ``Connection`` opened against the same
    schema the ``variant`` table lives in (the caller's
    :meth:`PostgresStore.__init__` runs migrations first and then this
    function, so the table is guaranteed to exist).

    ``evaluation_schema`` is the experiment's ``EvaluationSchema``. The
    per-metric columns the view exposes are generated from it; their
    Postgres types follow :data:`_METRIC_PG_TYPE`. When
    ``evaluation_schema`` is ``None`` (test deployments that don't
    declare one), the view is still created — just without any
    per-metric columns.

    Idempotent: re-running with the same schema is a no-op; re-running
    with a different schema replaces the view in place. The base
    ``variant`` table is never touched.
    """
    metric_cols = _metric_column_exprs(evaluation_schema)
    select_lines = [f'    {expr} AS {alias}' for alias, expr in _COMMON_COLUMN_EXPRS]
    select_lines.extend(f'    {expr} AS "{alias}"' for alias, expr in metric_cols)
    select_body = ",\n".join(select_lines)
    # DROP + CREATE rather than CREATE OR REPLACE: Postgres' OR REPLACE
    # only permits additive column changes, but a reopen against an
    # experiment whose schema actually changed (only legal on an empty
    # DB) may reshape the column set arbitrarily. Dropping first is
    # safe — the view holds no state and any readonly GRANT is re-issued
    # by `provision_readonly`.
    create_ddl = (
        f"CREATE VIEW {VARIANT_UNPACKED_VIEW} AS\n"
        f"SELECT\n{select_body}\nFROM variant"
    )
    with conn.cursor() as cur:
        cur.execute(f"DROP VIEW IF EXISTS {VARIANT_UNPACKED_VIEW}")
        cur.execute(create_ddl)


def _metric_column_exprs(
    evaluation_schema: EvaluationSchema | None,
) -> list[tuple[str, str]]:
    """Return ``[(alias, expr), ...]`` for each metric in the schema.

    Metric names are pattern-restricted by
    :data:`eden_contracts.evaluation.METRIC_NAME_PATTERN`
    (``^[A-Za-z_][A-Za-z0-9_]*$``) and the reserved-names check in
    :class:`EvaluationSchema` keeps them off the common-column space,
    so inline interpolation is safe.
    """
    if evaluation_schema is None:
        return []
    cols: list[tuple[str, str]] = []
    for name, declared_type in evaluation_schema.root.items():
        pg_type = _METRIC_PG_TYPE[declared_type]
        if declared_type == "text":
            expr = f"data::jsonb -> 'evaluation' ->> '{name}'"
        else:
            expr = f"(data::jsonb -> 'evaluation' ->> '{name}')::{pg_type}"
        cols.append((name, expr))
    return cols
