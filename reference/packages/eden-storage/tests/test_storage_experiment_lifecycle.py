"""``terminate_experiment`` + experiment-lifecycle guards (12a-3 wave 2).

Spec: [`spec/v0/02-data-model.md`](../../../../spec/v0/02-data-model.md)
§2.5 + [`spec/v0/04-task-protocol.md`](../../../../spec/v0/04-task-protocol.md)
§2 / §3.5 step 0 / §8 + [`spec/v0/05-event-protocol.md`](../../../../spec/v0/05-event-protocol.md)
§3.4.

Parametrized across the three reference backends (``memory`` /
``sqlite`` / ``postgres``) via the ``make_store`` fixture in
[`conftest.py`](conftest.py). The postgres rows skip when
``EDEN_TEST_POSTGRES_DSN`` is unset.
"""
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

from collections.abc import Callable

import pytest
from eden_contracts import EvaluationSchema, Idea, TaskTarget, Variant
from eden_storage import (
    EvaluationSubmission,
    IllegalTransition,
    InvalidPrecondition,
    Store,
    VariantSubmission,
    WorkerNotEligible,
)

_DT = "2026-05-16T00:00:00Z"
_SHA1 = "a" * 40


def _seed_admin(store: Store) -> None:
    """No-op since the identity rename (#128).

    Actor fields (``terminated_by`` etc.) are ActorId
    (``admin`` | ``wkr_*``) and are trusted by the store as data — no
    registered worker row is required. Tests use the literal ``admin``
    bearer principal directly. Retained as a no-op so the per-test call
    sites read unchanged.
    """


def _seed_idea(store: Store, idea_id: str, *, ready: bool = True) -> Idea:
    """Create a drafting idea; optionally mark it ready for dispatch."""
    store.create_idea(
        Idea(
            idea_id=idea_id,
            experiment_id=store.experiment_id,
            slug="s",
            priority=0.0,
            parent_commits=[_SHA1],
            artifacts_uri="s3://b/",
            state="drafting",
            created_at=_DT,
        )
    )
    if ready:
        store.mark_idea_ready(idea_id)
    return store.read_idea(idea_id)


def test_fresh_experiment_is_running(
    make_store: Callable[..., Store],
) -> None:
    """A freshly-initialized experiment starts in the running state."""
    store = make_store()
    assert store.read_experiment_state() == "running"
    exp = store.read_experiment()
    assert exp.experiment_id == store.experiment_id
    assert exp.state == "running"
    # `created_at` is stamped at insertion time; format-only check.
    assert exp.created_at.endswith("Z")


def test_terminate_experiment_transitions_atomically(
    make_store: Callable[..., Store],
) -> None:
    """``terminate_experiment`` updates state + emits event in one commit."""
    store = make_store()
    _seed_admin(store)
    pre_events = len(store.events())
    exp = store.terminate_experiment(
        reason="max_variants policy reached", terminated_by="admin"
    )
    assert exp.state == "terminated"
    assert store.read_experiment_state() == "terminated"
    new_events = store.events()[pre_events:]
    assert [e.type for e in new_events] == ["experiment.terminated"]
    payload = new_events[0].data
    assert payload["reason"] == "max_variants policy reached"
    assert payload["terminated_by"] == "admin"


def test_terminate_is_idempotent_on_terminated_state(
    make_store: Callable[..., Store],
) -> None:
    """A second ``terminate_experiment`` returns success without a new event."""
    store = make_store()
    other_actor, _ = store.register_worker(name="admin-different")
    store.terminate_experiment(reason="first", terminated_by="admin")
    pre_events = len(store.events())
    # Different caller + different reason: idempotency wins; the first
    # call's reason stays recorded.
    result = store.terminate_experiment(
        reason="different reason", terminated_by=other_actor.worker_id
    )
    assert result.state == "terminated"
    assert store.events()[pre_events:] == []
    # The first call's reason is preserved on the recorded event.
    term_events = [
        e for e in store.events() if e.type == "experiment.terminated"
    ]
    assert len(term_events) == 1
    assert term_events[0].data["reason"] == "first"
    assert term_events[0].data["terminated_by"] == "admin"


def test_terminate_rejects_reserved_actor_id(
    make_store: Callable[..., Store],
) -> None:
    """``terminated_by`` MUST satisfy the ActorId (``admin`` | ``wkr_*``) grammar."""
    store = make_store()
    with pytest.raises(InvalidPrecondition):
        store.terminate_experiment(reason="x", terminated_by="Admin")


def test_create_ideation_task_rejected_after_termination(
    make_store: Callable[..., Store],
) -> None:
    """The terminated-experiment guard rejects new ideation tasks."""
    store = make_store()
    _seed_admin(store)
    store.terminate_experiment(reason="done", terminated_by="admin")
    with pytest.raises(IllegalTransition):
        store.create_ideation_task("plan-1")


def test_create_execution_task_rejected_after_termination(
    make_store: Callable[..., Store],
) -> None:
    """Same guard applies to execution tasks (idea must exist as `ready` first)."""
    store = make_store()
    _seed_admin(store)
    _seed_idea(store, "idea-1", ready=True)
    store.terminate_experiment(reason="done", terminated_by="admin")
    with pytest.raises(IllegalTransition):
        store.create_execution_task("exec-1", "idea-1")


def test_create_evaluation_task_rejected_after_termination(
    make_store: Callable[..., Store],
) -> None:
    """Same guard applies to evaluation tasks (variant must be starting + commit_sha)."""
    store = make_store()
    _seed_admin(store)
    store.create_variant(
        Variant(
            variant_id="variant-1",
            experiment_id=store.experiment_id,
            idea_id="idea-x",
            status="starting",
            parent_commits=[_SHA1],
            branch="work/v1",
            commit_sha="b" * 40,
            started_at=_DT,
        )
    )
    store.terminate_experiment(reason="done", terminated_by="admin")
    with pytest.raises(IllegalTransition):
        store.create_evaluation_task("eval-1", "variant-1")


def test_claim_rejected_after_termination(
    make_store: Callable[..., Store],
) -> None:
    """Pending tasks at termination time are unreachable: claim → IllegalTransition.

    Per [`spec/v0/04-task-protocol.md`](../../../../spec/v0/04-task-protocol.md)
    §3.5 step 0: the guard runs before the §3.4 state precondition, so
    a pending task in a terminated experiment surfaces the
    terminated-experiment error (not "not pending").
    """
    store = make_store()
    _seed_admin(store)
    store.create_ideation_task("plan-1")
    store.terminate_experiment(reason="done", terminated_by="admin")
    with pytest.raises(IllegalTransition):
        store.claim("plan-1", store.seeded_workers["ideator-1"])


def test_already_claimed_tasks_complete_after_termination(
    make_store: Callable[..., Store],
) -> None:
    """Drain semantics: in-progress work is not stranded by termination.

    Per [`spec/v0/04-task-protocol.md`](../../../../spec/v0/04-task-protocol.md)
    §3.5: ``submit`` on an already-claimed task remains legal after
    termination — termination stops new work, not committed work.
    """
    from eden_storage.submissions import IdeaSubmission

    store = make_store()
    _seed_admin(store)
    store.create_ideation_task("plan-1")
    store.claim("plan-1", store.seeded_workers["ideator-1"])
    store.terminate_experiment(reason="done", terminated_by="admin")
    # Submit should still succeed; the claim was committed before
    # the lifecycle transition.
    store.submit(
        "plan-1",
        store.seeded_workers["ideator-1"],
        IdeaSubmission(status="success", idea_ids=()),
    )
    task = store.read_task("plan-1")
    assert task.state == "submitted"


def test_integrate_variant_continues_after_termination(
    make_store: Callable[..., Store],
) -> None:
    """Integration drain: success variants get integrated even after termination."""
    store = make_store(
        evaluation_schema=EvaluationSchema.model_validate({"score": "real"})
    )
    _seed_admin(store)
    # Drive a variant to status=success through the normal executor +
    # evaluator flow so `integrate_variant` has a legal target.
    _seed_idea(store, "idea-1", ready=True)
    store.create_execution_task("t-exec", "idea-1")
    store.claim("t-exec", store.seeded_workers["executor-w"])
    store.create_variant(
        Variant(
            variant_id="variant-1",
            experiment_id=store.experiment_id,
            idea_id="idea-1",
            status="starting",
            parent_commits=[_SHA1],
            branch="work/v1",
            started_at=_DT,
        )
    )
    store.submit(
        "t-exec",
        store.seeded_workers["executor-w"],
        VariantSubmission(
            status="success", variant_id="variant-1", commit_sha="b" * 40
        ),
    )
    store.accept("t-exec")
    store.create_evaluation_task("t-eval", "variant-1")
    store.claim("t-eval", store.seeded_workers["evaluator-w"])
    store.submit(
        "t-eval",
        store.seeded_workers["evaluator-w"],
        EvaluationSubmission(
            status="success", variant_id="variant-1", evaluation={"score": 0.9}
        ),
    )
    store.accept("t-eval")
    assert store.read_variant("variant-1").status == "success"

    store.terminate_experiment(reason="done", terminated_by="admin")
    # Integrator continues to drain per the §2.5 contract.
    store.integrate_variant("variant-1", "c" * 40)
    variant = store.read_variant("variant-1")
    assert variant.variant_commit_sha == "c" * 40
    # And the `variant.integrated` event fires after `experiment.terminated`
    # — both legitimate orderings per `03-roles.md` §6.4.1.
    type_seq = [e.type for e in store.events()]
    assert type_seq.index("experiment.terminated") < type_seq.index(
        "variant.integrated"
    )


def test_update_experiment_state_only_allows_running_to_terminated(
    make_store: Callable[..., Store],
) -> None:
    """v0 defines exactly one legal transition."""
    store = make_store()
    # No legal "running → running" mutation either (same-state is no-op).
    same = store.update_experiment_state("running")
    assert same.state == "running"
    # Bad value rejected up front.
    with pytest.raises(InvalidPrecondition):
        store.update_experiment_state("paused")  # type: ignore[arg-type]
    store.update_experiment_state("terminated")
    # "terminated → running" is not a v0 transition.
    with pytest.raises(IllegalTransition):
        store.update_experiment_state("running")


def test_create_execution_task_inherits_idea_intended_executor(
    make_store: Callable[..., Store],
) -> None:
    """The auto-orchestrator path: idea.intended_executor flows to task.target."""
    store = make_store()
    executor_a, _ = store.register_worker(name="executor-a")
    # Build an idea that names a specific intended executor (worker).
    store.create_idea(
        Idea(
            idea_id="idea-1",
            experiment_id=store.experiment_id,
            slug="s",
            priority=0.0,
            parent_commits=[_SHA1],
            artifacts_uri="s3://b/",
            state="drafting",
            created_at=_DT,
            intended_executor=TaskTarget(kind="worker", id=executor_a.worker_id),
        )
    )
    store.mark_idea_ready("idea-1")
    task = store.create_execution_task("exec-1", "idea-1")
    assert task.target is not None
    assert task.target.kind == "worker"
    assert task.target.id == executor_a.worker_id


def test_create_execution_task_explicit_target_overrides_intended_executor(
    make_store: Callable[..., Store],
) -> None:
    """Admin override: explicit target wins over idea.intended_executor."""
    store = make_store()
    executor_a, _ = store.register_worker(name="executor-a")
    executor_b, _ = store.register_worker(name="executor-b")
    store.create_idea(
        Idea(
            idea_id="idea-1",
            experiment_id=store.experiment_id,
            slug="s",
            priority=0.0,
            parent_commits=[_SHA1],
            artifacts_uri="s3://b/",
            state="drafting",
            created_at=_DT,
            intended_executor=TaskTarget(kind="worker", id=executor_a.worker_id),
        )
    )
    store.mark_idea_ready("idea-1")
    task = store.create_execution_task(
        "exec-1",
        "idea-1",
        target=TaskTarget(kind="worker", id=executor_b.worker_id),
    )
    assert task.target is not None
    assert task.target.id == executor_b.worker_id


def test_create_execution_task_no_target_when_idea_has_no_hint(
    make_store: Callable[..., Store],
) -> None:
    """Default: an idea without intended_executor produces an open task."""
    store = make_store()
    store.create_idea(
        Idea(
            idea_id="idea-1",
            experiment_id=store.experiment_id,
            slug="s",
            priority=0.0,
            parent_commits=[_SHA1],
            artifacts_uri="s3://b/",
            state="drafting",
            created_at=_DT,
        )
    )
    store.mark_idea_ready("idea-1")
    task = store.create_execution_task("exec-1", "idea-1")
    assert task.target is None


def test_intended_executor_resolves_at_claim_time(
    make_store: Callable[..., Store],
) -> None:
    """Routing hint resolution is claim-time, matching `Task.target` semantics.

    The auto-dispatched task targets a specific worker; another worker
    cannot claim, even though both are registered.
    """
    store = make_store()
    executor_a, _ = store.register_worker(name="executor-a")
    executor_b, _ = store.register_worker(name="executor-b")
    store.create_idea(
        Idea(
            idea_id="idea-1",
            experiment_id=store.experiment_id,
            slug="s",
            priority=0.0,
            parent_commits=[_SHA1],
            artifacts_uri="s3://b/",
            state="drafting",
            created_at=_DT,
            intended_executor=TaskTarget(kind="worker", id=executor_a.worker_id),
        )
    )
    store.mark_idea_ready("idea-1")
    store.create_execution_task("exec-1", "idea-1")
    with pytest.raises(WorkerNotEligible):
        store.claim("exec-1", executor_b.worker_id)
    # The intended executor's claim succeeds.
    store.claim("exec-1", executor_a.worker_id)
