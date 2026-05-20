"""Shared fixtures for the eden-control-plane store conformance tests.

`make_store` is parametrized across `memory` and `postgres` backends.
Postgres rows skip when `EDEN_TEST_POSTGRES_DSN` is unset.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Callable, Iterator
from typing import Any

import psycopg
import pytest
from eden_control_plane import (
    ControlPlaneStore,
    InMemoryControlPlaneStore,
    PostgresControlPlaneStore,
)
from psycopg import sql


def _postgres_dsn() -> str | None:
    return os.environ.get("EDEN_TEST_POSTGRES_DSN") or None


_BACKEND_NAMES: list[str] = ["memory", "postgres"]


@pytest.fixture(params=_BACKEND_NAMES, ids=_BACKEND_NAMES)
def make_store(
    request: pytest.FixtureRequest,
) -> Iterator[Callable[..., ControlPlaneStore]]:
    """Factory fixture parametrized across backends.

    Each call returns a fresh store. For the Postgres backend, each
    call provisions an isolated schema and drops it on teardown.
    """
    name = request.param
    if name == "memory":

        def _memory_factory(**kwargs: Any) -> ControlPlaneStore:
            return InMemoryControlPlaneStore(**kwargs)

        yield _memory_factory
        return
    if name == "postgres":
        dsn = _postgres_dsn()
        if dsn is None:
            pytest.skip("EDEN_TEST_POSTGRES_DSN not set")
        cleanup: list[Callable[[], None]] = []
        schemas: list[str] = []

        def _pg_factory(**kwargs: Any) -> ControlPlaneStore:
            schema_name = f"cp_test_{os.urandom(8).hex()}"
            schemas.append(schema_name)
            # Provision an isolated schema; redirect search_path so
            # `CREATE TABLE …` lands in it. Each store gets its own
            # connection scoped to the schema.
            admin = psycopg.connect(dsn, autocommit=True)
            try:
                with admin.cursor() as cur:
                    cur.execute(
                        sql.SQL("CREATE SCHEMA {}").format(
                            sql.Identifier(schema_name)
                        )
                    )
            finally:
                admin.close()
            scoped_dsn = (
                f"{dsn}?options=-csearch_path%3D{schema_name}"
                if "?" not in dsn
                else f"{dsn}&options=-csearch_path%3D{schema_name}"
            )
            return PostgresControlPlaneStore(scoped_dsn, **kwargs)

        def _drop_schemas() -> None:
            admin = psycopg.connect(dsn, autocommit=True)
            try:
                with admin.cursor() as cur:
                    for name_ in schemas:
                        cur.execute(
                            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                                sql.Identifier(name_)
                            )
                        )
            finally:
                admin.close()

        cleanup.append(_drop_schemas)
        try:
            yield _pg_factory
        finally:
            for fn in reversed(cleanup):
                with contextlib.suppress(Exception):
                    fn()
        return
    raise AssertionError(f"unknown backend {name!r}")
