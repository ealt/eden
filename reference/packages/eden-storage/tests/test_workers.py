"""Worker-registry conformance scenarios (issue #128 identity rename).

Drives every backend through the ``register_worker`` / ``read_worker``
/ ``list_workers`` / ``reissue_credential`` / ``verify_worker_credential``
operations. Since the rename, ``register_worker`` MINTS an opaque
``wkr_*`` id and takes an optional display ``name``; there is no
id-based idempotency (every call mints a fresh row + credential).
Reserved values moved to NAME-space. The Store-layer contract is in
[`spec/v0/02-data-model.md`](../../../../spec/v0/02-data-model.md)
§1.6/§1.7 + §6 and
[`spec/v0/08-storage.md`](../../../../spec/v0/08-storage.md) §9.
"""

from __future__ import annotations

import re
from collections.abc import Callable

import pytest
from eden_contracts import Worker
from eden_storage import (
    InvalidName,
    NotFound,
    ReservedIdentifier,
    Store,
)

_WORKER_ID_RE = re.compile(r"^wkr_[0-9a-hjkmnp-tv-z]{26}$")


def test_register_worker_mints_opaque_id_and_token(
    make_store: Callable[..., Store],
) -> None:
    store = make_store(seed_workers=False)
    worker, token = store.register_worker(name="eric")
    assert isinstance(worker, Worker)
    assert _WORKER_ID_RE.fullmatch(worker.worker_id)
    assert worker.name == "eric"
    assert worker.experiment_id == store.experiment_id
    assert token is not None
    # ≥256 bits of entropy = 64 hex chars per `_generate_credential_token`.
    assert len(token) == 64
    assert all(c in "0123456789abcdef" for c in token)


def test_register_worker_without_name_mints_bare_id(
    make_store: Callable[..., Store],
) -> None:
    store = make_store(seed_workers=False)
    worker, token = store.register_worker()
    assert _WORKER_ID_RE.fullmatch(worker.worker_id)
    assert worker.name is None
    assert token is not None


def test_register_worker_always_mints_fresh_row_and_credential(
    make_store: Callable[..., Store],
) -> None:
    """Post-#128 there is no id-based idempotency: a second register with the
    same name mints a distinct worker_id and a distinct credential."""
    store = make_store(seed_workers=False)
    first, first_token = store.register_worker(name="eric")
    second, second_token = store.register_worker(name="eric")
    assert first.worker_id != second.worker_id
    assert first_token is not None
    assert second_token is not None
    assert first_token != second_token
    # Each credential authenticates only its own worker.
    assert store.verify_worker_credential(first.worker_id, first_token) is True
    assert store.verify_worker_credential(second.worker_id, second_token) is True
    assert store.verify_worker_credential(first.worker_id, second_token) is False


def test_register_worker_invalid_name_rejected(
    make_store: Callable[..., Store],
) -> None:
    store = make_store(seed_workers=False)
    for bad in [" eric", "eric ", "  ", "a" * 129, "bad\x00name"]:
        with pytest.raises(InvalidName):
            store.register_worker(name=bad)


def test_register_worker_reserved_name_rejected(
    make_store: Callable[..., Store],
) -> None:
    store = make_store(seed_workers=False)
    for reserved in ["admin", "system", "internal"]:
        with pytest.raises(ReservedIdentifier):
            store.register_worker(name=reserved)


def test_register_worker_persists_labels_and_name(
    make_store: Callable[..., Store],
) -> None:
    store = make_store(seed_workers=False)
    worker, _ = store.register_worker(
        name="agent-claude",
        labels={"role": "executor", "model": "claude-opus-4-7"},
    )
    read = store.read_worker(worker.worker_id)
    assert read.name == "agent-claude"
    assert read.labels == {"role": "executor", "model": "claude-opus-4-7"}


def test_read_worker_not_found(make_store: Callable[..., Store]) -> None:
    store = make_store(seed_workers=False)
    with pytest.raises(NotFound):
        store.read_worker("wkr_00000000000000000000000000")


def test_list_workers_returns_sorted_snapshot(
    make_store: Callable[..., Store],
) -> None:
    store = make_store(seed_workers=False)
    ids = [store.register_worker(name=n)[0].worker_id for n in ["zara", "alice", "bob"]]
    workers = store.list_workers()
    assert [w.worker_id for w in workers] == sorted(ids)


def test_list_workers_name_filter(
    make_store: Callable[..., Store],
) -> None:
    """`list_workers(name=...)` filters to exact, case-sensitive matches."""
    store = make_store(seed_workers=False)
    a1, _ = store.register_worker(name="alice")
    a2, _ = store.register_worker(name="alice")
    store.register_worker(name="bob")
    matches = store.list_workers(name="alice")
    assert {w.worker_id for w in matches} == {a1.worker_id, a2.worker_id}
    assert all(w.name == "alice" for w in matches)
    # Case-sensitive: a different case does not match.
    assert store.list_workers(name="Alice") == []
    # Default returns every worker.
    assert len(store.list_workers()) == 3


def test_list_workers_excludes_credential(
    make_store: Callable[..., Store],
) -> None:
    """The wire-visible Worker shape MUST NOT carry a credential or hash."""
    store = make_store(seed_workers=False)
    store.register_worker(name="eric")
    [worker] = store.list_workers()
    serialized = worker.model_dump(mode="json", exclude_none=True)
    assert "auth_credential_hash" not in serialized
    assert "credential_hash" not in serialized
    assert "registration_token" not in serialized


def test_verify_worker_credential_accepts_current_token(
    make_store: Callable[..., Store],
) -> None:
    store = make_store(seed_workers=False)
    worker, token = store.register_worker(name="eric")
    assert token is not None
    assert store.verify_worker_credential(worker.worker_id, token) is True


def test_verify_worker_credential_rejects_wrong_token(
    make_store: Callable[..., Store],
) -> None:
    store = make_store(seed_workers=False)
    worker, _ = store.register_worker(name="eric")
    assert store.verify_worker_credential(worker.worker_id, "wrong-token") is False


def test_verify_worker_credential_rejects_unknown_worker(
    make_store: Callable[..., Store],
) -> None:
    """Unknown ``worker_id`` returns False rather than raising.

    The binding-layer caller collapses both arms (no such worker /
    wrong secret) into a single 401 without leaking which arm hit.
    """
    store = make_store(seed_workers=False)
    assert (
        store.verify_worker_credential("wkr_00000000000000000000000000", "tok") is False
    )


def test_reissue_credential_invalidates_prior(
    make_store: Callable[..., Store],
) -> None:
    store = make_store(seed_workers=False)
    worker, first_token = store.register_worker(name="eric")
    assert first_token is not None
    second_token = store.reissue_credential(worker.worker_id)
    assert second_token != first_token
    assert store.verify_worker_credential(worker.worker_id, first_token) is False
    assert store.verify_worker_credential(worker.worker_id, second_token) is True


def test_reissue_credential_unknown_worker(
    make_store: Callable[..., Store],
) -> None:
    store = make_store(seed_workers=False)
    with pytest.raises(NotFound):
        store.reissue_credential("wkr_00000000000000000000000000")


# ---------------------------------------------------------------------
# Store.claim §3.5 RBAC enforcement
# ---------------------------------------------------------------------


def test_claim_by_unregistered_worker_rejected(
    make_store: Callable[..., Store],
) -> None:
    """§3.5 step 2 — claim by an unregistered worker raises WorkerNotRegistered."""
    from eden_storage import WorkerNotRegistered

    store = make_store(seed_workers=False)
    store.create_ideation_task("t1")
    with pytest.raises(WorkerNotRegistered):
        store.claim("t1", "wkr_00000000000000000000000000")


def test_claim_by_registered_worker_accepted_when_target_absent(
    make_store: Callable[..., Store],
) -> None:
    """§3.5 — target=null permits any registered worker."""
    store = make_store(seed_workers=False)
    eric, _ = store.register_worker(name="eric")
    store.create_ideation_task("t1")
    claim = store.claim("t1", eric.worker_id)
    assert claim.worker_id == eric.worker_id


def test_claim_with_worker_target_accepts_matching_worker(
    make_store: Callable[..., Store],
) -> None:
    """§3.5 step 3 — target.kind=worker accepts only the named worker."""
    from eden_contracts import IdeationPayload, IdeationTask, TaskTarget

    store = make_store(seed_workers=False)
    eric, _ = store.register_worker(name="eric")
    target = TaskTarget(kind="worker", id=eric.worker_id)
    task = IdeationTask.model_validate(
        {
            "task_id": "t1",
            "kind": "ideation",
            "state": "pending",
            "payload": IdeationPayload(experiment_id=store.experiment_id).model_dump(
                mode="json", exclude_none=True
            ),
            "target": target.model_dump(mode="json", exclude_none=True),
            "created_at": "2026-04-23T00:00:00.000Z",
            "updated_at": "2026-04-23T00:00:00.000Z",
        }
    )
    store.create_task(task)
    claim = store.claim("t1", eric.worker_id)
    assert claim.worker_id == eric.worker_id


def test_claim_with_worker_target_rejects_other_worker(
    make_store: Callable[..., Store],
) -> None:
    """§3.5 step 3 — target.kind=worker raises WorkerNotEligible on mismatch."""
    from eden_contracts import IdeationPayload, IdeationTask, TaskTarget
    from eden_storage import WorkerNotEligible

    store = make_store(seed_workers=False)
    eric, _ = store.register_worker(name="eric")
    alice, _ = store.register_worker(name="alice")
    target = TaskTarget(kind="worker", id=eric.worker_id)
    task = IdeationTask.model_validate(
        {
            "task_id": "t1",
            "kind": "ideation",
            "state": "pending",
            "payload": IdeationPayload(experiment_id=store.experiment_id).model_dump(
                mode="json", exclude_none=True
            ),
            "target": target.model_dump(mode="json", exclude_none=True),
            "created_at": "2026-04-23T00:00:00.000Z",
            "updated_at": "2026-04-23T00:00:00.000Z",
        }
    )
    store.create_task(task)
    with pytest.raises(WorkerNotEligible):
        store.claim("t1", alice.worker_id)


def test_claim_with_group_target_accepts_transitive_member(
    make_store: Callable[..., Store],
) -> None:
    """§3.5 step 3 — target.kind=group accepts any transitive member."""
    from eden_contracts import IdeationPayload, IdeationTask, TaskTarget

    store = make_store(seed_workers=False)
    eric, _ = store.register_worker(name="eric")
    alice, _ = store.register_worker(name="alice")
    team_a = store.register_group(
        name="team-a", members=[eric.worker_id, alice.worker_id]
    )
    humans = store.register_group(name="humans", members=[team_a.group_id])
    target = TaskTarget(kind="group", id=humans.group_id)
    task = IdeationTask.model_validate(
        {
            "task_id": "t1",
            "kind": "ideation",
            "state": "pending",
            "payload": IdeationPayload(experiment_id=store.experiment_id).model_dump(
                mode="json", exclude_none=True
            ),
            "target": target.model_dump(mode="json", exclude_none=True),
            "created_at": "2026-04-23T00:00:00.000Z",
            "updated_at": "2026-04-23T00:00:00.000Z",
        }
    )
    store.create_task(task)
    claim = store.claim("t1", alice.worker_id)
    assert claim.worker_id == alice.worker_id


def test_claim_with_group_target_rejects_non_member(
    make_store: Callable[..., Store],
) -> None:
    """§3.5 step 3 — target.kind=group rejects non-members."""
    from eden_contracts import IdeationPayload, IdeationTask, TaskTarget
    from eden_storage import WorkerNotEligible

    store = make_store(seed_workers=False)
    eric, _ = store.register_worker(name="eric")
    claude, _ = store.register_worker(name="claude")
    humans = store.register_group(name="humans", members=[eric.worker_id])
    target = TaskTarget(kind="group", id=humans.group_id)
    task = IdeationTask.model_validate(
        {
            "task_id": "t1",
            "kind": "ideation",
            "state": "pending",
            "payload": IdeationPayload(experiment_id=store.experiment_id).model_dump(
                mode="json", exclude_none=True
            ),
            "target": target.model_dump(mode="json", exclude_none=True),
            "created_at": "2026-04-23T00:00:00.000Z",
            "updated_at": "2026-04-23T00:00:00.000Z",
        }
    )
    store.create_task(task)
    with pytest.raises(WorkerNotEligible):
        store.claim("t1", claude.worker_id)


def test_submit_by_deregistered_worker_rejected(
    make_store: Callable[..., Store],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§3.5 step 2 extended to submit — a worker_id no longer registered
    MUST NOT be able to submit even when it still holds a live claim.

    Spec citation: chapter 04 §3.5 step 2 + §4.1. The §3.5 ladder runs
    atomically with the claim write; the same registration check is
    extended into the submit transition so a worker whose registry
    row disappears between claim and submit cannot finish a stranded
    claim. The reference impl doesn't expose ``delete_worker`` on the
    public Store today, so this test simulates the post-claim
    deregistration by monkey-patching ``_get_worker`` to return
    ``None`` for the claimant during the submit call.
    """
    from eden_storage import IdeaSubmission, WorkerNotRegistered

    store = make_store(seed_workers=False)
    eric, _ = store.register_worker(name="eric")
    store.create_ideation_task("t1")
    store.claim("t1", eric.worker_id)

    original_get_worker = store._get_worker  # type: ignore[attr-defined]

    def _missing_eric(worker_id: str):
        if worker_id == eric.worker_id:
            return None
        return original_get_worker(worker_id)

    monkeypatch.setattr(store, "_get_worker", _missing_eric)

    with pytest.raises(WorkerNotRegistered):
        store.submit("t1", eric.worker_id, IdeaSubmission(status="success", idea_ids=()))
