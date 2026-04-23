"""Scripted reference workers.

These workers drive the in-memory store through a full experiment
lifecycle using deterministic fake outputs. They exercise the real
state machine — claim, execute, submit — so the dispatch loop's
behavior can be asserted end-to-end without any LLM or git machinery.

Phase 5 non-goals (roadmap):
  • no git: implementer ``commit_sha`` values are fabricated.
  • no evaluation logic: metrics come from a script hook.
  • no dispatch policy: there is one worker per role.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from eden_contracts import (
    EvaluateTask,
    ImplementTask,
    PlanTask,
    Proposal,
    Trial,
)

from .store import (
    EvaluateSubmission,
    ImplementSubmission,
    InMemoryStore,
    PlanSubmission,
)


@dataclass(frozen=True)
class ProposalTemplate:
    """Stand-in for the planner's domain logic; planner persists these as proposals."""

    slug: str
    priority: float
    parent_commits: tuple[str, ...]
    artifacts_uri: str


@dataclass(frozen=True)
class ImplementOutcome:
    """Stand-in for the implementer's output."""

    status: Literal["success", "error"]
    commit_sha: str | None = None
    branch: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class EvaluateOutcome:
    """Stand-in for the evaluator's output."""

    status: Literal["success", "error", "eval_error"]
    metrics: dict[str, Any] | None = None
    artifacts_uri: str | None = None


PlanFn = Callable[[PlanTask], list[ProposalTemplate]]
ImplementFn = Callable[[ImplementTask, Proposal], ImplementOutcome]
EvaluateFn = Callable[[EvaluateTask, Trial], EvaluateOutcome]


class ScriptedPlanner:
    """Poll-and-run planner worker.

    Discovers pending ``plan`` tasks, claims each in turn, drafts its
    scripted proposals (one by one, marking each ``ready`` before
    submitting), and submits with ``status=success``. Multi-proposal
    drafting per plan is supported; zero-proposal plans also submit
    with success per ``03-roles.md`` §2.4.
    """

    def __init__(
        self,
        worker_id: str,
        plan_fn: PlanFn,
        *,
        proposal_id_factory: Callable[[], str],
        now: Callable[[], str],
    ) -> None:
        self._worker_id = worker_id
        self._plan_fn = plan_fn
        self._proposal_id_factory = proposal_id_factory
        self._now = now

    @property
    def worker_id(self) -> str:
        """Opaque worker identifier this planner claims tasks under."""
        return self._worker_id

    def run_pending(self, store: InMemoryStore) -> int:
        """Claim and process every pending plan task. Returns count processed."""
        count = 0
        while True:
            pending = store.list_tasks(kind="plan", state="pending")
            if not pending:
                return count
            task = pending[0]
            assert isinstance(task, PlanTask)
            self._handle(store, task)
            count += 1

    def _handle(self, store: InMemoryStore, task: PlanTask) -> None:
        claim = store.claim(task.task_id, self._worker_id)
        templates = self._plan_fn(task)
        proposal_ids: list[str] = []
        for tpl in templates:
            proposal_id = self._proposal_id_factory()
            proposal = Proposal(
                proposal_id=proposal_id,
                experiment_id=store.experiment_id,
                slug=tpl.slug,
                priority=tpl.priority,
                parent_commits=list(tpl.parent_commits),
                artifacts_uri=tpl.artifacts_uri,
                state="drafting",
                created_at=self._now(),
            )
            store.create_proposal(proposal)
            store.mark_proposal_ready(proposal_id)
            proposal_ids.append(proposal_id)
        store.submit(
            task.task_id,
            claim.token,
            PlanSubmission(status="success", proposal_ids=tuple(proposal_ids)),
        )


class ScriptedImplementer:
    """Poll-and-run implementer worker.

    Discovers pending ``implement`` tasks, reads the referenced
    proposal, creates a ``starting`` trial on a scripted ``work/*``
    branch, and submits with the scripted outcome. A successful
    outcome carries ``commit_sha``; an errored outcome submits with
    ``status=error``.
    """

    def __init__(
        self,
        worker_id: str,
        implement_fn: ImplementFn,
        *,
        trial_id_factory: Callable[[], str],
        now: Callable[[], str],
    ) -> None:
        self._worker_id = worker_id
        self._implement_fn = implement_fn
        self._trial_id_factory = trial_id_factory
        self._now = now

    @property
    def worker_id(self) -> str:
        """Opaque worker identifier this implementer claims tasks under."""
        return self._worker_id

    def run_pending(self, store: InMemoryStore) -> int:
        """Claim and process every pending implement task. Returns count processed."""
        count = 0
        while True:
            pending = store.list_tasks(kind="implement", state="pending")
            if not pending:
                return count
            task = pending[0]
            assert isinstance(task, ImplementTask)
            self._handle(store, task)
            count += 1

    def _handle(self, store: InMemoryStore, task: ImplementTask) -> None:
        proposal = store.read_proposal(task.payload.proposal_id)
        claim = store.claim(task.task_id, self._worker_id)

        trial_id = self._trial_id_factory()
        outcome = self._implement_fn(task, proposal)
        branch = outcome.branch or f"work/{proposal.slug}-{trial_id}"
        trial_kwargs: dict[str, Any] = {
            "trial_id": trial_id,
            "experiment_id": store.experiment_id,
            "proposal_id": proposal.proposal_id,
            "status": "starting",
            "parent_commits": list(proposal.parent_commits),
            "branch": branch,
            "started_at": self._now(),
        }
        if outcome.description is not None:
            trial_kwargs["description"] = outcome.description
        trial = Trial(**trial_kwargs)
        store.create_trial(trial)
        store.submit(
            task.task_id,
            claim.token,
            ImplementSubmission(
                status=outcome.status,
                trial_id=trial_id,
                commit_sha=outcome.commit_sha,
            ),
        )


class ScriptedEvaluator:
    """Poll-and-run evaluator worker.

    Discovers pending ``evaluate`` tasks, reads the referenced trial,
    runs its scripted evaluation, and submits. ``success`` and
    ``error`` write metrics on the trial via the orchestrator's
    terminal transition; ``eval_error`` leaves the trial in
    ``starting`` per ``03-roles.md`` §4.4.
    """

    def __init__(
        self,
        worker_id: str,
        evaluate_fn: EvaluateFn,
    ) -> None:
        self._worker_id = worker_id
        self._evaluate_fn = evaluate_fn

    @property
    def worker_id(self) -> str:
        """Opaque worker identifier this evaluator claims tasks under."""
        return self._worker_id

    def run_pending(self, store: InMemoryStore) -> int:
        """Claim and process every pending evaluate task. Returns count processed."""
        count = 0
        while True:
            pending = store.list_tasks(kind="evaluate", state="pending")
            if not pending:
                return count
            task = pending[0]
            assert isinstance(task, EvaluateTask)
            self._handle(store, task)
            count += 1

    def _handle(self, store: InMemoryStore, task: EvaluateTask) -> None:
        trial = store.read_trial(task.payload.trial_id)
        claim = store.claim(task.task_id, self._worker_id)
        outcome = self._evaluate_fn(task, trial)
        store.submit(
            task.task_id,
            claim.token,
            EvaluateSubmission(
                status=outcome.status,
                trial_id=trial.trial_id,
                metrics=outcome.metrics,
                artifacts_uri=outcome.artifacts_uri,
            ),
        )
