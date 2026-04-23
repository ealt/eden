"""Shared fixtures for the ``Store`` conformance suite.

Every test that takes ``make_store`` runs against **both** backends —
``InMemoryStore`` and ``SqliteStore`` — via pytest parametrization.
This is what makes "passes the conformance suite" mean something:
adding a future backend (Postgres, a remote driver) requires it to
pass every scenario here before being considered conforming.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from eden_storage import InMemoryStore, SqliteStore, Store


@pytest.fixture
def token_sequence() -> Callable[[], str]:
    """Deterministic claim-token factory for token-comparison assertions."""
    counter = itertools.count(1)
    return lambda: f"tok-{next(counter):06d}"


def _memory_factory(
    token_factory: Callable[[], str],
    tmp_path: Path,  # noqa: ARG001 - accepted for uniform factory signature
) -> Callable[..., Store]:
    def _make(experiment_id: str = "exp-test", **kwargs: Any) -> Store:
        return InMemoryStore(
            experiment_id=experiment_id,
            token_factory=token_factory,
            **kwargs,
        )

    return _make


def _sqlite_factory(
    token_factory: Callable[[], str],
    tmp_path: Path,
) -> Callable[..., Store]:
    counter = itertools.count(1)

    def _make(experiment_id: str = "exp-test", **kwargs: Any) -> Store:
        # Each call gets its own database so the tests that create
        # multiple stores (e.g. an AlreadyExists collision test)
        # don't share state.
        db_path = tmp_path / f"store-{next(counter):04d}.db"
        return SqliteStore(
            experiment_id,
            db_path,
            token_factory=token_factory,
            **kwargs,
        )

    return _make


_BACKENDS: list[tuple[str, Callable[..., Callable[..., Store]]]] = [
    ("memory", _memory_factory),
    ("sqlite", _sqlite_factory),
]


@pytest.fixture(params=[name for name, _ in _BACKENDS], ids=[name for name, _ in _BACKENDS])
def make_store(
    request: pytest.FixtureRequest,
    token_sequence: Callable[[], str],
    tmp_path: Path,
) -> Callable[..., Store]:
    """Factory fixture parametrized across every backend.

    Tests that take ``make_store`` run once per backend. Backend
    implementations that drift silently from the Protocol surface
    here rather than in production.
    """
    for name, builder in _BACKENDS:
        if name == request.param:
            return builder(token_sequence, tmp_path)
    raise AssertionError(f"unknown backend {request.param!r}")


@pytest.fixture
def ids() -> Iterator[str]:
    """Monotonic ID allocator for tasks, proposals, trials."""
    counter = itertools.count(1)
    return (f"id-{next(counter):04d}" for _ in iter(int, 1))
