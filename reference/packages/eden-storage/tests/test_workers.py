"""Worker-registry conformance scenarios (12a-1 wave 2).

Drives every backend through the new ``register_worker`` /
``read_worker`` / ``list_workers`` / ``reissue_credential`` /
``verify_worker_credential`` operations. The Store-layer contract
those ops implement is in [`spec/v0/02-data-model.md`](
../../../../spec/v0/02-data-model.md) §6 + §6.3 (idempotent
re-registration; credential rotation as a separate explicit op) and
[`spec/v0/08-storage.md`](../../../../spec/v0/08-storage.md) §9.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from eden_contracts import Worker
from eden_storage import (
    InvalidPrecondition,
    NotFound,
    ReservedIdentifier,
    Store,
)


def test_register_worker_returns_token_first_time(
    make_store: Callable[..., Store],
) -> None:
    store = make_store()
    worker, token = store.register_worker("eric")
    assert isinstance(worker, Worker)
    assert worker.worker_id == "eric"
    assert worker.experiment_id == store.experiment_id
    assert token is not None
    # ≥256 bits of entropy = 64 hex chars per `_generate_credential_token`.
    assert len(token) == 64
    assert all(c in "0123456789abcdef" for c in token)


def test_register_worker_idempotent_returns_no_new_token(
    make_store: Callable[..., Store],
) -> None:
    store = make_store()
    first, first_token = store.register_worker("eric")
    second, second_token = store.register_worker("eric")
    # The wire-visible record is unchanged.
    assert second.worker_id == first.worker_id
    assert second.registered_at == first.registered_at
    # Idempotent re-register MUST NOT mint a new token; the original
    # credential remains valid.
    assert second_token is None
    assert first_token is not None
    assert store.verify_worker_credential("eric", first_token) is True


def test_register_worker_grammar_rejected(
    make_store: Callable[..., Store],
) -> None:
    store = make_store()
    for bad in ["Eric", "-eric", "_eric", "eric:secret", "eric/agent", "a" * 65]:
        with pytest.raises(InvalidPrecondition):
            store.register_worker(bad)


def test_register_worker_reserved_rejected(
    make_store: Callable[..., Store],
) -> None:
    store = make_store()
    for reserved in ["admin", "system", "internal"]:
        with pytest.raises(ReservedIdentifier):
            store.register_worker(reserved)


def test_register_worker_persists_labels(
    make_store: Callable[..., Store],
) -> None:
    store = make_store()
    store.register_worker(
        "agent-claude", labels={"role": "executor", "model": "claude-opus-4-7"}
    )
    worker = store.read_worker("agent-claude")
    assert worker.labels == {"role": "executor", "model": "claude-opus-4-7"}


def test_read_worker_not_found(make_store: Callable[..., Store]) -> None:
    store = make_store()
    with pytest.raises(NotFound):
        store.read_worker("eric")


def test_list_workers_returns_sorted_snapshot(
    make_store: Callable[..., Store],
) -> None:
    store = make_store()
    for wid in ["zara", "alice", "bob"]:
        store.register_worker(wid)
    workers = store.list_workers()
    assert [w.worker_id for w in workers] == ["alice", "bob", "zara"]


def test_list_workers_excludes_credential(
    make_store: Callable[..., Store],
) -> None:
    """The wire-visible Worker shape MUST NOT carry a credential or hash."""
    store = make_store()
    store.register_worker("eric")
    workers = store.list_workers()
    [worker] = workers
    serialized = worker.model_dump(mode="json", exclude_none=True)
    assert "auth_credential_hash" not in serialized
    assert "credential_hash" not in serialized
    assert "registration_token" not in serialized


def test_verify_worker_credential_accepts_current_token(
    make_store: Callable[..., Store],
) -> None:
    store = make_store()
    _, token = store.register_worker("eric")
    assert token is not None
    assert store.verify_worker_credential("eric", token) is True


def test_verify_worker_credential_rejects_wrong_token(
    make_store: Callable[..., Store],
) -> None:
    store = make_store()
    store.register_worker("eric")
    assert store.verify_worker_credential("eric", "wrong-token") is False


def test_verify_worker_credential_rejects_unknown_worker(
    make_store: Callable[..., Store],
) -> None:
    """Unknown ``worker_id`` returns False rather than raising.

    The binding-layer caller collapses both arms (no such worker /
    wrong secret) into a single 401 without leaking which arm hit.
    """
    store = make_store()
    assert store.verify_worker_credential("ghost", "tok") is False


def test_reissue_credential_invalidates_prior(
    make_store: Callable[..., Store],
) -> None:
    store = make_store()
    _, first_token = store.register_worker("eric")
    assert first_token is not None
    second_token = store.reissue_credential("eric")
    assert second_token != first_token
    assert store.verify_worker_credential("eric", first_token) is False
    assert store.verify_worker_credential("eric", second_token) is True


def test_reissue_credential_unknown_worker(
    make_store: Callable[..., Store],
) -> None:
    store = make_store()
    with pytest.raises(NotFound):
        store.reissue_credential("ghost")
