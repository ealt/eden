"""Unit tests for the lineage helper module (phase 12a-1c, wave 1).

The lineage helpers are plain Python — no FastAPI involved. Each
test seeds a fixture ``InMemoryStore`` directly and asserts the
view models the route layer will hand to the templates.
"""

from __future__ import annotations

from typing import Any

import pytest
from eden_contracts import Idea, Variant
from eden_storage import (
    EvaluationSubmission,
    IdeaSubmission,
    InMemoryStore,
    VariantSubmission,
)
from eden_storage.errors import NotFound as StorageNotFound
from eden_web_ui.routes._lineage import (
    EvaluationTaskLineage,
    ExecutionTaskLineage,
    IdeaLineage,
    IdeationTaskLineage,
    LineageLink,
    VariantLineage,
    lineage_for_evaluation_task,
    lineage_for_execution_task,
    lineage_for_idea,
    lineage_for_ideation_task,
    lineage_for_variant,
)

EXPERIMENT_ID = "exp-lineage"
BASE_SHA = "a" * 40


def _store() -> InMemoryStore:
    store = InMemoryStore(experiment_id=EXPERIMENT_ID, evaluation_schema={})
    for wid in ("ideator-w", "executor-w", "evaluator-w", "executor-other"):
        store.register_worker(wid)
    return store


def _make_idea(
    store: InMemoryStore, *, slug: str = "demo", state: str = "ready"
) -> str:
    idea_id = f"idea-{slug}"
    store.create_idea(
        Idea(
            idea_id=idea_id,
            experiment_id=store.experiment_id,
            slug=slug,
            priority=1.0,
            parent_commits=[BASE_SHA],
            artifacts_uri="https://example.invalid/x.md",
            state="drafting",
            created_at="2026-04-24T11:00:00Z",
        )
    )
    if state != "drafting":
        store.mark_idea_ready(idea_id)
    return idea_id


def _seed_ideation_submission(
    store: InMemoryStore,
    *,
    task_id: str = "plan-1",
    slugs: tuple[str, ...] = ("alpha",),
    status: str = "success",
) -> tuple[str, tuple[str, ...]]:
    """Drive an ideation task to ``submitted`` with the given idea slugs."""
    store.create_ideation_task(task_id)
    claim = store.claim(task_id, "ideator-w")
    idea_ids: list[str] = []
    if status == "success":
        for slug in slugs:
            idea_id = _make_idea(store, slug=slug)
            idea_ids.append(idea_id)
        store.submit(
            task_id,
            claim.worker_id,
            IdeaSubmission(status="success", idea_ids=tuple(idea_ids)),
        )
    else:
        store.submit(
            task_id, claim.worker_id, IdeaSubmission(status="error")
        )
    return task_id, tuple(idea_ids)


def _seed_execution_to_starting(
    store: InMemoryStore,
    *,
    idea_id: str,
    variant_id: str = "v1",
    task_id: str = "exec-1",
    branch: str = "work/v1",
    worker: str = "executor-w",
) -> tuple[str, str]:
    """Seed an execution task → claim → create_variant(starting)."""
    store.create_execution_task(task_id, idea_id)
    store.claim(task_id, worker)
    store.create_variant(
        Variant(
            variant_id=variant_id,
            experiment_id=store.experiment_id,
            idea_id=idea_id,
            status="starting",
            parent_commits=[BASE_SHA],
            branch=branch,
            started_at="2026-04-24T12:00:00Z",
        )
    )
    return task_id, variant_id


def _seed_execution_completed(
    store: InMemoryStore,
    *,
    idea_id: str,
    variant_id: str = "v1",
    task_id: str = "exec-1",
    branch: str = "work/v1",
    commit_sha: str = "b" * 40,
    worker: str = "executor-w",
) -> tuple[str, str]:
    """Seed an execution task all the way through submit+accept."""
    store.create_execution_task(task_id, idea_id)
    claim = store.claim(task_id, worker)
    store.create_variant(
        Variant(
            variant_id=variant_id,
            experiment_id=store.experiment_id,
            idea_id=idea_id,
            status="starting",
            parent_commits=[BASE_SHA],
            branch=branch,
            started_at="2026-04-24T12:00:00Z",
        )
    )
    store.submit(
        task_id,
        claim.worker_id,
        VariantSubmission(
            status="success", variant_id=variant_id, commit_sha=commit_sha
        ),
    )
    store.accept(task_id)
    return task_id, variant_id


# ---------------------------------------------------------------------
# IdeationTaskLineage
# ---------------------------------------------------------------------


def test_ideation_lineage_pending_task_yields_empty() -> None:
    store = _store()
    store.create_ideation_task("plan-pending")
    task = store.read_task("plan-pending")

    result = lineage_for_ideation_task(store, task)  # type: ignore[arg-type]

    assert isinstance(result, IdeationTaskLineage)
    assert result.ideas == ()
    assert result.ideas_total == 0
    assert result.transport_errors == 0


def test_ideation_lineage_lists_ideas_in_submission_order() -> None:
    store = _store()
    task_id, idea_ids = _seed_ideation_submission(
        store, slugs=("alpha", "beta")
    )
    task = store.read_task(task_id)

    result = lineage_for_ideation_task(store, task)  # type: ignore[arg-type]

    assert tuple(link.href for link in result.ideas) == tuple(
        f"/admin/ideas/{iid}/" for iid in idea_ids
    )
    assert all("slug=" in link.descriptor for link in result.ideas)
    assert result.ideas_total == 2
    assert result.transport_errors == 0


def test_ideation_lineage_status_error_returns_empty_ideas() -> None:
    store = _store()
    task_id, _ = _seed_ideation_submission(store, status="error")
    task = store.read_task(task_id)

    result = lineage_for_ideation_task(store, task)  # type: ignore[arg-type]

    assert result.ideas == ()
    assert result.transport_errors == 0


def test_ideation_lineage_drops_missing_idea_without_transport_error() -> None:
    store = _store()
    task_id, idea_ids = _seed_ideation_submission(store, slugs=("alpha",))
    task = store.read_task(task_id)

    class _MissingIdea:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        def read_idea(self, idea_id: str) -> Any:
            raise StorageNotFound(f"idea {idea_id!r}")

    result = lineage_for_ideation_task(
        _MissingIdea(store), task  # type: ignore[arg-type]
    )

    assert result.ideas == ()
    assert result.ideas_total == len(idea_ids)
    assert result.transport_errors == 0


def test_ideation_lineage_transport_error_increments_counter() -> None:
    store = _store()
    task_id, _ = _seed_ideation_submission(store, slugs=("alpha",))
    task = store.read_task(task_id)

    class _Flaky:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        def read_idea(self, idea_id: str) -> Any:
            raise RuntimeError("transport blip")

    result = lineage_for_ideation_task(_Flaky(store), task)  # type: ignore[arg-type]

    assert result.ideas == ()
    assert result.transport_errors == 1


# ---------------------------------------------------------------------
# ExecutionTaskLineage
# ---------------------------------------------------------------------


def test_execution_lineage_back_to_idea_forward_to_variant() -> None:
    store = _store()
    _, (idea_id,) = _seed_ideation_submission(store, slugs=("alpha",))
    exec_task_id, variant_id = _seed_execution_to_starting(
        store, idea_id=idea_id
    )
    task = store.read_task(exec_task_id)

    result = lineage_for_execution_task(store, task)  # type: ignore[arg-type]

    assert isinstance(result, ExecutionTaskLineage)
    assert result.idea is not None
    assert result.idea.href == f"/admin/ideas/{idea_id}/"
    assert len(result.variants) == 1
    assert result.variants[0].href == f"/admin/variants/{variant_id}/"
    assert result.transport_errors == 0


def test_execution_lineage_multiple_variants_on_reclaim() -> None:
    """Same idea, two variants — both surface in the lineage."""
    store = _store()
    _, (idea_id,) = _seed_ideation_submission(store, slugs=("alpha",))
    # First execution: create variant v1; status=error → exec task fails.
    store.create_execution_task("exec-1", idea_id)
    claim_1 = store.claim("exec-1", "executor-w")
    store.create_variant(
        Variant(
            variant_id="v1",
            experiment_id=store.experiment_id,
            idea_id=idea_id,
            status="starting",
            parent_commits=[BASE_SHA],
            branch="work/v1",
            started_at="2026-04-24T12:00:00Z",
        )
    )
    store.submit(
        "exec-1",
        claim_1.worker_id,
        VariantSubmission(status="error", variant_id="v1"),
    )
    store.reject("exec-1", "worker_error")

    # Second execution against the same idea — a fresh task; idea must
    # be ready again. We synthesize a second execution by creating a
    # second variant manually and a parallel task. Actual reclaim
    # behaviour is implementation-specific; for this unit test we just
    # need two variants with the same idea_id.
    store.create_variant(
        Variant(
            variant_id="v2",
            experiment_id=store.experiment_id,
            idea_id=idea_id,
            status="starting",
            parent_commits=[BASE_SHA],
            branch="work/v2",
            started_at="2026-04-24T12:30:00Z",
        )
    )

    task = store.read_task("exec-1")
    result = lineage_for_execution_task(store, task)  # type: ignore[arg-type]

    assert len(result.variants) == 2
    # Ordered by started_at, oldest first
    assert result.variants[0].href == "/admin/variants/v1/"
    assert result.variants[1].href == "/admin/variants/v2/"


def test_execution_lineage_unknown_idea_is_silent() -> None:
    """The ideation submission's idea was deleted out from under us."""
    store = _store()
    # Create an execution task referencing an idea that never existed:
    # we have to seed an idea, then drop it from a mock store. Simpler:
    # drive the idea through ready+dispatched normally, then wrap with
    # a mock that 404s on read_idea.
    _, (idea_id,) = _seed_ideation_submission(store, slugs=("alpha",))
    exec_task_id, _ = _seed_execution_to_starting(store, idea_id=idea_id)
    task = store.read_task(exec_task_id)

    class _MissingIdea:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        def read_idea(self, idea_id: str) -> Any:
            raise StorageNotFound(f"idea {idea_id!r}")

    result = lineage_for_execution_task(_MissingIdea(store), task)  # type: ignore[arg-type]

    assert result.idea is None
    assert result.transport_errors == 0
    # forward link still resolves
    assert len(result.variants) == 1


# ---------------------------------------------------------------------
# EvaluationTaskLineage
# ---------------------------------------------------------------------


def test_evaluation_lineage_back_to_variant() -> None:
    store = _store()
    _, (idea_id,) = _seed_ideation_submission(store, slugs=("alpha",))
    _, variant_id = _seed_execution_completed(store, idea_id=idea_id)
    store.create_evaluation_task("eval-1", variant_id)
    task = store.read_task("eval-1")

    result = lineage_for_evaluation_task(store, task)  # type: ignore[arg-type]

    assert isinstance(result, EvaluationTaskLineage)
    assert result.variant is not None
    assert result.variant.href == f"/admin/variants/{variant_id}/"
    assert result.transport_errors == 0


def test_evaluation_lineage_missing_variant_renders_none() -> None:
    store = _store()
    _, (idea_id,) = _seed_ideation_submission(store, slugs=("alpha",))
    _, variant_id = _seed_execution_completed(store, idea_id=idea_id)
    store.create_evaluation_task("eval-1", variant_id)
    task = store.read_task("eval-1")

    class _MissingVariant:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        def read_variant(self, variant_id: str) -> Any:
            raise StorageNotFound(f"variant {variant_id!r}")

    result = lineage_for_evaluation_task(_MissingVariant(store), task)  # type: ignore[arg-type]

    assert result.variant is None
    assert result.transport_errors == 0


# ---------------------------------------------------------------------
# IdeaLineage — reverse walk to ideation task
# ---------------------------------------------------------------------


def test_idea_lineage_reverse_finds_originating_ideation_task() -> None:
    store = _store()
    task_id, (idea_id,) = _seed_ideation_submission(
        store, task_id="plan-find-me", slugs=("alpha",)
    )
    idea = store.read_idea(idea_id)

    result = lineage_for_idea(store, idea)

    assert isinstance(result, IdeaLineage)
    assert result.ideation_task is not None
    assert result.ideation_task.href == f"/admin/tasks/{task_id}/"
    assert result.transport_errors == 0


def test_idea_lineage_disambiguates_among_multiple_ideation_tasks() -> None:
    store = _store()
    _seed_ideation_submission(
        store, task_id="plan-1", slugs=("alpha", "beta")
    )
    _, (gamma_id,) = _seed_ideation_submission(
        store, task_id="plan-2", slugs=("gamma",)
    )
    idea = store.read_idea(gamma_id)

    result = lineage_for_idea(store, idea)

    assert result.ideation_task is not None
    assert result.ideation_task.href == "/admin/tasks/plan-2/"


def test_idea_lineage_status_error_ideation_task_does_not_match() -> None:
    """An idea that exists but whose ideation task was status=error is
    not matched (the error submission carries empty idea_ids). Verify
    that an idea created out-of-band is correctly unmatched."""
    store = _store()
    _seed_ideation_submission(store, task_id="plan-error", status="error")
    # Add an idea directly — there's no ideation task that produced it.
    orphan_id = _make_idea(store, slug="orphan")
    idea = store.read_idea(orphan_id)

    result = lineage_for_idea(store, idea)

    assert result.ideation_task is None


def test_idea_lineage_forward_to_variants() -> None:
    store = _store()
    _, (idea_id,) = _seed_ideation_submission(store, slugs=("alpha",))
    _seed_execution_to_starting(store, idea_id=idea_id, variant_id="v1")
    idea = store.read_idea(idea_id)

    result = lineage_for_idea(store, idea)

    assert len(result.variants) == 1
    assert result.variants[0].href == "/admin/variants/v1/"


def test_idea_lineage_pre_submit_ideation_task_yields_none() -> None:
    """An idea whose ideation task is `pending`/`claimed` has no
    submission; the reverse walk skips it."""
    store = _store()
    store.create_ideation_task("plan-pending")
    store.claim("plan-pending", "ideator-w")
    # Independently seed an idea to walk against.
    orphan_id = _make_idea(store, slug="orphan")
    idea = store.read_idea(orphan_id)

    result = lineage_for_idea(store, idea)

    assert result.ideation_task is None


# ---------------------------------------------------------------------
# VariantLineage — producing execution task disambiguation
# ---------------------------------------------------------------------


def test_variant_lineage_one_execution_task_back_one_idea_back() -> None:
    store = _store()
    _, (idea_id,) = _seed_ideation_submission(store, slugs=("alpha",))
    exec_task_id, variant_id = _seed_execution_completed(
        store, idea_id=idea_id
    )
    variant = store.read_variant(variant_id)

    result = lineage_for_variant(store, variant)

    assert isinstance(result, VariantLineage)
    assert result.execution_task is not None
    assert result.execution_task.href == f"/admin/tasks/{exec_task_id}/"
    assert result.idea is not None
    assert result.idea.href == f"/admin/ideas/{idea_id}/"
    assert result.evaluation_tasks == ()


def test_variant_lineage_forward_to_evaluation_tasks() -> None:
    store = _store()
    _, (idea_id,) = _seed_ideation_submission(store, slugs=("alpha",))
    _, variant_id = _seed_execution_completed(store, idea_id=idea_id)
    store.create_evaluation_task("eval-1", variant_id)
    variant = store.read_variant(variant_id)

    result = lineage_for_variant(store, variant)

    assert len(result.evaluation_tasks) == 1
    assert result.evaluation_tasks[0].href == "/admin/tasks/eval-1/"


def test_variant_lineage_attribution_fallback_used_when_unambiguous() -> None:
    """If submission can't be read but attribution matches uniquely,
    fall back to it."""
    store = _store()
    _, (idea_id,) = _seed_ideation_submission(store, slugs=("alpha",))
    exec_task_id, variant_id = _seed_execution_completed(
        store, idea_id=idea_id, worker="executor-w"
    )
    variant = store.read_variant(variant_id)

    class _NoSubmission:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        def read_submission(self, task_id: str) -> Any:
            raise StorageNotFound(f"task {task_id!r}")

    result = lineage_for_variant(_NoSubmission(store), variant)

    assert result.execution_task is not None
    assert result.execution_task.href == f"/admin/tasks/{exec_task_id}/"


def test_variant_lineage_ambiguous_attribution_yields_none() -> None:
    """Two execution tasks against the same idea + same worker, but
    we can't read submissions — refuse to guess and return None."""
    store = _store()
    _, (idea_id,) = _seed_ideation_submission(store, slugs=("alpha",))
    # Two execution tasks against the same idea, both attributed to
    # the same worker. We need the idea to be re-dispatchable.
    store.create_execution_task("exec-A", idea_id)
    claim_a = store.claim("exec-A", "executor-w")
    store.create_variant(
        Variant(
            variant_id="vA",
            experiment_id=store.experiment_id,
            idea_id=idea_id,
            status="starting",
            parent_commits=[BASE_SHA],
            branch="work/vA",
            started_at="2026-04-24T12:00:00Z",
        )
    )
    store.submit(
        "exec-A",
        claim_a.worker_id,
        VariantSubmission(status="error", variant_id="vA"),
    )
    store.reject("exec-A", "worker_error")
    # Synthesize a second execution task referencing the same idea,
    # also attributed to executor-w via submission. Idea is now
    # completed; create_execution_task may refuse. We construct
    # candidates by writing a second execution task directly via
    # create_execution_task is gated, so we use a different shape:
    # use a manual store mock to add a second candidate task.

    class _DualCandidate:
        def __init__(self, inner: Any, extra_candidate: Any) -> None:
            self._inner = inner
            self._extra = extra_candidate

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        def list_tasks(self, *, kind: str | None = None, state: str | None = None) -> list[Any]:  # noqa: E501
            base = self._inner.list_tasks(kind=kind, state=state)
            if kind == "execution":
                return [*base, self._extra]
            return base

        def read_submission(self, task_id: str) -> Any:
            raise StorageNotFound(f"task {task_id!r}")

    # Fabricate a second execution-task candidate object — just needs
    # the fields our helper reads. Use a SimpleNamespace so the nested
    # class scope doesn't shadow the outer ``idea_id`` binding.
    from types import SimpleNamespace

    fake_extra = SimpleNamespace(
        task_id="exec-B",
        state="submitted",
        submitted_by="executor-w",
        payload=SimpleNamespace(idea_id=idea_id),
    )

    # Pin variant.executed_by so the attribution fallback path is
    # eligible; the producer is ambiguous so it must yield None.
    variant_obj = Variant(
        variant_id="vA",
        experiment_id=store.experiment_id,
        idea_id=idea_id,
        status="error",
        parent_commits=[BASE_SHA],
        branch="work/vA",
        started_at="2026-04-24T12:00:00Z",
        executed_by="executor-w",
    )

    result = lineage_for_variant(
        _DualCandidate(store, fake_extra), variant_obj
    )

    assert result.execution_task is None


def test_variant_lineage_no_attribution_no_submission_yields_none() -> None:
    """An auth-disabled deployment with no attribution: no producer found."""
    store = _store()
    _, (idea_id,) = _seed_ideation_submission(store, slugs=("alpha",))
    # Seed a variant with no associated execution task. Variants
    # must enter in `starting` per the store invariant.
    store.create_variant(
        Variant(
            variant_id="lonely",
            experiment_id=store.experiment_id,
            idea_id=idea_id,
            status="starting",
            parent_commits=[BASE_SHA],
            branch="work/lonely",
            started_at="2026-04-24T12:00:00Z",
        )
    )
    variant = store.read_variant("lonely")
    # variant.executed_by stays None — attribution fallback is
    # ineligible, and there's no execution-task record.

    result = lineage_for_variant(store, variant)

    assert result.execution_task is None


# ---------------------------------------------------------------------
# Cap behavior
# ---------------------------------------------------------------------


def test_ideation_lineage_caps_at_twenty() -> None:
    """A submission with > 20 ideas renders 20 links + total = N."""
    store = _store()
    store.create_ideation_task("plan-many")
    claim = store.claim("plan-many", "ideator-w")
    slugs = [f"s{i:02d}" for i in range(25)]
    idea_ids = [_make_idea(store, slug=s) for s in slugs]
    store.submit(
        "plan-many",
        claim.worker_id,
        IdeaSubmission(status="success", idea_ids=tuple(idea_ids)),
    )
    task = store.read_task("plan-many")

    result = lineage_for_ideation_task(store, task)  # type: ignore[arg-type]

    assert len(result.ideas) == 20
    assert result.ideas_total == 25


# ---------------------------------------------------------------------
# LineageLink shape
# ---------------------------------------------------------------------


def test_lineage_link_descriptor_includes_state() -> None:
    store = _store()
    _, (idea_id,) = _seed_ideation_submission(store, slugs=("alpha",))
    exec_task_id, _ = _seed_execution_completed(store, idea_id=idea_id)
    task = store.read_task(exec_task_id)

    result = lineage_for_execution_task(store, task)  # type: ignore[arg-type]

    assert result.idea is not None
    assert "slug=alpha" in result.idea.descriptor
    assert "state=" in result.idea.descriptor


def test_lineage_link_is_a_dataclass() -> None:
    link = LineageLink(label="x", href="/y/", descriptor="z")
    # Frozen dataclass — assignment raises FrozenInstanceError.
    with pytest.raises((AttributeError, Exception)) as exc_info:
        link.label = "other"  # type: ignore[misc]
    # dataclasses.FrozenInstanceError subclasses AttributeError on py3.11+.
    assert "frozen" in str(exc_info.value).lower() or isinstance(
        exc_info.value, AttributeError
    )


# ---------------------------------------------------------------------
# EvaluationSubmission unused-import guard
# ---------------------------------------------------------------------


def test_evaluationsubmission_export_is_present() -> None:
    """Sanity guard: the evaluator-side imports compile."""
    assert EvaluationSubmission is not None
