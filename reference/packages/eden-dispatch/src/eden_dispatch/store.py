"""In-memory task/event/proposal/trial store enforcing the v0 invariants.

This module is the reference implementation of the three stores named
in [`spec/v0/08-storage.md`](../../../../spec/v0/08-storage.md) — task
store, event log, proposal/trial persistence — collapsed into a single
in-process object. Collapsing is allowed by §7 (implementation
latitude) and is the simplest arrangement that still honors the
transactional invariant.

The invariant (`05-event-protocol.md` §2): every state change commits
atomically with its event(s). In a single-threaded in-memory store we
realize this by staging all mutations in a `_Tx` object and only
applying them in one shot at the end of the operation; any
precondition failure raises before `commit`, so readers never observe
a partial write.

The ``submit`` operation is idempotent per `04-task-protocol.md` §4.2:
a resubmit with the current token and a content-equivalent payload
succeeds without mutating state; an inconsistent resubmit is rejected.
Content equivalence is role-specific and is computed against the
submission dataclasses defined here, not against the spec's wire
format, because submissions are not part of the wire-format schemas
in v0.

Integration (`06-integrator.md`) is collapsed into a single
``integrate_trial`` operation: the in-memory store has no git
topology to express, so it persists only the ``trial_commit_sha``
field + the ``trial.integrated`` event. Phase 7 will replace this
with a real git integrator.
"""

from __future__ import annotations

import itertools
import secrets
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import RLock
from typing import Any, Literal

from eden_contracts import (
    EvaluatePayload,
    EvaluateTask,
    Event,
    FailReason,
    ImplementPayload,
    ImplementTask,
    PlanPayload,
    PlanTask,
    Proposal,
    ReclaimCause,
    Task,
    TaskClaim,
    Trial,
)

from .errors import (
    AlreadyExists,
    ConflictingResubmission,
    IllegalTransition,
    InvalidPrecondition,
    NotFound,
    WrongToken,
)

PlanStatus = Literal["success", "error"]
ImplementStatus = Literal["success", "error"]
EvaluateStatus = Literal["success", "error", "eval_error"]


@dataclass(frozen=True)
class PlanSubmission:
    """Planner submission result. See spec/v0/03-roles.md §2.4."""

    status: PlanStatus
    proposal_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ImplementSubmission:
    """Implementer submission result. See spec/v0/03-roles.md §3.4."""

    status: ImplementStatus
    trial_id: str
    commit_sha: str | None = None


@dataclass(frozen=True)
class EvaluateSubmission:
    """Evaluator submission result. See spec/v0/03-roles.md §4.4."""

    status: EvaluateStatus
    trial_id: str
    metrics: dict[str, Any] | None = None
    artifacts_uri: str | None = None


Submission = PlanSubmission | ImplementSubmission | EvaluateSubmission


@dataclass
class _Tx:
    """Staged writes for a single atomic operation.

    A public store method stages all mutations here and calls
    ``commit`` exactly once at the end. Any precondition failure
    raises before ``commit``, so readers never observe a partial
    state change.
    """

    tasks: dict[str, Task] = field(default_factory=dict)
    proposals: dict[str, Proposal] = field(default_factory=dict)
    trials: dict[str, Trial] = field(default_factory=dict)
    submissions: dict[str, Submission] = field(default_factory=dict)
    task_deletes_submission: set[str] = field(default_factory=set)
    events: list[Event] = field(default_factory=list)


class InMemoryStore:
    """Reference in-memory implementation of the three v0 stores.

    Thread-safe under a single process lock. All public operations
    are atomic: either every staged write and event is applied, or
    none is.
    """

    def __init__(
        self,
        experiment_id: str,
        *,
        now: Callable[[], datetime] | None = None,
        event_id_factory: Callable[[], str] | None = None,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self._experiment_id = experiment_id
        self._now = now or (lambda: datetime.now(UTC))
        self._event_ids = itertools.count(1)
        self._event_id_factory = event_id_factory or self._default_event_id
        self._token_factory = token_factory or (lambda: secrets.token_hex(16))
        self._tasks: dict[str, Task] = {}
        self._proposals: dict[str, Proposal] = {}
        self._trials: dict[str, Trial] = {}
        self._submissions: dict[str, Submission] = {}
        self._events: list[Event] = []
        self._lock = RLock()

    def _default_event_id(self) -> str:
        return f"evt-{next(self._event_ids):06d}"

    @property
    def experiment_id(self) -> str:
        """The experiment this store is scoped to."""
        return self._experiment_id

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------

    def read_task(self, task_id: str) -> Task:
        """Return the current task object, or raise ``NotFound``."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise NotFound(f"task {task_id!r}")
            return task

    def read_proposal(self, proposal_id: str) -> Proposal:
        """Return the current proposal, or raise ``NotFound``."""
        with self._lock:
            proposal = self._proposals.get(proposal_id)
            if proposal is None:
                raise NotFound(f"proposal {proposal_id!r}")
            return proposal

    def read_trial(self, trial_id: str) -> Trial:
        """Return the current trial, or raise ``NotFound``."""
        with self._lock:
            trial = self._trials.get(trial_id)
            if trial is None:
                raise NotFound(f"trial {trial_id!r}")
            return trial

    def read_submission(self, task_id: str) -> Submission | None:
        """Return the committed submission for a task, or ``None`` if not submitted."""
        with self._lock:
            return self._submissions.get(task_id)

    def list_tasks(
        self,
        *,
        kind: str | None = None,
        state: str | None = None,
    ) -> list[Task]:
        """Return tasks matching an optional ``kind`` and ``state`` filter."""
        with self._lock:
            out: list[Task] = []
            for task in self._tasks.values():
                if kind is not None and task.kind != kind:
                    continue
                if state is not None and task.state != state:
                    continue
                out.append(task)
            return out

    def list_proposals(self, *, state: str | None = None) -> list[Proposal]:
        """Return proposals matching an optional ``state`` filter."""
        with self._lock:
            out = [
                p
                for p in self._proposals.values()
                if state is None or p.state == state
            ]
            return out

    def list_trials(self, *, status: str | None = None) -> list[Trial]:
        """Return trials matching an optional ``status`` filter."""
        with self._lock:
            out = [
                t
                for t in self._trials.values()
                if status is None or t.status == status
            ]
            return out

    def events(self) -> list[Event]:
        """Return a snapshot of the full event log in append order."""
        with self._lock:
            return list(self._events)

    def subscribe(self) -> Iterator[Event]:
        """Yield every event currently in the log. Call again to pick up new events.

        Reference subscription: take-a-snapshot-now, not a live
        stream. The spec requires at-least-once delivery with full
        replay (§4.3, §4.4); a single snapshot read satisfies that
        for synchronous workers that drive the loop themselves.
        """
        with self._lock:
            events = list(self._events)
        yield from events

    # ------------------------------------------------------------------
    # Task lifecycle — creation
    # ------------------------------------------------------------------

    def create_plan_task(self, task_id: str) -> PlanTask:
        """Create a ``plan`` task. Emits ``task.created`` atomically."""
        with self._lock:
            self._require_no_task(task_id)
            now = self._ts()
            task = PlanTask(
                task_id=task_id,
                kind="plan",
                state="pending",
                payload=PlanPayload(experiment_id=self._experiment_id),
                created_at=now,
                updated_at=now,
            )
            tx = _Tx()
            tx.tasks[task_id] = task
            tx.events.append(self._event("task.created", {"task_id": task_id, "kind": "plan"}))
            self._commit(tx)
            return task

    def create_implement_task(self, task_id: str, proposal_id: str) -> ImplementTask:
        """Create an ``implement`` task; composite-commits ``proposal.dispatched``.

        Per ``05-event-protocol.md`` §2.2: creating the implement task
        and transitioning the referenced proposal from ``ready`` to
        ``dispatched`` land in one atomic commit.
        """
        with self._lock:
            self._require_no_task(task_id)
            proposal = self._proposals.get(proposal_id)
            if proposal is None:
                raise NotFound(f"proposal {proposal_id!r}")
            if proposal.state != "ready":
                raise InvalidPrecondition(
                    f"proposal {proposal_id!r} must be 'ready' "
                    f"to dispatch, not {proposal.state!r}"
                )
            now = self._ts()
            task = ImplementTask(
                task_id=task_id,
                kind="implement",
                state="pending",
                payload=ImplementPayload(proposal_id=proposal_id),
                created_at=now,
                updated_at=now,
            )
            tx = _Tx()
            tx.tasks[task_id] = task
            tx.proposals[proposal_id] = proposal.model_copy(update={"state": "dispatched"})
            tx.events.append(
                self._event("task.created", {"task_id": task_id, "kind": "implement"})
            )
            tx.events.append(
                self._event(
                    "proposal.dispatched",
                    {"proposal_id": proposal_id, "task_id": task_id},
                )
            )
            self._commit(tx)
            return task

    def create_evaluate_task(self, task_id: str, trial_id: str) -> EvaluateTask:
        """Create an ``evaluate`` task against a starting trial with ``commit_sha``."""
        with self._lock:
            self._require_no_task(task_id)
            trial = self._trials.get(trial_id)
            if trial is None:
                raise NotFound(f"trial {trial_id!r}")
            if trial.status != "starting":
                raise InvalidPrecondition(
                    f"trial {trial_id!r} must be 'starting' to dispatch eval, "
                    f"not {trial.status!r}"
                )
            if trial.commit_sha is None:
                raise InvalidPrecondition(
                    f"trial {trial_id!r} has no commit_sha; implementer must set it first"
                )
            now = self._ts()
            task = EvaluateTask(
                task_id=task_id,
                kind="evaluate",
                state="pending",
                payload=EvaluatePayload(trial_id=trial_id),
                created_at=now,
                updated_at=now,
            )
            tx = _Tx()
            tx.tasks[task_id] = task
            tx.events.append(
                self._event("task.created", {"task_id": task_id, "kind": "evaluate"})
            )
            self._commit(tx)
            return task

    # ------------------------------------------------------------------
    # Task lifecycle — claim, submit, accept, reject, reclaim
    # ------------------------------------------------------------------

    def claim(
        self,
        task_id: str,
        worker_id: str,
        *,
        expires_at: datetime | str | None = None,
    ) -> TaskClaim:
        """Transition a pending task to claimed. Returns the issued claim.

        Rejects if the task is not in ``pending`` state
        (``04-task-protocol.md`` §3.4). Issues a fresh token.
        """
        with self._lock:
            task = self._require_task(task_id)
            if task.state != "pending":
                raise IllegalTransition(
                    f"cannot claim task in state {task.state!r} (must be 'pending')"
                )
            claimed_at = self._ts()
            claim_kwargs: dict[str, Any] = {
                "token": self._token_factory(),
                "worker_id": worker_id,
                "claimed_at": claimed_at,
            }
            exp = self._maybe_ts(expires_at)
            if exp is not None:
                claim_kwargs["expires_at"] = exp
            claim = TaskClaim(**claim_kwargs)
            now = self._ts()
            tx = _Tx()
            tx.tasks[task_id] = task.model_copy(
                update={"state": "claimed", "claim": claim, "updated_at": now}
            )
            tx.events.append(
                self._event(
                    "task.claimed",
                    {"task_id": task_id, "worker_id": worker_id},
                )
            )
            self._commit(tx)
            return claim

    def submit(self, task_id: str, token: str, submission: Submission) -> None:
        """Transition a claimed task to submitted, persisting the result.

        Idempotent per ``04-task-protocol.md`` §4.2: a resubmit with
        the current token and a content-equivalent payload MUST
        succeed without mutating state; an inconsistent resubmit MUST
        be rejected.
        """
        with self._lock:
            task = self._require_task(task_id)
            if task.state not in {"claimed", "submitted"}:
                raise IllegalTransition(
                    f"cannot submit task in state {task.state!r}"
                )
            if task.claim is None or task.claim.token != token:
                raise WrongToken(f"token does not match current claim on {task_id!r}")
            self._require_submission_kind_matches(task, submission)

            if task.state == "submitted":
                prior = self._submissions.get(task_id)
                if prior is None:
                    raise IllegalTransition(
                        f"task {task_id!r} is submitted but has no recorded submission"
                    )
                if not _submissions_equivalent(prior, submission):
                    raise ConflictingResubmission(
                        f"resubmit of {task_id!r} disagrees with committed result"
                    )
                return

            now = self._ts()
            tx = _Tx()
            tx.tasks[task_id] = task.model_copy(
                update={"state": "submitted", "updated_at": now}
            )
            tx.submissions[task_id] = submission
            tx.events.append(self._event("task.submitted", {"task_id": task_id}))
            self._commit(tx)

    def accept(self, task_id: str) -> None:
        """Orchestrator accept: ``submitted → completed`` with composite effects.

        Dispatches by task kind to emit the right composite-commit
        events (``05-event-protocol.md`` §2.2). Clears the task's
        ``claim``.
        """
        with self._lock:
            task = self._require_task(task_id)
            if task.state != "submitted":
                raise IllegalTransition(
                    f"cannot accept task in state {task.state!r}"
                )
            submission = self._submissions.get(task_id)
            if submission is None:
                raise IllegalTransition(
                    f"task {task_id!r} is submitted but has no recorded submission"
                )
            if task.kind == "plan":
                self._accept_plan(task, submission)
            elif task.kind == "implement":
                self._accept_implement(task, submission)
            else:
                self._accept_evaluate(task, submission)

    def reject(self, task_id: str, reason: FailReason) -> None:
        """Orchestrator reject: ``submitted → failed`` with composite effects.

        Dispatches by task kind. For evaluate tasks, the trial-side
        effect depends on whether the worker declared ``error`` or
        ``eval_error`` (``04-task-protocol.md`` §4.3).
        """
        with self._lock:
            task = self._require_task(task_id)
            if task.state != "submitted":
                raise IllegalTransition(
                    f"cannot reject task in state {task.state!r}"
                )
            submission = self._submissions.get(task_id)
            if submission is None:
                raise IllegalTransition(
                    f"task {task_id!r} is submitted but has no recorded submission"
                )
            if task.kind == "plan":
                self._reject_plan(task, reason)
            elif task.kind == "implement":
                self._reject_implement(task, submission, reason)
            else:
                self._reject_evaluate(task, submission, reason)

    def reclaim(self, task_id: str, cause: ReclaimCause) -> None:
        """Move a claimed (or operator-reclaimed submitted) task back to pending.

        Automatic causes (``expired``, ``health_policy``) are rejected
        against ``submitted`` tasks per ``04-task-protocol.md`` §5.1;
        only ``operator`` reclaim is permitted from ``submitted``. If
        the reclaimed task is an ``implement`` whose prior execution
        left a trial in ``starting``, the trial transitions to
        ``error`` atomically with the reclaim
        (``05-event-protocol.md`` §2.2).
        """
        with self._lock:
            task = self._require_task(task_id)
            if task.state in {"completed", "failed"}:
                raise IllegalTransition(
                    f"cannot reclaim terminal task {task_id!r} (state={task.state!r})"
                )
            if task.state not in {"claimed", "submitted"}:
                raise IllegalTransition(
                    f"cannot reclaim task in state {task.state!r}"
                )
            if task.state == "submitted" and cause != "operator":
                raise IllegalTransition(
                    "automatic reclaim (expired, health_policy) is not permitted "
                    "against a submitted task; only operator reclaim is"
                )

            now = self._ts()
            tx = _Tx()
            tx.tasks[task_id] = task.model_copy(
                update={"state": "pending", "claim": None, "updated_at": now}
            )
            if task_id in self._submissions:
                tx.task_deletes_submission.add(task_id)
            tx.events.append(
                self._event("task.reclaimed", {"task_id": task_id, "cause": cause})
            )

            if task.kind == "implement":
                trial = self._find_starting_trial_for_implement_task(task)
                if trial is not None:
                    tx.trials[trial.trial_id] = trial.model_copy(
                        update={"status": "error", "completed_at": now}
                    )
                    tx.events.append(
                        self._event("trial.errored", {"trial_id": trial.trial_id})
                    )

            self._commit(tx)

    # ------------------------------------------------------------------
    # Proposal store
    # ------------------------------------------------------------------

    def create_proposal(self, proposal: Proposal) -> None:
        """Persist a new proposal in ``drafting``. Emits ``proposal.drafted``."""
        with self._lock:
            if proposal.proposal_id in self._proposals:
                raise AlreadyExists(f"proposal {proposal.proposal_id!r}")
            if proposal.experiment_id != self._experiment_id:
                raise InvalidPrecondition(
                    f"proposal experiment_id {proposal.experiment_id!r} "
                    f"does not match store experiment {self._experiment_id!r}"
                )
            if proposal.state != "drafting":
                raise InvalidPrecondition(
                    f"new proposal must start in 'drafting', not {proposal.state!r}"
                )
            tx = _Tx()
            tx.proposals[proposal.proposal_id] = proposal
            tx.events.append(
                self._event("proposal.drafted", {"proposal_id": proposal.proposal_id})
            )
            self._commit(tx)

    def mark_proposal_ready(self, proposal_id: str) -> None:
        """Transition a proposal ``drafting → ready``. Emits ``proposal.ready``."""
        with self._lock:
            proposal = self._require_proposal(proposal_id)
            if proposal.state != "drafting":
                raise IllegalTransition(
                    f"cannot mark proposal ready from state {proposal.state!r}"
                )
            tx = _Tx()
            tx.proposals[proposal_id] = proposal.model_copy(update={"state": "ready"})
            tx.events.append(self._event("proposal.ready", {"proposal_id": proposal_id}))
            self._commit(tx)

    # ------------------------------------------------------------------
    # Trial store
    # ------------------------------------------------------------------

    def create_trial(self, trial: Trial) -> None:
        """Persist a new trial in ``starting``. Emits ``trial.started``."""
        with self._lock:
            if trial.trial_id in self._trials:
                raise AlreadyExists(f"trial {trial.trial_id!r}")
            if trial.status != "starting":
                raise InvalidPrecondition(
                    f"new trial must start in 'starting', not {trial.status!r}"
                )
            if trial.experiment_id != self._experiment_id:
                raise InvalidPrecondition(
                    f"trial experiment_id {trial.experiment_id!r} "
                    f"does not match store experiment {self._experiment_id!r}"
                )
            tx = _Tx()
            tx.trials[trial.trial_id] = trial
            tx.events.append(
                self._event(
                    "trial.started",
                    {"trial_id": trial.trial_id, "proposal_id": trial.proposal_id},
                )
            )
            self._commit(tx)

    def declare_trial_eval_error(self, trial_id: str) -> None:
        """Retry-exhausted terminal: ``starting → eval_error`` (``05-event-protocol.md`` §2.2).

        Writes ``completed_at`` atomically; MUST NOT set metrics or
        artifacts_uri (``03-roles.md`` §4.4).
        """
        with self._lock:
            trial = self._require_trial(trial_id)
            if trial.status != "starting":
                raise IllegalTransition(
                    f"cannot declare eval_error from trial status {trial.status!r}"
                )
            now = self._ts()
            tx = _Tx()
            tx.trials[trial_id] = trial.model_copy(
                update={"status": "eval_error", "completed_at": now}
            )
            tx.events.append(self._event("trial.eval_errored", {"trial_id": trial_id}))
            self._commit(tx)

    def integrate_trial(self, trial_id: str, trial_commit_sha: str) -> None:
        """Integrator promotion: write ``trial_commit_sha`` and emit ``trial.integrated``.

        Per ``08-storage.md`` §1.7: ``trial_commit_sha`` is the one
        post-terminal write permitted on a trial; it must be written
        atomically with its event.
        """
        with self._lock:
            trial = self._require_trial(trial_id)
            if trial.status != "success":
                raise InvalidPrecondition(
                    f"trial {trial_id!r} must be in 'success' to integrate, "
                    f"not {trial.status!r}"
                )
            if trial.trial_commit_sha is not None:
                raise IllegalTransition(
                    f"trial {trial_id!r} is already integrated"
                )
            tx = _Tx()
            tx.trials[trial_id] = trial.model_copy(
                update={"trial_commit_sha": trial_commit_sha}
            )
            tx.events.append(
                self._event(
                    "trial.integrated",
                    {"trial_id": trial_id, "trial_commit_sha": trial_commit_sha},
                )
            )
            self._commit(tx)

    # ------------------------------------------------------------------
    # Internal dispatch
    # ------------------------------------------------------------------

    def _accept_plan(self, task: Task, submission: Submission) -> None:
        if not isinstance(submission, PlanSubmission):
            raise IllegalTransition("plan task requires PlanSubmission")
        now = self._ts()
        tx = _Tx()
        tx.tasks[task.task_id] = task.model_copy(
            update={"state": "completed", "claim": None, "updated_at": now}
        )
        tx.events.append(self._event("task.completed", {"task_id": task.task_id}))
        self._commit(tx)

    def _reject_plan(self, task: Task, reason: FailReason) -> None:
        now = self._ts()
        tx = _Tx()
        tx.tasks[task.task_id] = task.model_copy(
            update={"state": "failed", "claim": None, "updated_at": now}
        )
        tx.events.append(
            self._event("task.failed", {"task_id": task.task_id, "reason": reason})
        )
        self._commit(tx)

    def _accept_implement(self, task: Task, submission: Submission) -> None:
        if not isinstance(submission, ImplementSubmission):
            raise IllegalTransition("implement task requires ImplementSubmission")
        if submission.status != "success":
            raise IllegalTransition(
                "implement submission with status != 'success' cannot be accepted; "
                "orchestrator must reject"
            )
        if submission.commit_sha is None:
            raise IllegalTransition(
                "implement submission with status='success' requires commit_sha"
            )
        assert isinstance(task, ImplementTask)
        proposal_id = task.payload.proposal_id
        proposal = self._require_proposal(proposal_id)
        trial = self._require_trial(submission.trial_id)

        now = self._ts()
        tx = _Tx()
        tx.tasks[task.task_id] = task.model_copy(
            update={"state": "completed", "claim": None, "updated_at": now}
        )
        tx.proposals[proposal_id] = proposal.model_copy(update={"state": "completed"})
        tx.trials[trial.trial_id] = trial.model_copy(
            update={"commit_sha": submission.commit_sha}
        )
        tx.events.append(self._event("task.completed", {"task_id": task.task_id}))
        tx.events.append(
            self._event(
                "proposal.completed",
                {"proposal_id": proposal_id, "task_id": task.task_id},
            )
        )
        self._commit(tx)

    def _reject_implement(
        self,
        task: Task,
        submission: Submission,
        reason: FailReason,
    ) -> None:
        assert isinstance(task, ImplementTask)
        proposal_id = task.payload.proposal_id
        proposal = self._require_proposal(proposal_id)
        trial_for_error: Trial | None = None
        if isinstance(submission, ImplementSubmission):
            trial = self._trials.get(submission.trial_id)
            if trial is not None and trial.status == "starting":
                trial_for_error = trial

        now = self._ts()
        tx = _Tx()
        tx.tasks[task.task_id] = task.model_copy(
            update={"state": "failed", "claim": None, "updated_at": now}
        )
        tx.proposals[proposal_id] = proposal.model_copy(update={"state": "completed"})
        tx.events.append(
            self._event("task.failed", {"task_id": task.task_id, "reason": reason})
        )
        tx.events.append(
            self._event(
                "proposal.completed",
                {"proposal_id": proposal_id, "task_id": task.task_id},
            )
        )
        if trial_for_error is not None:
            tx.trials[trial_for_error.trial_id] = trial_for_error.model_copy(
                update={"status": "error", "completed_at": now}
            )
            tx.events.append(
                self._event("trial.errored", {"trial_id": trial_for_error.trial_id})
            )
        self._commit(tx)

    def _accept_evaluate(self, task: Task, submission: Submission) -> None:
        if not isinstance(submission, EvaluateSubmission):
            raise IllegalTransition("evaluate task requires EvaluateSubmission")
        if submission.status != "success":
            raise IllegalTransition(
                "evaluate submission with status != 'success' cannot be accepted"
            )
        trial = self._require_trial(submission.trial_id)
        if trial.status != "starting":
            raise IllegalTransition(
                f"cannot succeed trial {trial.trial_id!r} from status {trial.status!r}"
            )
        if trial.commit_sha is None:
            raise IllegalTransition(
                f"trial {trial.trial_id!r} has no commit_sha at success time"
            )

        now = self._ts()
        tx = _Tx()
        tx.tasks[task.task_id] = task.model_copy(
            update={"state": "completed", "claim": None, "updated_at": now}
        )
        tx.trials[trial.trial_id] = trial.model_copy(
            update={
                "status": "success",
                "metrics": dict(submission.metrics) if submission.metrics else None,
                "artifacts_uri": submission.artifacts_uri,
                "completed_at": now,
            }
        )
        tx.events.append(self._event("task.completed", {"task_id": task.task_id}))
        tx.events.append(
            self._event(
                "trial.succeeded",
                {"trial_id": trial.trial_id, "commit_sha": trial.commit_sha},
            )
        )
        self._commit(tx)

    def _reject_evaluate(
        self,
        task: Task,
        submission: Submission,
        reason: FailReason,
    ) -> None:
        if not isinstance(submission, EvaluateSubmission):
            raise IllegalTransition("evaluate task requires EvaluateSubmission")
        trial = self._require_trial(submission.trial_id)

        now = self._ts()
        tx = _Tx()
        tx.tasks[task.task_id] = task.model_copy(
            update={"state": "failed", "claim": None, "updated_at": now}
        )
        tx.events.append(
            self._event("task.failed", {"task_id": task.task_id, "reason": reason})
        )

        if submission.status == "eval_error":
            # Per 03-roles.md §4.4: no trial writes; trial stays in starting.
            pass
        else:
            if trial.status != "starting":
                raise IllegalTransition(
                    f"cannot error trial {trial.trial_id!r} from status {trial.status!r}"
                )
            tx.trials[trial.trial_id] = trial.model_copy(
                update={
                    "status": "error",
                    "metrics": (
                        dict(submission.metrics) if submission.metrics else None
                    ),
                    "artifacts_uri": submission.artifacts_uri,
                    "completed_at": now,
                }
            )
            tx.events.append(self._event("trial.errored", {"trial_id": trial.trial_id}))

        self._commit(tx)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _find_starting_trial_for_implement_task(self, task: Task) -> Trial | None:
        assert isinstance(task, ImplementTask)
        for trial in self._trials.values():
            if (
                trial.proposal_id == task.payload.proposal_id
                and trial.status == "starting"
            ):
                return trial
        return None

    def _require_submission_kind_matches(self, task: Task, submission: Submission) -> None:
        expected: type[Submission]
        if task.kind == "plan":
            expected = PlanSubmission
        elif task.kind == "implement":
            expected = ImplementSubmission
        else:
            expected = EvaluateSubmission
        if not isinstance(submission, expected):
            raise IllegalTransition(
                f"task {task.task_id!r} (kind={task.kind!r}) requires "
                f"{expected.__name__}, got {type(submission).__name__}"
            )

    def _require_no_task(self, task_id: str) -> None:
        if task_id in self._tasks:
            raise AlreadyExists(f"task {task_id!r}")

    def _require_task(self, task_id: str) -> Task:
        task = self._tasks.get(task_id)
        if task is None:
            raise NotFound(f"task {task_id!r}")
        return task

    def _require_proposal(self, proposal_id: str) -> Proposal:
        proposal = self._proposals.get(proposal_id)
        if proposal is None:
            raise NotFound(f"proposal {proposal_id!r}")
        return proposal

    def _require_trial(self, trial_id: str) -> Trial:
        trial = self._trials.get(trial_id)
        if trial is None:
            raise NotFound(f"trial {trial_id!r}")
        return trial

    def _event(self, type_: str, data: dict[str, Any]) -> Event:
        return Event(
            event_id=self._event_id_factory(),
            type=type_,
            occurred_at=self._ts(),
            experiment_id=self._experiment_id,
            data=data,
        )

    def _ts(self) -> str:
        dt = self._now()
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        else:
            dt = dt.astimezone(UTC)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    def _maybe_ts(self, value: datetime | str | None) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        else:
            value = value.astimezone(UTC)
        return value.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    def _commit(self, tx: _Tx) -> None:
        for task_id, task in tx.tasks.items():
            self._tasks[task_id] = task
        for proposal_id, proposal in tx.proposals.items():
            self._proposals[proposal_id] = proposal
        for trial_id, trial in tx.trials.items():
            self._trials[trial_id] = trial
        for task_id, submission in tx.submissions.items():
            self._submissions[task_id] = submission
        for task_id in tx.task_deletes_submission:
            self._submissions.pop(task_id, None)
        self._events.extend(tx.events)


def _submissions_equivalent(a: Submission, b: Submission) -> bool:
    """Content equivalence per ``04-task-protocol.md`` §4.2.

    Plan: compare status and the *set* of proposal_ids (order is not
    significant per ``03-roles.md`` §2.4). Implement: compare status,
    trial_id, commit_sha. Evaluate: compare status, trial_id, metrics
    (as JSON values, so dict equality), artifacts_uri.
    """
    if type(a) is not type(b):
        return False
    if isinstance(a, PlanSubmission) and isinstance(b, PlanSubmission):
        return a.status == b.status and set(a.proposal_ids) == set(b.proposal_ids)
    if isinstance(a, ImplementSubmission) and isinstance(b, ImplementSubmission):
        return (
            a.status == b.status
            and a.trial_id == b.trial_id
            and a.commit_sha == b.commit_sha
        )
    if isinstance(a, EvaluateSubmission) and isinstance(b, EvaluateSubmission):
        return (
            a.status == b.status
            and a.trial_id == b.trial_id
            and a.metrics == b.metrics
            and a.artifacts_uri == b.artifacts_uri
        )
    return False


def iter_events_by_type(events: Iterable[Event], type_: str) -> Iterator[Event]:
    """Yield events whose ``type`` equals ``type_``. Convenience for tests."""
    for event in events:
        if event.type == type_:
            yield event
