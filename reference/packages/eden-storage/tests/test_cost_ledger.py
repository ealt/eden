"""Tests for the reference-only cost ledger (issue #343).

Runs against every backend via the ``make_store`` fixture, because the
ledger's two guarantees — first-write-wins idempotency and a stable
cross-backend read order — are exactly the ones a per-backend
implementation can get subtly wrong.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from eden_storage import (
    CostEntry,
    InvalidPrecondition,
    Store,
    cost_entry_id,
)


def _entry(store: Store, entry_id: str, **overrides: Any) -> CostEntry:
    fields: dict[str, Any] = {
        "entry_id": entry_id,
        "experiment_id": store.experiment_id,
        "role": "executor",
        "source": "claude-code-stream-json",
        "task_id": "execution-1",
        "variant_id": "variant-1",
        "idea_id": "idea-1",
        "model": "claude-sonnet-4-6",
        "total_cost_usd": 0.125,
        "input_tokens": 6,
        "output_tokens": 637,
        "cache_creation_input_tokens": 16584,
        "cache_read_input_tokens": 47413,
        "num_turns": 4,
        "duration_ms": 18118,
    }
    fields.update(overrides)
    return CostEntry.model_validate(fields)


def test_record_and_read_round_trips_every_field(
    make_store: Callable[..., Store],
) -> None:
    """A recorded entry reads back with every figure intact."""
    store = make_store()
    store.record_cost(_entry(store, "e1"))

    (read,) = store.list_cost_entries()
    assert read.entry_id == "e1"
    assert read.role == "executor"
    assert read.source == "claude-code-stream-json"
    assert read.task_id == "execution-1"
    assert read.variant_id == "variant-1"
    assert read.idea_id == "idea-1"
    assert read.model == "claude-sonnet-4-6"
    assert read.total_cost_usd == pytest.approx(0.125)
    assert read.input_tokens == 6
    assert read.output_tokens == 637
    assert read.cache_creation_input_tokens == 16584
    assert read.cache_read_input_tokens == 47413
    assert read.num_turns == 4
    assert read.duration_ms == 18118


def test_recorded_at_is_store_stamped(
    make_store: Callable[..., Store],
) -> None:
    """The store stamps ``recorded_at``; a caller-supplied value is ignored."""
    store = make_store()
    store.record_cost(_entry(store, "e1"))
    store.record_cost(
        _entry(store, "e2", recorded_at="1999-01-01T00:00:00.000Z")
    )

    stamped = {e.entry_id: e.recorded_at for e in store.list_cost_entries()}
    assert stamped["e1"] is not None
    assert stamped["e2"] != "1999-01-01T00:00:00.000Z"


def test_repeat_entry_id_is_first_write_wins(
    make_store: Callable[..., Store],
) -> None:
    """Re-recording the same key never duplicates and never overwrites.

    This is what makes a worker host's retry-after-transport-failure
    safe: a second write of the same attempt must not double the
    experiment's reported spend.
    """
    store = make_store()
    store.record_cost(_entry(store, "e1", total_cost_usd=0.125))
    store.record_cost(_entry(store, "e1", total_cost_usd=99.0))

    (read,) = store.list_cost_entries()
    assert read.total_cost_usd == pytest.approx(0.125)


def test_partial_entry_is_recorded_not_dropped(
    make_store: Callable[..., Store],
) -> None:
    """A source that reports only some figures still lands a row."""
    store = make_store()
    store.record_cost(
        CostEntry(
            entry_id="e1",
            experiment_id=store.experiment_id,
            role="ideator",
            source="worker-reported",
            task_id="ideation-1",
            input_tokens=100,
            output_tokens=20,
        )
    )

    (read,) = store.list_cost_entries()
    assert read.total_cost_usd is None
    assert read.variant_id is None
    assert read.input_tokens == 100


def test_filters_by_role_and_variant(
    make_store: Callable[..., Store],
) -> None:
    """Both filters narrow, and they compose."""
    store = make_store()
    store.record_cost(_entry(store, "e1", role="executor", variant_id="v1"))
    store.record_cost(_entry(store, "e2", role="evaluator", variant_id="v1"))
    store.record_cost(_entry(store, "e3", role="executor", variant_id="v2"))

    assert {e.entry_id for e in store.list_cost_entries()} == {"e1", "e2", "e3"}
    assert {e.entry_id for e in store.list_cost_entries(role="executor")} == {
        "e1",
        "e3",
    }
    assert {e.entry_id for e in store.list_cost_entries(variant_id="v1")} == {
        "e1",
        "e2",
    }
    assert {
        e.entry_id
        for e in store.list_cost_entries(role="executor", variant_id="v1")
    } == {"e1"}


def test_unknown_role_filter_returns_empty(
    make_store: Callable[..., Store],
) -> None:
    """A filter that matches nothing is empty, not an error."""
    store = make_store()
    store.record_cost(_entry(store, "e1"))
    assert store.list_cost_entries(role="ideator") == []


def test_read_order_is_recorded_at_then_entry_id(
    make_store: Callable[..., Store],
) -> None:
    """Reads are ordered deterministically across every backend.

    Recorded in an order that is neither insertion-sorted by id nor
    reverse, so an accidental ``ORDER BY entry_id`` or an
    insertion-order-only backend both diverge from the contract.
    """
    store = make_store()
    for entry_id in ("e3", "e1", "e2"):
        store.record_cost(_entry(store, entry_id))

    order = [(e.recorded_at, e.entry_id) for e in store.list_cost_entries()]
    assert order == sorted(order)


def test_experiment_id_mismatch_is_rejected(
    make_store: Callable[..., Store],
) -> None:
    """An entry for another experiment never lands in this ledger."""
    store = make_store()
    with pytest.raises(InvalidPrecondition):
        store.record_cost(_entry(store, "e1", experiment_id="exp_other"))
    assert store.list_cost_entries() == []


def test_cost_entries_emit_no_events(
    make_store: Callable[..., Store],
) -> None:
    """Recording cost is bookkeeping, not a protocol state change.

    The chapter-5 §2 transactional invariant pairs a state change with
    an event; a cost row has no state change to pair with, and emitting
    an unregistered type would violate the closed v0 registry.
    """
    store = make_store()
    before = len(store.events())
    store.record_cost(_entry(store, "e1"))
    assert len(store.events()) == before


def test_cost_entry_id_is_per_attempt() -> None:
    """The derived key separates roles and attempts."""
    assert cost_entry_id(role="executor", attempt_key="variant-1") == (
        "cost-executor-variant-1"
    )
    assert cost_entry_id(role="executor", attempt_key="variant-1") != (
        cost_entry_id(role="evaluator", attempt_key="variant-1")
    )
    assert cost_entry_id(role="executor", attempt_key="variant-1") != (
        cost_entry_id(role="executor", attempt_key="variant-2")
    )


def test_unknown_field_is_rejected() -> None:
    """``extra="forbid"`` keeps the ledger from absorbing typos.

    A silently-absorbed ``cost_usd`` (vs ``total_cost_usd``) would read
    as a zero-cost run rather than an error.
    """
    with pytest.raises(ValueError, match="cost_usd"):
        CostEntry.model_validate(
            {
                "entry_id": "e1",
                "experiment_id": "exp_1",
                "role": "executor",
                "source": "worker-reported",
                "task_id": "t1",
                "cost_usd": 0.5,
            }
        )


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("total_cost_usd", -1.0, "greater than or equal to 0"),
        ("input_tokens", -1, "greater than or equal to 0"),
        ("num_turns", -1, "greater than or equal to 0"),
        ("role", "integrator", "role"),
        ("source", "some-other-vendor", "source"),
        ("entry_id", "", "entry_id"),
    ],
)
def test_invalid_values_are_rejected(field: str, value: Any, match: str) -> None:
    """Negative spend, unknown roles, and unknown sources are all errors."""
    fields: dict[str, Any] = {
        "entry_id": "e1",
        "experiment_id": "exp_1",
        "role": "executor",
        "source": "worker-reported",
        "task_id": "t1",
        "total_cost_usd": 1.0,
    }
    fields[field] = value
    with pytest.raises(ValueError, match=match):
        CostEntry.model_validate(fields)


def test_postgres_cost_primitives_shadow_the_abstract_stubs() -> None:
    """``PostgresStore`` resolves its cost primitives to the sibling mixin.

    The Postgres ledger primitives live in
    [`_postgres_cost.py`](../src/eden_storage/_postgres_cost.py) and reach
    the backend by MRO position, so a bases reorder would silently route
    to ``_StoreCore``'s ``NotImplementedError`` stubs. Everything else
    about the Postgres backend needs a live server (the ``postgres`` rows
    of ``make_store`` skip without ``EDEN_TEST_POSTGRES_DSN``); this
    check does not.
    """
    from eden_storage._base import _StoreCore
    from eden_storage._postgres_cost import _PostgresCostMixin
    from eden_storage.postgres import PostgresStore

    assert PostgresStore.__mro__[1] is _PostgresCostMixin
    for name in ("_get_cost_entry", "_iter_cost_entries", "_insert_cost_entry"):
        resolved = getattr(PostgresStore, name)
        assert resolved is getattr(_PostgresCostMixin, name)
        assert resolved is not getattr(_StoreCore, name, None)
