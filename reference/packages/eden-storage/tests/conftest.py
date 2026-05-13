"""Shared fixtures for the ``Store`` conformance suite.

Every test that takes ``make_store`` runs against the full set of
reference backends via pytest parametrization. ``memory`` and
``sqlite`` always run; ``postgres`` runs when the
``EDEN_TEST_POSTGRES_DSN`` environment variable points at a live
database (CI sets it). Adding a future backend means: implement the
``Store`` Protocol, add a factory here, and let the existing
scenarios surface drift in tests rather than in production.
"""

from __future__ import annotations

import contextlib
import itertools
import os
import secrets
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from eden_storage import InMemoryStore, PostgresStore, SqliteStore, Store

# Workers pre-registered against every freshly-built store. The
# claim-time RBAC enforcement in `Store.claim` (spec §3.5 step 2)
# requires the caller's worker_id to exist in the registry; auto-
# registering this small set keeps the legacy state-machine tests
# focused on transition shapes rather than registration plumbing.
# Tests that exercise the RBAC checks themselves use bespoke
# worker_ids and register them inline.
_DEFAULT_WORKERS: tuple[str, ...] = (
    "test-worker",
    "worker-a",
    "worker-b",
    "ideator-1",
    "ideator-2",
    "ideator-w",
    "ideator-x",
    "executor-w",
    "executor-bootstrap",
    "execution-bootstrap",
    "evaluator-w",
    "evaluator-other",
    "impl-worker",
)


def _seed_default_workers(store: Store) -> None:
    """Register the standard test-worker set on a fresh store.

    Idempotent on existing rows (per spec §6.3) so suites that share
    a backend (postgres-per-schema) don't fail on second-call.
    """
    for wid in _DEFAULT_WORKERS:
        store.register_worker(wid)


def _memory_factory(
    tmp_path: Path,  # noqa: ARG001 - accepted for uniform factory signature
) -> Callable[..., Store]:
    def _make(
        experiment_id: str = "exp-test",
        *,
        seed_workers: bool = True,
        **kwargs: Any,
    ) -> Store:
        store = InMemoryStore(
            experiment_id=experiment_id,
            **kwargs,
        )
        if seed_workers:
            _seed_default_workers(store)
        return store

    return _make


def _sqlite_factory(
    tmp_path: Path,
) -> Callable[..., Store]:
    counter = itertools.count(1)

    def _make(
        experiment_id: str = "exp-test",
        *,
        seed_workers: bool = True,
        **kwargs: Any,
    ) -> Store:
        # Each call gets its own database so the tests that create
        # multiple stores (e.g. an AlreadyExists collision test)
        # don't share state.
        db_path = tmp_path / f"store-{next(counter):04d}.db"
        store = SqliteStore(
            experiment_id,
            db_path,
            **kwargs,
        )
        if seed_workers:
            _seed_default_workers(store)
        return store

    return _make


def _postgres_factory(
    tmp_path: Path,  # noqa: ARG001 - uniform factory signature
    dsn: str,
    cleanup: list[Callable[[], None]],
) -> Callable[..., Store]:
    """Build a factory that issues PostgresStores on a per-test schema.

    Each call creates a fresh ``test_<rand>`` schema, sets it as the
    sole entry on ``search_path``, runs the schema migration there,
    and registers a teardown that drops the schema. Two factory
    calls in the same test (e.g. AlreadyExists collision tests) get
    distinct schemas, mirroring SqliteStore's per-call distinct
    db-file behavior.
    """
    import psycopg
    from psycopg import sql

    counter = itertools.count(1)

    def _make(
        experiment_id: str = "exp-test",
        *,
        seed_workers: bool = True,
        **kwargs: Any,
    ) -> Store:
        schema = f"test_{secrets.token_hex(8)}_{next(counter):04d}"
        # Pre-create the schema. PostgresStore opens its own
        # connection with `search_path` overridden so all DDL +
        # DML lands inside it.
        with psycopg.connect(dsn, autocommit=True) as setup:
            setup.execute(
                sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
            )

        store_dsn = dsn
        # psycopg honors `options=-csearch_path=...` in the libpq
        # URL; appending it as a query keyword is ergonomic.
        sep = "&" if "?" in store_dsn else "?"
        store_dsn = (
            f"{store_dsn}{sep}options="
            f"-c%20search_path%3D{schema}"
        )
        store = PostgresStore(
            experiment_id,
            store_dsn,
            **kwargs,
        )
        if seed_workers:
            _seed_default_workers(store)

        def _drop() -> None:
            store.close()
            with psycopg.connect(dsn, autocommit=True) as drop:
                drop.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                        sql.Identifier(schema)
                    )
                )

        cleanup.append(_drop)
        return store

    return _make


def _postgres_dsn() -> str | None:
    return os.environ.get("EDEN_TEST_POSTGRES_DSN") or None


_BACKEND_NAMES: list[str] = ["memory", "sqlite", "postgres"]


@pytest.fixture(params=_BACKEND_NAMES, ids=_BACKEND_NAMES)
def make_store(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> Iterator[Callable[..., Store]]:
    """Factory fixture parametrized across every backend.

    Tests that take ``make_store`` run once per backend. The
    ``postgres`` parametrization skips when ``EDEN_TEST_POSTGRES_DSN``
    is unset; CI sets it and runs the parametrized rows.
    """
    name = request.param
    if name == "memory":
        yield _memory_factory(tmp_path)
        return
    if name == "sqlite":
        yield _sqlite_factory(tmp_path)
        return
    if name == "postgres":
        dsn = _postgres_dsn()
        if dsn is None:
            pytest.skip("EDEN_TEST_POSTGRES_DSN not set")
        cleanup: list[Callable[[], None]] = []
        try:
            yield _postgres_factory(tmp_path, dsn, cleanup)
        finally:
            for fn in reversed(cleanup):
                with contextlib.suppress(Exception):
                    fn()
        return
    raise AssertionError(f"unknown backend {name!r}")


@pytest.fixture
def ids() -> Iterator[str]:
    """Monotonic ID allocator for tasks, ideas, variants."""
    counter = itertools.count(1)
    return (f"id-{next(counter):04d}" for _ in iter(int, 1))
