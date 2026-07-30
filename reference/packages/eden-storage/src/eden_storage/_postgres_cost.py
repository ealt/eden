"""Postgres cost-ledger primitives — reference-only (issue #343).

A sibling module rather than three more methods on
[`postgres.py`](postgres.py), for the same reason
[`_postgres_schema.py`](_postgres_schema.py) and
[`_postgres_views.py`](_postgres_views.py) are siblings: the backend
module is at its size budget (`scripts/check-complexity.py`), and this
particular seam is worth having anyway — everything in here is
**non-normative**, so keeping it out of the backend's spec-tracking
surface makes that visible at file granularity.

See [`cost.py`](cost.py) for why the ledger exists as a reference
extension in the first place, and [`sqlite.py`](sqlite.py) for the twin
implementation (kept inline there; that file has room).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from .cost import CostEntry


def _serialize_model(model: CostEntry) -> str:
    """Mirror :func:`eden_storage.postgres._serialize_model`.

    Duplicated (one line) rather than imported: the backend imports this
    module, so importing back would be circular.
    """
    return json.dumps(model.model_dump(mode="json", exclude_none=True))


class _PostgresCostMixin:
    """Cost-ledger backend primitives for :class:`PostgresStore`.

    Mixed into the backend, not standalone: ``_conn`` is the store's
    autocommit connection, and every write runs inside the caller's
    ``_atomic_operation`` transaction like any other primitive.
    """

    _conn: Any
    """The owning store's psycopg connection (declared for the mixin)."""

    def _get_cost_entry(self, entry_id: str) -> CostEntry | None:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT data FROM cost_entry WHERE entry_id = %s", (entry_id,)
            )
            row = cur.fetchone()
        if row is None:
            return None
        return CostEntry.model_validate_json(row[0])

    def _iter_cost_entries(
        self, *, role: str | None = None, variant_id: str | None = None
    ) -> Iterable[CostEntry]:
        """Return ledger rows, filtered in SQL, in ``(recorded_at, entry_id)`` order.

        Both filter columns are indexed, so a per-variant rollup over a
        long-running experiment doesn't deserialize every row. Composed
        via ``psycopg.sql`` rather than an f-string because psycopg types
        its query parameter as ``LiteralString``.
        """
        from psycopg import sql

        clauses = []
        params: list[str] = []
        if role is not None:
            clauses.append(sql.SQL("role = %s"))
            params.append(role)
        if variant_id is not None:
            clauses.append(sql.SQL("variant_id = %s"))
            params.append(variant_id)
        where = (
            sql.SQL(" WHERE ") + sql.SQL(" AND ").join(clauses)
            if clauses
            else sql.SQL("")
        )
        query = (
            sql.SQL("SELECT data FROM cost_entry")
            + where
            + sql.SQL(" ORDER BY recorded_at, entry_id")
        )
        with self._conn.cursor() as cur:
            cur.execute(query, tuple(params))
            rows = cur.fetchall()
        return [CostEntry.model_validate_json(row[0]) for row in rows]

    def _insert_cost_entry(self, entry_id: str, entry: CostEntry) -> None:
        """Insert one ledger row; a repeat ``entry_id`` is a no-op.

        DO NOTHING rather than an upsert: ``record_cost`` is
        first-write-wins so a re-record after a transport failure cannot
        double-count. The ops mixin already short-circuits on a hit;
        this is the backstop for a concurrent writer.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO cost_entry(
                    entry_id, recorded_at, role, variant_id, data
                )
                VALUES(%s, %s, %s, %s, %s)
                ON CONFLICT(entry_id) DO NOTHING
                """,
                (
                    entry_id,
                    entry.recorded_at,
                    entry.role,
                    entry.variant_id,
                    _serialize_model(entry),
                ),
            )
