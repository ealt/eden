"""Scripted reference workers.

These workers drive the in-memory store through a full experiment
lifecycle using deterministic fake outputs. They exercise the real
state machine — claim, execute, submit — so the dispatch loop's
behavior can be asserted end-to-end without any LLM or git machinery.

Phase 5 non-goals (roadmap):
  • no git: executor ``commit_sha`` values are fabricated.
  • no evaluation logic: metrics come from a script hook.
  • no dispatch policy: there is one worker per role.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from eden_contracts import (
    EvaluationTask,
    ExecutionTask,
    Idea,
    IdeationTask,
    Variant,
)
from eden_storage import (
    EvaluationSubmission,
    IdeaSubmission,
    Store,
    VariantSubmission,
)


@dataclass(frozen=True)
class IdeaTemplate:
    """Stand-in for the ideator's domain logic; ideator persists these as ideas."""

    slug: str
    priority: float
    parent_commits: tuple[str, ...]
    artifacts_uri: str


@dataclass(frozen=True)
class ExecutionOutcome:
    """Stand-in for the executor's output."""

    status: Literal["success", "error"]
    commit_sha: str | None = None
    branch: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class EvaluationOutcome:
    """Stand-in for the evaluator's output."""

    status: Literal["success", "error", "evaluation_error"]
    evaluation: dict[str, Any] | None = None
    artifacts_uri: str | None = None


IdeationFn = Callable[[IdeationTask], list[IdeaTemplate]]
ExecutionFn = Callable[[ExecutionTask, Idea], ExecutionOutcome]
EvaluationFn = Callable[[EvaluationTask, Variant], EvaluationOutcome]


class ScriptedIdeator:
    """Poll-and-run ideator worker.

    Discovers pending ``ideation`` tasks, claims each in turn, drafts its
    scripted ideas (one by one, marking each ``ready`` before
    submitting), and submits with ``status=success``. Multi-idea
    drafting per plan is supported; zero-idea plans also submit
    with success per ``03-roles.md`` §2.4.
    """

    def __init__(
        self,
        worker_id: str,
        ideation_fn: IdeationFn,
        *,
        idea_id_factory: Callable[[], str],
        now: Callable[[], str],
    ) -> None:
        self._worker_id = worker_id
        self._ideation_fn = ideation_fn
        self._idea_id_factory = idea_id_factory
        self._now = now

    @property
    def worker_id(self) -> str:
        """Opaque worker identifier this ideator claims tasks under."""
        return self._worker_id

    def run_pending(
        self,
        store: Store,
        *,
        stop: Callable[[], bool] | None = None,
    ) -> int:
        """Claim and process every pending ideation task. Returns count processed.

        If ``stop`` is provided, it is consulted before each task; the loop
        returns early when it returns ``True``. This lets a host running
        in a SIGTERM-aware loop break mid-drain instead of finishing the
        whole queue first.
        """
        count = 0
        while True:
            if stop is not None and stop():
                return count
            pending = store.list_tasks(kind="ideation", state="pending")
            if not pending:
                return count
            task = pending[0]
            assert isinstance(task, IdeationTask)
            self._handle(store, task)
            count += 1

    def _handle(self, store: Store, task: IdeationTask) -> None:
        claim = store.claim(task.task_id, self._worker_id)
        templates = self._ideation_fn(task)
        idea_ids: list[str] = []
        for tpl in templates:
            idea_id = self._idea_id_factory()
            idea = Idea(
                idea_id=idea_id,
                experiment_id=store.experiment_id,
                slug=tpl.slug,
                priority=tpl.priority,
                parent_commits=list(tpl.parent_commits),
                artifacts_uri=tpl.artifacts_uri,
                state="drafting",
                created_at=self._now(),
            )
            store.create_idea(idea)
            store.mark_idea_ready(idea_id)
            idea_ids.append(idea_id)
        store.submit(
            task.task_id,
            claim.worker_id,
            IdeaSubmission(status="success", idea_ids=tuple(idea_ids)),
        )


class ScriptedExecutor:
    """Poll-and-run executor worker.

    Discovers pending ``execution`` tasks, reads the referenced
    idea, creates a ``starting`` variant on a scripted ``work/*``
    branch, and submits with the scripted outcome. A successful
    outcome carries ``commit_sha``; an errored outcome submits with
    ``status=error``.
    """

    def __init__(
        self,
        worker_id: str,
        execution_fn: ExecutionFn,
        *,
        variant_id_factory: Callable[[], str],
        now: Callable[[], str],
    ) -> None:
        self._worker_id = worker_id
        self._execution_fn = execution_fn
        self._variant_id_factory = variant_id_factory
        self._now = now

    @property
    def worker_id(self) -> str:
        """Opaque worker identifier this executor claims tasks under."""
        return self._worker_id

    def run_pending(
        self,
        store: Store,
        *,
        stop: Callable[[], bool] | None = None,
    ) -> int:
        """Claim and process every pending execution task. Returns count processed.

        ``stop`` lets a host break mid-drain on SIGTERM. See
        :meth:`ScriptedIdeator.run_pending`.
        """
        count = 0
        while True:
            if stop is not None and stop():
                return count
            pending = store.list_tasks(kind="execution", state="pending")
            if not pending:
                return count
            task = pending[0]
            assert isinstance(task, ExecutionTask)
            self._handle(store, task)
            count += 1

    def _handle(self, store: Store, task: ExecutionTask) -> None:
        idea = store.read_idea(task.payload.idea_id)
        claim = store.claim(task.task_id, self._worker_id)

        variant_id = self._variant_id_factory()
        outcome = self._execution_fn(task, idea)
        branch = outcome.branch or f"work/{idea.slug}-{variant_id}"
        variant_kwargs: dict[str, Any] = {
            "variant_id": variant_id,
            "experiment_id": store.experiment_id,
            "idea_id": idea.idea_id,
            "status": "starting",
            "parent_commits": list(idea.parent_commits),
            "branch": branch,
            "started_at": self._now(),
        }
        if outcome.description is not None:
            variant_kwargs["description"] = outcome.description
        variant = Variant(**variant_kwargs)
        store.create_variant(variant)
        store.submit(
            task.task_id,
            claim.worker_id,
            VariantSubmission(
                status=outcome.status,
                variant_id=variant_id,
                commit_sha=outcome.commit_sha,
            ),
        )


class ScriptedEvaluator:
    """Poll-and-run evaluator worker.

    Discovers pending ``evaluation`` tasks, reads the referenced variant,
    runs its scripted evaluation, and submits. ``success`` and
    ``error`` write metrics on the variant via the orchestrator's
    terminal transition; ``evaluation_error`` leaves the variant in
    ``starting`` per ``03-roles.md`` §4.4.
    """

    def __init__(
        self,
        worker_id: str,
        evaluation_fn: EvaluationFn,
    ) -> None:
        self._worker_id = worker_id
        self._evaluation_fn = evaluation_fn

    @property
    def worker_id(self) -> str:
        """Opaque worker identifier this evaluator claims tasks under."""
        return self._worker_id

    def run_pending(
        self,
        store: Store,
        *,
        stop: Callable[[], bool] | None = None,
    ) -> int:
        """Claim and process every pending evaluation task. Returns count processed.

        ``stop`` lets a host break mid-drain on SIGTERM. See
        :meth:`ScriptedIdeator.run_pending`.
        """
        count = 0
        while True:
            if stop is not None and stop():
                return count
            pending = store.list_tasks(kind="evaluation", state="pending")
            if not pending:
                return count
            task = pending[0]
            assert isinstance(task, EvaluationTask)
            self._handle(store, task)
            count += 1

    def _handle(self, store: Store, task: EvaluationTask) -> None:
        variant = store.read_variant(task.payload.variant_id)
        claim = store.claim(task.task_id, self._worker_id)
        outcome = self._evaluation_fn(task, variant)
        store.submit(
            task.task_id,
            claim.worker_id,
            EvaluationSubmission(
                status=outcome.status,
                variant_id=variant.variant_id,
                evaluation=outcome.evaluation,
                artifacts_uri=outcome.artifacts_uri,
            ),
        )
