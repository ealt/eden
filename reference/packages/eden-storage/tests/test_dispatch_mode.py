"""``read_dispatch_mode`` / ``update_dispatch_mode`` semantics (12a-2 wave 2).

Spec: [`spec/v0/02-data-model.md`](../../../../spec/v0/02-data-model.md)
§2.5 + [`spec/v0/04-task-protocol.md`](../../../../spec/v0/04-task-protocol.md)
§7 + [`spec/v0/05-event-protocol.md`](../../../../spec/v0/05-event-protocol.md)
§3.4.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from eden_contracts import DispatchMode
from eden_storage import InvalidPrecondition, ReservedIdentifier, Store


def _seed_admin(store: Store) -> None:
    store.register_worker("admin-eric")


def test_default_dispatch_mode_is_all_auto(
    make_store: Callable[..., Store],
) -> None:
    store = make_store()
    mode = store.read_dispatch_mode()
    assert mode.ideation_creation == "auto"
    assert mode.execution_dispatch == "auto"
    assert mode.evaluation_dispatch == "auto"
    assert mode.integration == "auto"


def test_partial_update_preserves_omitted_keys(
    make_store: Callable[..., Store],
) -> None:
    store = make_store()
    _seed_admin(store)
    result = store.update_dispatch_mode(
        {"evaluation_dispatch": "manual"}, updated_by="admin-eric"
    )
    assert result.evaluation_dispatch == "manual"
    # Unchanged keys retained at "auto".
    assert result.ideation_creation == "auto"
    assert result.execution_dispatch == "auto"
    assert result.integration == "auto"

    # Persistence across read.
    fresh = store.read_dispatch_mode()
    assert fresh.evaluation_dispatch == "manual"
    assert fresh.integration == "auto"


def test_update_emits_event_with_diff(
    make_store: Callable[..., Store],
) -> None:
    store = make_store()
    _seed_admin(store)
    pre = len(store.events())
    store.update_dispatch_mode(
        {"ideation_creation": "manual", "integration": "manual"},
        updated_by="admin-eric",
    )
    new_events = store.events()[pre:]
    assert [e.type for e in new_events] == ["experiment.dispatch_mode_changed"]
    payload = new_events[0].data
    assert payload["updated_by"] == "admin-eric"
    assert payload["changed"] == {
        "ideation_creation": "manual",
        "integration": "manual",
    }
    # Full post-update state, all four keys present.
    assert payload["dispatch_mode"]["ideation_creation"] == "manual"
    assert payload["dispatch_mode"]["execution_dispatch"] == "auto"
    assert payload["dispatch_mode"]["evaluation_dispatch"] == "auto"
    assert payload["dispatch_mode"]["integration"] == "manual"


def test_idempotent_flip_emits_no_event(
    make_store: Callable[..., Store],
) -> None:
    """An update that flips no key value MUST NOT emit an event."""
    store = make_store()
    _seed_admin(store)
    # Default state is all-auto; ask to set everything to auto again.
    pre = len(store.events())
    result = store.update_dispatch_mode(
        {"ideation_creation": "auto", "execution_dispatch": "auto"},
        updated_by="admin-eric",
    )
    assert result.ideation_creation == "auto"
    assert len(store.events()) == pre  # no event emitted


def test_partial_idempotent_emits_only_changed_diff(
    make_store: Callable[..., Store],
) -> None:
    """When part of the update is a no-op, the event records only the actual diff."""
    store = make_store()
    _seed_admin(store)
    store.update_dispatch_mode(
        {"integration": "manual"}, updated_by="admin-eric"
    )
    pre = len(store.events())
    # Re-flip integration to manual (no-op) AND ideation to manual (real diff).
    store.update_dispatch_mode(
        {"integration": "manual", "ideation_creation": "manual"},
        updated_by="admin-eric",
    )
    new_events = store.events()[pre:]
    assert len(new_events) == 1
    payload = new_events[0].data
    assert payload["changed"] == {"ideation_creation": "manual"}


def test_dispatch_mode_accepts_model_input(
    make_store: Callable[..., Store],
) -> None:
    """Either a ``DispatchMode`` model or a dict satisfies ``update_dispatch_mode``."""
    store = make_store()
    _seed_admin(store)
    new_mode = DispatchMode(integration="manual")
    result = store.update_dispatch_mode(new_mode, updated_by="admin-eric")
    assert result.integration == "manual"


def test_dispatch_mode_rejects_invalid_value(
    make_store: Callable[..., Store],
) -> None:
    store = make_store()
    _seed_admin(store)
    with pytest.raises(InvalidPrecondition):
        store.update_dispatch_mode(
            {"ideation_creation": "paused"},
            updated_by="admin-eric",
        )


def test_dispatch_mode_rejects_invalid_actor_id(
    make_store: Callable[..., Store],
) -> None:
    """Actor id must satisfy the §6.1 grammar; reserved id ('admin') rejected."""
    store = make_store()
    with pytest.raises(ReservedIdentifier):
        store.update_dispatch_mode(
            {"integration": "manual"},
            updated_by="admin",
        )
    with pytest.raises(InvalidPrecondition):
        store.update_dispatch_mode(
            {"integration": "manual"},
            updated_by="Admin",  # uppercase violates grammar
        )
