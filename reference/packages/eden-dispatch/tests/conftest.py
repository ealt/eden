"""Shared fixtures for conformance scenarios."""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterator

import pytest
from eden_dispatch import InMemoryStore


@pytest.fixture
def token_sequence() -> Callable[[], str]:
    """Deterministic claim-token factory used by tests that compare tokens.

    Using a predictable sequence makes assertions like "reclaimed
    token is rejected" readable in test output.
    """
    counter = itertools.count(1)
    return lambda: f"tok-{next(counter):06d}"


@pytest.fixture
def make_store(
    token_sequence: Callable[[], str],
) -> Callable[..., InMemoryStore]:
    """Factory for stores with deterministic tokens and monotonic timestamps."""

    def _make(experiment_id: str = "exp-test") -> InMemoryStore:
        return InMemoryStore(
            experiment_id=experiment_id,
            token_factory=token_sequence,
        )

    return _make


@pytest.fixture
def ids() -> Iterator[str]:
    """Monotonic ID allocator for tasks, proposals, trials."""
    counter = itertools.count(1)
    return (f"id-{next(counter):04d}" for _ in iter(int, 1))
