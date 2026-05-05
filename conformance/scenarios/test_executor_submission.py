"""Executor submission semantics — chapter 03 §3.4."""

from __future__ import annotations

import pytest
from conformance.harness import _seed
from conformance.harness.event_cursor import EventLog
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = 'Executor submission'


def test_submit_with_unknown_variant_rejected(wire_client: WireClient) -> None:
    """spec/v0/03-roles.md §3.4 — submission's variant_id MUST refer to the variant it created.

    The §3.4 wire shape requires `variant_id` to identify the variant the
    executor persisted under §3.2 step 1. A submission whose
    `variant_id` does not resolve violates the role contract.
    """
    pid = _seed.create_idea(wire_client)
    _seed.mark_idea_ready(wire_client, pid)
    impl_tid = _seed.create_execute_task(wire_client, idea_id=pid)
    c = _seed.claim(wire_client, impl_tid)
    r = _seed.submit_execution(
        wire_client,
        impl_tid,
        token=c["token"],
        variant_id="does-not-exist",
        commit_sha="a" * 40,
    )
    assert r.status_code == 409, r.text
    assert r.json().get("type") == "eden://error/illegal-transition"


def test_submit_with_wrong_idea_variant_rejected(wire_client: WireClient) -> None:
    """spec/v0/03-roles.md §3.4 — the submitted variant MUST belong to the task's idea.

    The executor's submission is scoped to the idea it received
    in the task payload (§3.1). A submission whose variant belongs to a
    different idea violates the role-contract scope and the task
    store rejects rather than silently coupling unrelated objects.
    """
    pid_a = _seed.create_idea(wire_client, slug="a")
    pid_b = _seed.create_idea(wire_client, slug="b")
    _seed.mark_idea_ready(wire_client, pid_a)
    impl_tid = _seed.create_execute_task(wire_client, idea_id=pid_a)
    # Variant belongs to idea_b, not the task's idea_a.
    foreign_variant = _seed.create_variant(
        wire_client, idea_id=pid_b, status="starting"
    )
    c = _seed.claim(wire_client, impl_tid)
    r = _seed.submit_execution(
        wire_client,
        impl_tid,
        token=c["token"],
        variant_id=foreign_variant,
        commit_sha="a" * 40,
    )
    assert r.status_code == 409, r.text
    assert r.json().get("type") == "eden://error/illegal-transition"


def test_success_without_commit_sha_must_not_complete_variant(
    wire_client: WireClient,
) -> None:
    """spec/v0/03-roles.md §3.4 — commit_sha MUST be present when status is success.

    "commit_sha — required when status == success." A conforming IUT
    MUST reject this somewhere on the path from /submit to /accept;
    the variant's terminal status MUST NOT become `success` from a
    submission that omitted the required field. Where in the pipeline
    the rejection surfaces is implementation-defined (§9 latitude),
    so the assertion checks the observable end-state rather than the
    exact failing endpoint.
    """
    pid = _seed.create_idea(wire_client)
    _seed.mark_idea_ready(wire_client, pid)
    impl_tid = _seed.create_execute_task(wire_client, idea_id=pid)
    variant_id = _seed.create_variant(wire_client, idea_id=pid, status="starting")
    c = _seed.claim(wire_client, impl_tid)
    # Wire-level submit without commit_sha. submit_execution always
    # includes commit_sha on success, so build the request inline.
    r = wire_client.post(
        wire_client.tasks_path(impl_tid, "/submit"),
        json={
            "token": c["token"],
            "payload": {
                "kind": "execute",
                "status": "success",
                "variant_id": variant_id,
            },
        },
    )
    if 400 <= r.status_code < 500:
        # IUT rejected at submit with a 4xx — conforming. Per the
        # plan's end-state-not-endpoint pattern, also confirm the
        # variant didn't somehow terminalize as success.
        variant = _seed.read_variant(wire_client, variant_id)
        assert variant["status"] != "success"
        return
    assert r.status_code == 200, (
        f"implement success without commit_sha returned {r.status_code}; "
        "spec/v0/03-roles.md §3.4 latitude is submit-time-reject (4xx) or "
        "accept-time-reject; 5xx is a server bug, not conforming behavior"
    )
    # IUT accepted at submit (commit_sha is optional in the wire
    # shape). The orchestrator-side accept call MUST then reject and
    # the variant MUST NOT terminalize as success.
    accept = _seed.accept(wire_client, impl_tid)
    if 200 <= accept.status_code < 300:
        # Accept somehow succeeded — the variant MUST NOT show success.
        variant = _seed.read_variant(wire_client, variant_id)
        assert variant["status"] != "success", (
            "implement success without commit_sha must not produce a success variant"
        )
        return
    assert 400 <= accept.status_code < 500, (
        f"implement /accept returned {accept.status_code}; expected 4xx "
        "rejection or 2xx acceptance — 5xx is a server bug"
    )
    # Accept rejected with 4xx — that's the validation_error path;
    # the variant stays in starting until the orchestrator rejects the
    # task with validation_error or reclaims it.
    variant = _seed.read_variant(wire_client, variant_id)
    assert variant["status"] != "success"


def test_success_with_commit_sha_writes_variant_commit_sha(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/03-roles.md §3.4 — accepted success submission MUST land commit_sha on the variant.

    "On successful completion, set the variant's `commit_sha` to the
    tip of the `work/*` branch" (§3.2 step 3) is realized at task
    accept time: the orchestrator atomically writes the submission's
    `commit_sha` to the variant as part of the §05 §2.2 composite
    commit.
    """
    sha = "abc" + "0" * 37
    pid = _seed.create_idea(wire_client)
    _seed.mark_idea_ready(wire_client, pid)
    impl_tid = _seed.create_execute_task(wire_client, idea_id=pid)
    variant_id = _seed.create_variant(wire_client, idea_id=pid, status="starting")
    c = _seed.claim(wire_client, impl_tid)
    r = _seed.submit_execution(
        wire_client,
        impl_tid,
        token=c["token"],
        variant_id=variant_id,
        commit_sha=sha,
    )
    assert r.status_code == 200, r.text
    accept = _seed.accept(wire_client, impl_tid)
    assert 200 <= accept.status_code < 300, accept.text
    variant = _seed.read_variant(wire_client, variant_id)
    assert variant["commit_sha"] == sha


def test_resubmit_same_commit_sha_is_idempotent(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/03-roles.md §3.4 — duplicate submit with same variant_id+commit_sha MUST be accepted.

    The §3.4 idempotency rule says "a duplicate submit presenting
    the same `variant_id` and `commit_sha` MUST be accepted without
    side effect." The duplicate response MUST NOT emit a second
    `task.submitted` event.
    """
    pid = _seed.create_idea(wire_client)
    _seed.mark_idea_ready(wire_client, pid)
    impl_tid = _seed.create_execute_task(wire_client, idea_id=pid)
    variant_id = _seed.create_variant(wire_client, idea_id=pid, status="starting")
    c = _seed.claim(wire_client, impl_tid)
    sha = "d" * 40
    r1 = _seed.submit_execution(
        wire_client, impl_tid, token=c["token"], variant_id=variant_id, commit_sha=sha
    )
    assert r1.status_code == 200, r1.text
    r2 = _seed.submit_execution(
        wire_client, impl_tid, token=c["token"], variant_id=variant_id, commit_sha=sha
    )
    assert r2.status_code == 200, r2.text
    submitted = [
        e
        for e in event_log.find_by_type(event_log.replay_all(), "task.submitted")
        if e["data"].get("task_id") == impl_tid
    ]
    assert len(submitted) == 1


def test_status_error_terminalizes_variant_and_blocks_evaluate_dispatch(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/03-roles.md §3.4 — status=error errors variant + blocks dispatch.

    "`error` — the executor could not realize the idea. The
    variant MUST be persisted with `status == 'error'`. No evaluate
    task is dispatched against an errored variant." Both halves
    asserted: (a) after the orchestrator reject path runs, the
    variant's `status` is `"error"`; (b) no `task.created` event with
    `kind == "evaluate"` references this variant.
    """
    pid = _seed.create_idea(wire_client)
    _seed.mark_idea_ready(wire_client, pid)
    impl_tid = _seed.create_execute_task(wire_client, idea_id=pid)
    variant_id = _seed.create_variant(wire_client, idea_id=pid, status="starting")
    c = _seed.claim(wire_client, impl_tid)
    r = _seed.submit_execution(
        wire_client,
        impl_tid,
        token=c["token"],
        variant_id=variant_id,
        status="error",
    )
    assert r.status_code == 200, r.text
    rejected = _seed.reject(wire_client, impl_tid, reason="worker_error")
    assert 200 <= rejected.status_code < 300, rejected.text
    variant = _seed.read_variant(wire_client, variant_id)
    assert variant["status"] == "error"
    events = event_log.replay_all()
    failed = [
        e
        for e in event_log.find_by_type(events, "task.failed")
        if e["data"].get("task_id") == impl_tid
    ]
    assert len(failed) == 1, (
        f"expected exactly one task.failed for impl task {impl_tid!r}; got {failed}"
    )
    for e in event_log.find_by_type(events, "task.created"):
        if e["data"].get("kind") != "evaluate":
            continue
        eval_tid = e["data"]["task_id"]
        eval_task = _seed.read_task(wire_client, eval_tid)
        assert eval_task["payload"].get("variant_id") != variant_id, (
            f"evaluate task {eval_tid!r} dispatched against errored variant "
            f"{variant_id!r}; spec/v0/03-roles.md §3.4 forbids this"
        )


def test_resubmit_divergent_commit_sha_rejected(wire_client: WireClient) -> None:
    """spec/v0/03-roles.md §3.4 — duplicate submit disagreeing on commit_sha MUST be rejected.

    "A duplicate submit that disagrees with the already-recorded
    result MUST be rejected" — executor §3.4 cites chapter 04
    §4.2 directly. The wire-level error type is
    `eden://error/conflicting-resubmission`.
    """
    pid = _seed.create_idea(wire_client)
    _seed.mark_idea_ready(wire_client, pid)
    impl_tid = _seed.create_execute_task(wire_client, idea_id=pid)
    variant_id = _seed.create_variant(wire_client, idea_id=pid, status="starting")
    c = _seed.claim(wire_client, impl_tid)
    r1 = _seed.submit_execution(
        wire_client,
        impl_tid,
        token=c["token"],
        variant_id=variant_id,
        commit_sha="1" * 40,
    )
    assert r1.status_code == 200, r1.text
    r2 = _seed.submit_execution(
        wire_client,
        impl_tid,
        token=c["token"],
        variant_id=variant_id,
        commit_sha="2" * 40,
    )
    assert r2.status_code == 409, r2.text
    assert r2.json().get("type") == "eden://error/conflicting-resubmission"
