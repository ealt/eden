"""Postgres-independent tests for :mod:`eden_storage._postgres_views`.

The integration tests that exercise the actual ``variant_unpacked``
view live in ``test_postgres_store.py`` (skipped without
``EDEN_TEST_POSTGRES_DSN``). The tests here exercise pure-Python
contracts of the view-DDL builder and run on every CI job.
"""

from __future__ import annotations

from eden_contracts import EvaluationSchema, Variant
from eden_storage._postgres_views import (
    _COMMON_COLUMN_EXPRS,
    _metric_column_exprs,
)


def test_common_columns_cover_every_variant_field() -> None:
    """The view's common-column list covers every public ``Variant`` field.

    Adding a field to :class:`eden_contracts.Variant` without extending
    ``_COMMON_COLUMN_EXPRS`` would leave operators blind to the new
    field in Adminer — surface that drift as a test failure.
    """
    view_columns = {alias for alias, _ in _COMMON_COLUMN_EXPRS}
    variant_fields = set(Variant.model_fields.keys())
    missing = variant_fields - view_columns
    assert missing == set(), (
        f"variant_unpacked view is missing columns for Variant fields: {missing}. "
        "Extend _COMMON_COLUMN_EXPRS in _postgres_views.py."
    )


def test_metric_columns_map_types_correctly() -> None:
    """Each declared metric type translates to its Postgres column type."""
    schema = EvaluationSchema.model_validate(
        {"correctness": "real", "effort_minutes": "integer", "notes": "text"}
    )
    cols = dict(_metric_column_exprs(schema))
    # double precision for real (wider than single-precision REAL).
    assert "::double precision" in cols["correctness"]
    assert "::integer" in cols["effort_minutes"]
    # text metric stays a `->>` extraction with no trailing cast —
    # the leading `data::jsonb` itself is a cast, so check the suffix.
    assert cols["notes"].endswith("->> 'notes'")
    assert "::text" not in cols["notes"]


def test_no_metric_columns_when_schema_absent() -> None:
    """Stores opened without an evaluation_schema still get the base view."""
    assert _metric_column_exprs(None) == []
