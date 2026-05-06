"""Backend-agnostic transition logic shared by every ``Store`` backend.

Every EDEN store backend — in-memory, SQLite, a future Postgres or
remote backend — has to enforce the same state-machine, the same
composite commits (``spec/v0/05-event-protocol.md`` §2.2), the same
token/idempotency/terminal-immutability rules, and the same metrics
validation. Factoring that logic out of any one backend and sharing
it is the only way to make "passes the same conformance suite"
mean what it says.

``_StoreBase`` owns:

- The public method surface (``claim``, ``submit``, ``accept``,
  ``reject``, ``reclaim``, ``create_*``, ``read_*``, ``list_*``,
  ``events``, ``validate_acceptance``, ``validate_terminal``,
  ``create_idea``, ``mark_idea_ready``, ``create_variant``,
  ``declare_variant_evaluation_error``, ``integrate_variant``).
- All validation, composite-commit staging, and event construction.

Subclasses own:

- ``_atomic_operation`` — the transaction scope. In-memory wraps an
  ``RLock``; SQLite wraps ``BEGIN IMMEDIATE``…``COMMIT``.
- ``_get_task``/``_get_idea``/``_get_variant``/``_get_submission``
  — primitive lookups.
- ``_iter_tasks``/``_iter_ideas``/``_iter_variants``/``_iter_events``
  — ordered iteration.
- ``_apply_commit(tx)`` — apply the staged ``_Tx`` inside the already-
  open transaction, without committing it. The outer
  ``_atomic_operation`` context does the actual commit.

Every public method follows the same pattern: open an atomic
operation, perform reads + validations (which may raise before any
write), stage all writes into a ``_Tx`` object, and call
``self._apply_commit(tx)`` exactly once. If validation raises the
atomic operation aborts and no partial state becomes visible
(chapter 8 §6.1–§6.3).
"""

from __future__ import annotations

import copy
import itertools
import math
import secrets
from collections.abc import Callable, Iterable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from eden_contracts import (
    EvaluationPayload,
    EvaluationSchema,
    EvaluationTask,
    Event,
    ExecutionPayload,
    ExecutionTask,
    FailReason,
    Idea,
    IdeationPayload,
    IdeationTask,
    ReclaimCause,
    Task,
    TaskClaim,
    Variant,
)
from pydantic import BaseModel, ValidationError

from .errors import (
    AlreadyExists,
    ConflictingResubmission,
    IllegalTransition,
    InvalidPrecondition,
    NotFound,
    WrongToken,
)
from .submissions import (
    EvaluationSubmission,
    IdeaSubmission,
    Submission,
    VariantSubmission,
    submissions_equivalent,
)

_METRIC_PY_TYPES: dict[str, tuple[type, ...]] = {
    # spec/v0/02-data-model.md §1.3 type mapping: integer / real / text.
    # bool is excluded from "integer" even though it is a Python int subclass.
    "integer": (int,),
    "real": (int, float),
    "text": (str,),
}


@dataclass
class _Tx:
    """Staged writes for a single atomic operation.

    A public method stages all mutations here and calls
    ``_apply_commit`` exactly once at the end. Any precondition
    failure raises before ``_apply_commit``, so readers never observe
    a partial state change. Subclasses apply the contents against
    whatever backing store they use (dicts, SQLite tables, …).
    """

    tasks: dict[str, Task] = field(default_factory=dict)
    ideas: dict[str, Idea] = field(default_factory=dict)
    variants: dict[str, Variant] = field(default_factory=dict)
    submissions: dict[str, Submission] = field(default_factory=dict)
    task_deletes_submission: set[str] = field(default_factory=set)
    events: list[Event] = field(default_factory=list)


def _validated_update[M: BaseModel](model: M, **changes: Any) -> M:
    """Return a copy of ``model`` with ``changes`` applied and re-validated.

    Replaces Pydantic's ``model_copy(update=...)``, which does **not**
    re-run validators. Without re-validation a caller could stamp an
    invalid ``commit_sha``, ``artifacts_uri``, or ``metrics`` shape
    onto a stored variant. Re-validating on every update is how every
    backend honors ``03-roles.md`` §3.4, §4.4 and ``08-storage.md``
    §3.

    Passing ``None`` for a field removes it (matches the ``NotNone``
    rule on optional typed fields in ``eden_contracts._common``:
    absent is permitted, explicit null is not).
    """
    data = model.model_dump(mode="json", exclude_none=True)
    for key, value in changes.items():
        if value is None:
            data.pop(key, None)
        elif isinstance(value, BaseModel):
            data[key] = value.model_dump(mode="json", exclude_none=True)
        else:
            data[key] = value
    return type(model).model_validate(data)


def _deep[M: BaseModel](model: M) -> M:
    """Return a deep copy of a Pydantic model.

    Used to insulate readers (``read_*``, ``list_*``, ``events``)
    from in-place mutation of stored values, and to insulate the
    store from mutation of caller-supplied values at ``create_*``
    time. Backends whose read path inherently rehydrates a fresh
    instance (e.g. SQLite's JSON round-trip) still go through
    ``_deep`` for uniformity.
    """
    return model.model_copy(deep=True)


class _StoreBase:
    """Shared transaction/validation/event logic for every store backend.

    Subclasses implement the backend primitives listed at module-top.
    The public surface here is the union of everything ``protocol.Store``
    declares.
    """

    def __init__(
        self,
        experiment_id: str,
        *,
        evaluation_schema: EvaluationSchema | None = None,
        now: Callable[[], datetime] | None = None,
        event_id_factory: Callable[[], str] | None = None,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self._experiment_id = experiment_id
        self._evaluation_schema = evaluation_schema
        self._now = now or (lambda: datetime.now(UTC))
        self._event_ids = itertools.count(1)
        self._event_id_factory = event_id_factory or self._default_event_id
        self._token_factory = token_factory or (lambda: secrets.token_hex(16))

    def _default_event_id(self) -> str:
        return f"evt-{next(self._event_ids):06d}"

    @property
    def experiment_id(self) -> str:
        """The experiment this store is scoped to."""
        return self._experiment_id

    # ------------------------------------------------------------------
    # Backend primitives (subclasses MUST override)
    # ------------------------------------------------------------------

    def _atomic_operation(self) -> AbstractContextManager[None]:
        """Return a context manager providing atomic-operation semantics.

        Either every write staged inside the context lands, or none of
        them does. The outer context manager's exit is responsible for
        committing (normal exit) or rolling back (exception); the inner
        ``_apply_commit`` call stages the writes without committing.
        """
        raise NotImplementedError

    def _get_task(self, task_id: str) -> Task | None:
        """Return the stored task, or ``None`` if absent."""
        raise NotImplementedError

    def _get_idea(self, idea_id: str) -> Idea | None:
        """Return the stored idea, or ``None`` if absent."""
        raise NotImplementedError

    def _get_variant(self, variant_id: str) -> Variant | None:
        """Return the stored variant, or ``None`` if absent."""
        raise NotImplementedError

    def _get_submission(self, task_id: str) -> Submission | None:
        """Return the committed submission, or ``None`` if absent."""
        raise NotImplementedError

    def _iter_tasks(
        self, *, kind: str | None = None, state: str | None = None
    ) -> Iterable[Task]:
        """Iterate tasks matching the optional filters."""
        raise NotImplementedError

    def _iter_ideas(self, *, state: str | None = None) -> Iterable[Idea]:
        """Iterate ideas matching the optional filter."""
        raise NotImplementedError

    def _iter_variants(self, *, status: str | None = None) -> Iterable[Variant]:
        """Iterate variants matching the optional filter."""
        raise NotImplementedError

    def _iter_events(self) -> Iterable[Event]:
        """Iterate events in log order."""
        raise NotImplementedError

    def _apply_commit(self, tx: _Tx) -> None:
        """Stage the contents of ``tx`` for the current atomic operation.

        For in-memory backends this applies directly to dicts. For
        SQLite it issues INSERT/UPDATE/DELETE statements against the
        already-open transaction; COMMIT fires when
        ``_atomic_operation`` exits without an exception.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Public read API
    # ------------------------------------------------------------------

    def read_task(self, task_id: str) -> Task:
        """Return a snapshot of the current task, or raise ``NotFound``."""
        with self._atomic_operation():
            task = self._get_task(task_id)
            if task is None:
                raise NotFound(f"task {task_id!r}")
            return _deep(task)

    def read_idea(self, idea_id: str) -> Idea:
        """Return a snapshot of the current idea, or raise ``NotFound``."""
        with self._atomic_operation():
            idea = self._get_idea(idea_id)
            if idea is None:
                raise NotFound(f"idea {idea_id!r}")
            return _deep(idea)

    def read_variant(self, variant_id: str) -> Variant:
        """Return a snapshot of the current variant, or raise ``NotFound``."""
        with self._atomic_operation():
            variant = self._get_variant(variant_id)
            if variant is None:
                raise NotFound(f"variant {variant_id!r}")
            return _deep(variant)

    def read_submission(self, task_id: str) -> Submission | None:
        """Return the committed submission for a task, or ``None`` if not submitted.

        Returns a deep copy. Submission dataclasses are ``frozen``,
        but their nested ``metrics`` dict is not; without deep copy a
        caller could mutate the committed metrics in place and
        corrupt future idempotency decisions.
        """
        with self._atomic_operation():
            submission = self._get_submission(task_id)
            if submission is None:
                return None
            return copy.deepcopy(submission)

    def list_tasks(
        self,
        *,
        kind: str | None = None,
        state: str | None = None,
    ) -> list[Task]:
        """Return snapshots of tasks matching an optional ``kind`` and ``state``."""
        with self._atomic_operation():
            return [_deep(task) for task in self._iter_tasks(kind=kind, state=state)]

    def list_ideas(self, *, state: str | None = None) -> list[Idea]:
        """Return snapshots of ideas matching an optional ``state`` filter."""
        with self._atomic_operation():
            return [_deep(p) for p in self._iter_ideas(state=state)]

    def list_variants(self, *, status: str | None = None) -> list[Variant]:
        """Return snapshots of variants matching an optional ``status`` filter."""
        with self._atomic_operation():
            return [_deep(t) for t in self._iter_variants(status=status)]

    def events(self) -> list[Event]:
        """Return an ordered snapshot of the full event log.

        Every returned event is a deep copy; mutation of the return
        value cannot rewrite log entries. Equivalent to ``replay()``;
        retained as the pre-Phase-6 convenience name.
        """
        return self.replay()

    def replay(self) -> list[Event]:
        """Return every event for this experiment in log order.

        Chapter 8 §2.1 / §4.4. Every returned event is a deep copy.
        """
        with self._atomic_operation():
            return [_deep(e) for e in self._iter_events()]

    def read_range(self, cursor: int | None = None) -> list[Event]:
        """Return events after ``cursor`` in log order (chapter 8 §2.1).

        ``cursor`` is the **cumulative** count of events the caller
        has already consumed — i.e. the total number of events the
        caller has observed, not the size of its last chunk. A
        caller polling in a loop advances ``cursor`` by the length
        of each returned chunk; passing the size of the last chunk
        alone would skip everything the caller has already read past
        that point.

        The reference backends' log order is total (chapter 8 §2.2),
        so indexing by cumulative count is stable. A ``None`` cursor
        (or ``0``) is equivalent to ``replay()``.
        """
        with self._atomic_operation():
            events = [_deep(e) for e in self._iter_events()]
        if cursor is None or cursor <= 0:
            return events
        return events[cursor:]

    # ------------------------------------------------------------------
    # Task lifecycle — creation
    # ------------------------------------------------------------------

    def create_task(self, task: Task) -> Task:
        """Spec-literal ``create_task`` per chapter 8 §1.1.

        Inserts a fully-formed task object in ``state="pending"``.
        For ``execution`` tasks the composite-commit from
        ``05-event-protocol.md`` §2.2 also transitions the referenced
        idea from ``ready`` to ``dispatched``; for ``evaluation``
        tasks the referenced variant's ``starting``/``commit_sha``
        precondition is enforced.

        The typed helpers ``create_ideation_task`` / ``create_execution_task``
        / ``create_evaluation_task`` build the task payload for the
        caller; both paths converge on the same commit.
        """
        if task.state != "pending":
            raise InvalidPrecondition(
                f"new task must be in 'pending' state, not {task.state!r}"
            )
        if task.claim is not None:
            raise InvalidPrecondition(
                "new task must not carry a claim (08-storage.md §1.1)"
            )
        if task.kind == "ideation":
            assert isinstance(task, IdeationTask)
            return self._insert_plan_task(task)
        if task.kind == "execution":
            assert isinstance(task, ExecutionTask)
            return self._insert_implement_task(task)
        assert isinstance(task, EvaluationTask)
        return self._insert_evaluate_task(task)

    def _insert_plan_task(self, task: IdeationTask) -> IdeationTask:
        if task.payload.experiment_id != self._experiment_id:
            raise InvalidPrecondition(
                f"ideation task payload.experiment_id={task.payload.experiment_id!r} "
                f"does not match store experiment {self._experiment_id!r}"
            )
        with self._atomic_operation():
            self._require_no_task(task.task_id)
            tx = _Tx()
            tx.tasks[task.task_id] = _deep(task)
            tx.events.append(
                self._event("task.created", {"task_id": task.task_id, "kind": "ideation"})
            )
            self._apply_commit(tx)
            return _deep(task)

    def _insert_implement_task(self, task: ExecutionTask) -> ExecutionTask:
        with self._atomic_operation():
            self._require_no_task(task.task_id)
            idea_id = task.payload.idea_id
            idea = self._get_idea(idea_id)
            if idea is None:
                raise NotFound(f"idea {idea_id!r}")
            if idea.state != "ready":
                raise InvalidPrecondition(
                    f"idea {idea_id!r} must be 'ready' "
                    f"to dispatch, not {idea.state!r}"
                )
            tx = _Tx()
            tx.tasks[task.task_id] = _deep(task)
            tx.ideas[idea_id] = _validated_update(idea, state="dispatched")
            tx.events.append(
                self._event("task.created", {"task_id": task.task_id, "kind": "execution"})
            )
            tx.events.append(
                self._event(
                    "idea.dispatched",
                    {"idea_id": idea_id, "task_id": task.task_id},
                )
            )
            self._apply_commit(tx)
            return _deep(task)

    def _insert_evaluate_task(self, task: EvaluationTask) -> EvaluationTask:
        with self._atomic_operation():
            self._require_no_task(task.task_id)
            variant_id = task.payload.variant_id
            variant = self._get_variant(variant_id)
            if variant is None:
                raise NotFound(f"variant {variant_id!r}")
            if variant.status != "starting":
                raise InvalidPrecondition(
                    f"variant {variant_id!r} must be 'starting' to dispatch eval, "
                    f"not {variant.status!r}"
                )
            if variant.commit_sha is None:
                raise InvalidPrecondition(
                    f"variant {variant_id!r} has no commit_sha; executor must set it first"
                )
            tx = _Tx()
            tx.tasks[task.task_id] = _deep(task)
            tx.events.append(
                self._event("task.created", {"task_id": task.task_id, "kind": "evaluation"})
            )
            self._apply_commit(tx)
            return _deep(task)

    def create_ideation_task(self, task_id: str) -> IdeationTask:
        """Create an ``ideation`` task. Emits ``task.created`` atomically."""
        with self._atomic_operation():
            self._require_no_task(task_id)
            now = self._ts()
            task = IdeationTask(
                task_id=task_id,
                kind="ideation",
                state="pending",
                payload=IdeationPayload(experiment_id=self._experiment_id),
                created_at=now,
                updated_at=now,
            )
            tx = _Tx()
            tx.tasks[task_id] = task
            tx.events.append(self._event("task.created", {"task_id": task_id, "kind": "ideation"}))
            self._apply_commit(tx)
            return _deep(task)

    def create_execution_task(self, task_id: str, idea_id: str) -> ExecutionTask:
        """Create an ``execution`` task; composite-commits ``idea.dispatched``.

        Per ``05-event-protocol.md`` §2.2: creating the execution task
        and transitioning the referenced idea from ``ready`` to
        ``dispatched`` land in one atomic commit.
        """
        with self._atomic_operation():
            self._require_no_task(task_id)
            idea = self._get_idea(idea_id)
            if idea is None:
                raise NotFound(f"idea {idea_id!r}")
            if idea.state != "ready":
                raise InvalidPrecondition(
                    f"idea {idea_id!r} must be 'ready' "
                    f"to dispatch, not {idea.state!r}"
                )
            now = self._ts()
            task = ExecutionTask(
                task_id=task_id,
                kind="execution",
                state="pending",
                payload=ExecutionPayload(idea_id=idea_id),
                created_at=now,
                updated_at=now,
            )
            tx = _Tx()
            tx.tasks[task_id] = task
            tx.ideas[idea_id] = _validated_update(idea, state="dispatched")
            tx.events.append(
                self._event("task.created", {"task_id": task_id, "kind": "execution"})
            )
            tx.events.append(
                self._event(
                    "idea.dispatched",
                    {"idea_id": idea_id, "task_id": task_id},
                )
            )
            self._apply_commit(tx)
            return _deep(task)

    def create_evaluation_task(self, task_id: str, variant_id: str) -> EvaluationTask:
        """Create an ``evaluation`` task against a starting variant with ``commit_sha``."""
        with self._atomic_operation():
            self._require_no_task(task_id)
            variant = self._get_variant(variant_id)
            if variant is None:
                raise NotFound(f"variant {variant_id!r}")
            if variant.status != "starting":
                raise InvalidPrecondition(
                    f"variant {variant_id!r} must be 'starting' to dispatch eval, "
                    f"not {variant.status!r}"
                )
            if variant.commit_sha is None:
                raise InvalidPrecondition(
                    f"variant {variant_id!r} has no commit_sha; executor must set it first"
                )
            now = self._ts()
            task = EvaluationTask(
                task_id=task_id,
                kind="evaluation",
                state="pending",
                payload=EvaluationPayload(variant_id=variant_id),
                created_at=now,
                updated_at=now,
            )
            tx = _Tx()
            tx.tasks[task_id] = task
            tx.events.append(
                self._event("task.created", {"task_id": task_id, "kind": "evaluation"})
            )
            self._apply_commit(tx)
            return _deep(task)

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
        with self._atomic_operation():
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
            tx.tasks[task_id] = _validated_update(
                task, state="claimed", claim=claim, updated_at=now
            )
            tx.events.append(
                self._event(
                    "task.claimed",
                    {"task_id": task_id, "worker_id": worker_id},
                )
            )
            self._apply_commit(tx)
            return _deep(claim)

    def submit(self, task_id: str, token: str, submission: Submission) -> None:
        """Transition a claimed task to submitted, persisting the result.

        Idempotent per ``04-task-protocol.md`` §4.2: a resubmit with
        the current token and a content-equivalent payload MUST
        succeed without mutating state; an inconsistent resubmit MUST
        be rejected.
        """
        with self._atomic_operation():
            task = self._require_task(task_id)
            if task.state not in {"claimed", "submitted"}:
                raise IllegalTransition(
                    f"cannot submit task in state {task.state!r}"
                )
            if task.claim is None or task.claim.token != token:
                raise WrongToken(f"token does not match current claim on {task_id!r}")
            self._require_submission_kind_matches(task, submission)
            self._validate_submission_ref_binding(task, submission)

            if task.state == "submitted":
                prior = self._get_submission(task_id)
                if prior is None:
                    raise IllegalTransition(
                        f"task {task_id!r} is submitted but has no recorded submission"
                    )
                if not submissions_equivalent(prior, submission):
                    raise ConflictingResubmission(
                        f"resubmit of {task_id!r} disagrees with committed result"
                    )
                return

            now = self._ts()
            tx = _Tx()
            tx.tasks[task_id] = _validated_update(task, state="submitted", updated_at=now)
            tx.submissions[task_id] = copy.deepcopy(submission)
            tx.events.append(self._event("task.submitted", {"task_id": task_id}))
            self._apply_commit(tx)

    def validate_acceptance(self, task_id: str) -> str | None:
        """Return an orchestrator-side reason to reject this submission, or ``None``.

        ``04-task-protocol.md`` §4.3 requires the orchestrator to
        transition a submitted task to ``failed`` when the result
        violates the role's success contract, even if the worker's
        declared status was ``success``. This helper performs that
        validation without mutating state; the driver calls it
        before deciding between ``accept`` and ``reject``.
        """
        with self._atomic_operation():
            return self._validate_acceptance_locked(task_id)

    def _validate_acceptance_locked(self, task_id: str) -> str | None:
        task = self._require_task(task_id)
        if task.state != "submitted":
            return None
        submission = self._get_submission(task_id)
        if submission is None:
            return "no submission recorded"
        if isinstance(submission, IdeaSubmission):
            if submission.status != "success":
                return None
            return self._validate_ideation_acceptance(submission)
        if isinstance(submission, VariantSubmission):
            if submission.status != "success":
                return None
            assert isinstance(task, ExecutionTask)
            return self._validate_execution_acceptance(task, submission)
        if isinstance(submission, EvaluationSubmission):
            if submission.status != "success":
                return None
            assert isinstance(task, EvaluationTask)
            return self._validate_evaluate_acceptance(task, submission)
        return None

    def validate_terminal(self, task_id: str) -> tuple[str, str | None]:
        """Decide how to terminalize a submitted task.

        Returns one of:
          * ``("accept", None)`` — worker-declared success and result
            satisfies the role's success contract.
          * ``("reject_worker", None)`` — worker-declared error/evaluation_error,
            and the recorded result is writable as specified in
            ``03-roles.md`` §2.4/§3.4/§4.4.
          * ``("reject_validation", reason)`` — the orchestrator must
            treat this submission as a validation failure per
            ``04-task-protocol.md`` §4.3, either because the worker
            declared ``success`` with a malformed payload, or because
            the worker declared ``error`` but the accompanying variant
            fields (metrics, artifacts_uri) would not validate.

        The driver calls this before every ``accept`` / ``reject`` and
        uses the returned decision to pick the correct `reason`.
        """
        with self._atomic_operation():
            task = self._require_task(task_id)
            if task.state != "submitted":
                return ("accept", None)
            submission = self._get_submission(task_id)
            if submission is None:
                return ("reject_validation", "no submission recorded")
            if submission.status == "success":
                reason = self._validate_acceptance_locked(task_id)
                if reason is not None:
                    return ("reject_validation", reason)
                return ("accept", None)
            # status is "error" or "evaluation_error" — worker_error is the
            # default, but a malformed payload on `error` still has to
            # be treated as validation_error so no invalid field
            # actually lands on the variant.
            if isinstance(submission, EvaluationSubmission) and submission.status == "error":
                assert isinstance(task, EvaluationTask)
                reason = self._validate_evaluate_error(task, submission)
                if reason is not None:
                    return ("reject_validation", reason)
            return ("reject_worker", None)

    def accept(self, task_id: str) -> None:
        """Orchestrator accept: ``submitted → completed`` with composite effects.

        Dispatches by task kind to emit the right composite-commit
        events (``05-event-protocol.md`` §2.2). Clears the task's
        ``claim``.
        """
        with self._atomic_operation():
            task = self._require_task(task_id)
            if task.state != "submitted":
                raise IllegalTransition(
                    f"cannot accept task in state {task.state!r}"
                )
            submission = self._get_submission(task_id)
            if submission is None:
                raise IllegalTransition(
                    f"task {task_id!r} is submitted but has no recorded submission"
                )
            if task.kind == "ideation":
                self._accept_ideation(task, submission)
            elif task.kind == "execution":
                self._accept_execution(task, submission)
            else:
                self._accept_evaluation(task, submission)

    def reject(self, task_id: str, reason: FailReason) -> None:
        """Orchestrator reject: ``submitted → failed`` with composite effects.

        Dispatches by task kind. For evaluation tasks, the variant-side
        effect depends on whether the worker declared ``error`` or
        ``evaluation_error`` (``04-task-protocol.md`` §4.3).
        """
        with self._atomic_operation():
            task = self._require_task(task_id)
            if task.state != "submitted":
                raise IllegalTransition(
                    f"cannot reject task in state {task.state!r}"
                )
            submission = self._get_submission(task_id)
            if submission is None:
                raise IllegalTransition(
                    f"task {task_id!r} is submitted but has no recorded submission"
                )
            if task.kind == "ideation":
                self._reject_ideation(task, reason)
            elif task.kind == "execution":
                self._reject_execution(task, submission, reason)
            else:
                self._reject_evaluate(task, submission, reason)

    def reclaim(self, task_id: str, cause: ReclaimCause) -> None:
        """Move a claimed (or operator-reclaimed submitted) task back to pending.

        Automatic causes (``expired``, ``health_policy``) are rejected
        against ``submitted`` tasks per ``04-task-protocol.md`` §5.1;
        only ``operator`` reclaim is permitted from ``submitted``. If
        the reclaimed task is an ``execution`` whose prior worker run
        left a variant in ``starting``, the variant transitions to
        ``error`` atomically with the reclaim
        (``05-event-protocol.md`` §2.2).
        """
        with self._atomic_operation():
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
            tx.tasks[task_id] = _validated_update(
                task, state="pending", claim=None, updated_at=now
            )
            if self._get_submission(task_id) is not None:
                tx.task_deletes_submission.add(task_id)
            tx.events.append(
                self._event("task.reclaimed", {"task_id": task_id, "cause": cause})
            )

            if task.kind == "execution":
                variant = self._find_starting_variant_for_implement_task(task)
                if variant is not None:
                    tx.variants[variant.variant_id] = _validated_update(
                        variant, status="error", completed_at=now
                    )
                    tx.events.append(
                        self._event("variant.errored", {"variant_id": variant.variant_id})
                    )

            self._apply_commit(tx)

    # ------------------------------------------------------------------
    # Idea store
    # ------------------------------------------------------------------

    def create_idea(self, idea: Idea) -> None:
        """Persist a new idea in ``drafting``. Emits ``idea.drafted``."""
        with self._atomic_operation():
            if self._get_idea(idea.idea_id) is not None:
                raise AlreadyExists(f"idea {idea.idea_id!r}")
            if idea.experiment_id != self._experiment_id:
                raise InvalidPrecondition(
                    f"idea experiment_id {idea.experiment_id!r} "
                    f"does not match store experiment {self._experiment_id!r}"
                )
            if idea.state != "drafting":
                raise InvalidPrecondition(
                    f"new idea must start in 'drafting', not {idea.state!r}"
                )
            tx = _Tx()
            tx.ideas[idea.idea_id] = _deep(idea)
            tx.events.append(
                self._event("idea.drafted", {"idea_id": idea.idea_id})
            )
            self._apply_commit(tx)

    def mark_idea_ready(self, idea_id: str) -> None:
        """Transition an idea ``drafting → ready``. Emits ``idea.ready``."""
        with self._atomic_operation():
            idea = self._require_idea(idea_id)
            if idea.state != "drafting":
                raise IllegalTransition(
                    f"cannot mark idea ready from state {idea.state!r}"
                )
            tx = _Tx()
            tx.ideas[idea_id] = _validated_update(idea, state="ready")
            tx.events.append(self._event("idea.ready", {"idea_id": idea_id}))
            self._apply_commit(tx)

    # ------------------------------------------------------------------
    # Variant store
    # ------------------------------------------------------------------

    def create_variant(self, variant: Variant) -> None:
        """Persist a new variant in ``starting``. Emits ``variant.started``."""
        with self._atomic_operation():
            if self._get_variant(variant.variant_id) is not None:
                raise AlreadyExists(f"variant {variant.variant_id!r}")
            if variant.status != "starting":
                raise InvalidPrecondition(
                    f"new variant must start in 'starting', not {variant.status!r}"
                )
            if variant.experiment_id != self._experiment_id:
                raise InvalidPrecondition(
                    f"variant experiment_id {variant.experiment_id!r} "
                    f"does not match store experiment {self._experiment_id!r}"
                )
            tx = _Tx()
            tx.variants[variant.variant_id] = _deep(variant)
            tx.events.append(
                self._event(
                    "variant.started",
                    {"variant_id": variant.variant_id, "idea_id": variant.idea_id},
                )
            )
            self._apply_commit(tx)

    def declare_variant_evaluation_error(self, variant_id: str) -> None:
        """Retry-exhausted: ``starting → evaluation_error`` (``05-event-protocol.md`` §2.2).

        Writes ``completed_at`` atomically; MUST NOT set metrics or
        artifacts_uri (``03-roles.md`` §4.4).
        """
        with self._atomic_operation():
            variant = self._require_variant(variant_id)
            if variant.status != "starting":
                raise IllegalTransition(
                    f"cannot declare evaluation_error from variant status {variant.status!r}"
                )
            now = self._ts()
            tx = _Tx()
            tx.variants[variant_id] = _validated_update(
                variant, status="evaluation_error", completed_at=now
            )
            tx.events.append(self._event("variant.evaluation_errored", {"variant_id": variant_id}))
            self._apply_commit(tx)

    def integrate_variant(self, variant_id: str, variant_commit_sha: str) -> None:
        """Integrator integration: write ``variant_commit_sha`` and emit ``variant.integrated``.

        Per ``08-storage.md`` §1.7: ``variant_commit_sha`` is the one
        post-terminal write permitted on a variant; it must be written
        atomically with its event.

        **Same-value idempotency** (``07-wire-protocol.md`` §5): a
        repeated call whose ``variant_commit_sha`` equals the value
        already stored on the variant is a no-op and MUST NOT append a
        second ``variant.integrated`` event. This rule lets an HTTP-
        mediated caller retry a transport-indeterminate
        ``integrate_variant`` request without risking double-commit;
        the same-value branch also keeps direct-``Store`` callers
        and wire-mediated callers on identical contracts.

        A repeated call with a **different** ``variant_commit_sha``
        raises ``InvalidPrecondition`` — the chapter 6 §1.2 sole-
        writer rule has been violated and operator intervention is
        required. The caller (e.g. ``Integrator``) maps this to an
        ``AtomicityViolation`` rather than compensating the ref.
        """
        with self._atomic_operation():
            variant = self._require_variant(variant_id)
            if variant.status != "success":
                raise InvalidPrecondition(
                    f"variant {variant_id!r} must be in 'success' to integrate, "
                    f"not {variant.status!r}"
                )
            if variant.variant_commit_sha is not None:
                if variant.variant_commit_sha == variant_commit_sha:
                    return
                raise InvalidPrecondition(
                    f"variant {variant_id!r} is already integrated with a "
                    f"different variant_commit_sha "
                    f"({variant.variant_commit_sha!r} != {variant_commit_sha!r})"
                )
            tx = _Tx()
            tx.variants[variant_id] = _validated_update(
                variant, variant_commit_sha=variant_commit_sha
            )
            tx.events.append(
                self._event(
                    "variant.integrated",
                    {"variant_id": variant_id, "variant_commit_sha": variant_commit_sha},
                )
            )
            self._apply_commit(tx)

    # ------------------------------------------------------------------
    # Internal dispatch — accept/reject helpers
    # ------------------------------------------------------------------

    def _accept_ideation(self, task: Task, submission: Submission) -> None:
        assert isinstance(submission, IdeaSubmission)
        reason = self._validate_ideation_acceptance(submission)
        if reason is not None:
            raise IllegalTransition(
                f"cannot accept ideate {task.task_id!r}: {reason}"
            )
        now = self._ts()
        tx = _Tx()
        tx.tasks[task.task_id] = _validated_update(
            task, state="completed", claim=None, updated_at=now
        )
        tx.events.append(self._event("task.completed", {"task_id": task.task_id}))
        self._apply_commit(tx)

    def _reject_ideation(self, task: Task, reason: FailReason) -> None:
        now = self._ts()
        tx = _Tx()
        tx.tasks[task.task_id] = _validated_update(
            task, state="failed", claim=None, updated_at=now
        )
        tx.events.append(
            self._event("task.failed", {"task_id": task.task_id, "reason": reason})
        )
        self._apply_commit(tx)

    def _accept_execution(self, task: Task, submission: Submission) -> None:
        assert isinstance(task, ExecutionTask)
        assert isinstance(submission, VariantSubmission)
        reason = self._validate_execution_acceptance(task, submission)
        if reason is not None:
            raise IllegalTransition(
                f"cannot accept execute {task.task_id!r}: {reason}"
            )
        assert submission.commit_sha is not None
        idea = self._require_idea(task.payload.idea_id)
        variant = self._require_variant(submission.variant_id)

        now = self._ts()
        tx = _Tx()
        tx.tasks[task.task_id] = _validated_update(
            task, state="completed", claim=None, updated_at=now
        )
        tx.ideas[task.payload.idea_id] = _validated_update(idea, state="completed")
        tx.variants[variant.variant_id] = _validated_update(
            variant, commit_sha=submission.commit_sha
        )
        tx.events.append(self._event("task.completed", {"task_id": task.task_id}))
        tx.events.append(
            self._event(
                "idea.completed",
                {"idea_id": task.payload.idea_id, "task_id": task.task_id},
            )
        )
        self._apply_commit(tx)

    def _reject_execution(
        self,
        task: Task,
        submission: Submission,
        reason: FailReason,
    ) -> None:
        assert isinstance(task, ExecutionTask)
        idea = self._require_idea(task.payload.idea_id)
        variant_for_error: Variant | None = None
        if isinstance(submission, VariantSubmission):
            # Only touch the variant if it was created under this very
            # task's idea — submission.variant_id is caller-supplied
            # and could reference an unrelated variant.
            variant = self._get_variant(submission.variant_id)
            if (
                variant is not None
                and variant.status == "starting"
                and variant.idea_id == task.payload.idea_id
            ):
                variant_for_error = variant

        now = self._ts()
        tx = _Tx()
        tx.tasks[task.task_id] = _validated_update(
            task, state="failed", claim=None, updated_at=now
        )
        tx.ideas[task.payload.idea_id] = _validated_update(idea, state="completed")
        tx.events.append(
            self._event("task.failed", {"task_id": task.task_id, "reason": reason})
        )
        tx.events.append(
            self._event(
                "idea.completed",
                {"idea_id": task.payload.idea_id, "task_id": task.task_id},
            )
        )
        if variant_for_error is not None:
            tx.variants[variant_for_error.variant_id] = _validated_update(
                variant_for_error, status="error", completed_at=now
            )
            tx.events.append(
                self._event("variant.errored", {"variant_id": variant_for_error.variant_id})
            )
        self._apply_commit(tx)

    def _accept_evaluation(self, task: Task, submission: Submission) -> None:
        assert isinstance(task, EvaluationTask)
        assert isinstance(submission, EvaluationSubmission)
        reason = self._validate_evaluate_acceptance(task, submission)
        if reason is not None:
            raise IllegalTransition(
                f"cannot accept evaluate {task.task_id!r}: {reason}"
            )
        variant = self._require_variant(submission.variant_id)

        now = self._ts()
        tx = _Tx()
        tx.tasks[task.task_id] = _validated_update(
            task, state="completed", claim=None, updated_at=now
        )
        tx.variants[variant.variant_id] = _validated_update(
            variant,
            status="success",
            evaluation=dict(submission.evaluation) if submission.evaluation else None,
            artifacts_uri=submission.artifacts_uri,
            completed_at=now,
        )
        tx.events.append(self._event("task.completed", {"task_id": task.task_id}))
        tx.events.append(
            self._event(
                "variant.succeeded",
                {"variant_id": variant.variant_id, "commit_sha": variant.commit_sha},
            )
        )
        self._apply_commit(tx)

    def _reject_evaluate(
        self,
        task: Task,
        submission: Submission,
        reason: FailReason,
    ) -> None:
        assert isinstance(task, EvaluationTask)
        assert isinstance(submission, EvaluationSubmission)
        # submission.variant_id is already bound to task.payload.variant_id
        # by _validate_submission_ref_binding at submit time.
        variant = self._require_variant(task.payload.variant_id)

        now = self._ts()
        tx = _Tx()
        tx.tasks[task.task_id] = _validated_update(
            task, state="failed", claim=None, updated_at=now
        )
        tx.events.append(
            self._event("task.failed", {"task_id": task.task_id, "reason": reason})
        )

        # Variant-side effect depends on the submission status, not on
        # the orchestrator's reason (03-roles.md §4.4):
        #   • evaluation_error — variant stays in starting; evaluator-side
        #     failure does not condemn the variant.
        #   • success — reject can only happen via validation_error
        #     (malformed success). Variant stays in starting: the
        #     evaluator has not produced a verdict either way.
        #   • error — variant MUST transition to error, because the
        #     worker declared variant failure. If the payload is
        #     malformed (reason=validation_error), drop the invalid
        #     metrics/artifacts_uri but still record variant.errored.
        if submission.status == "error":
            if variant.status != "starting":
                raise IllegalTransition(
                    f"cannot error variant {variant.variant_id!r} from status {variant.status!r}"
                )
            update_kwargs: dict[str, Any] = {"status": "error", "completed_at": now}
            if reason != "validation_error":
                if submission.evaluation is not None:
                    self._validate_evaluation(submission.evaluation)
                    update_kwargs["evaluation"] = dict(submission.evaluation)
                if submission.artifacts_uri is not None:
                    update_kwargs["artifacts_uri"] = submission.artifacts_uri
            tx.variants[variant.variant_id] = _validated_update(variant, **update_kwargs)
            tx.events.append(self._event("variant.errored", {"variant_id": variant.variant_id}))
        # status in {"evaluation_error", "success"} — no variant-side writes.

        self._apply_commit(tx)

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------

    def _validate_submission_ref_binding(
        self, task: Task, submission: Submission
    ) -> None:
        """Reject a submission whose referenced IDs don't match the task.

        Per ``04-task-protocol.md`` §4.1, the submission's result
        payload is scoped to the task it is being submitted against.
        An ideate-task submission's idea_ids must reference existing
        ideas (03-roles §2.4); an execute-task submission's
        variant_id must refer to a variant under the task's idea
        (03-roles §3.4); an evaluate-task submission's variant_id must
        equal the task payload's variant_id (03-roles §4.4).
        """
        if isinstance(submission, IdeaSubmission):
            for pid in submission.idea_ids:
                idea = self._get_idea(pid)
                if idea is None:
                    raise IllegalTransition(
                        f"ideate-task submission references unknown idea {pid!r}"
                    )
                if idea.state == "drafting":
                    raise IllegalTransition(
                        f"ideate-task submission references drafting idea {pid!r}; "
                        "ideator MUST NOT submit while ideas are in drafting "
                        "(03-roles.md §2.4)"
                    )
        elif isinstance(submission, VariantSubmission):
            assert isinstance(task, ExecutionTask)
            variant = self._get_variant(submission.variant_id)
            if variant is None:
                raise IllegalTransition(
                    f"execute-task submission references unknown variant "
                    f"{submission.variant_id!r}"
                )
            if variant.idea_id != task.payload.idea_id:
                raise IllegalTransition(
                    f"execute-task submission variant_id={submission.variant_id!r} "
                    f"belongs to idea {variant.idea_id!r}, not the "
                    f"task's idea {task.payload.idea_id!r}"
                )
        elif isinstance(submission, EvaluationSubmission):
            assert isinstance(task, EvaluationTask)
            if submission.variant_id != task.payload.variant_id:
                raise IllegalTransition(
                    f"evaluate-task submission variant_id={submission.variant_id!r} "
                    f"does not match task's variant {task.payload.variant_id!r}"
                )

    def _validate_ideation_acceptance(self, submission: IdeaSubmission) -> str | None:
        for pid in submission.idea_ids:
            idea = self._get_idea(pid)
            if idea is None:
                return f"idea {pid!r} no longer exists"
            if idea.state == "drafting":
                return f"idea {pid!r} is still in drafting at accept time"
        return None

    def _validate_execution_acceptance(
        self, task: ExecutionTask, submission: VariantSubmission
    ) -> str | None:
        if submission.commit_sha is None:
            return "success submission requires commit_sha (03-roles.md §3.4)"
        variant = self._get_variant(submission.variant_id)
        if variant is None:
            return f"referenced variant {submission.variant_id!r} does not exist"
        if variant.idea_id != task.payload.idea_id:
            return (
                f"referenced variant {submission.variant_id!r} belongs to a "
                "different idea"
            )
        if variant.status != "starting":
            return (
                f"referenced variant {submission.variant_id!r} is "
                f"{variant.status!r}, not 'starting'"
            )
        # Dry-run the variant write so an invalid commit_sha pattern
        # surfaces as validation_error instead of crashing accept.
        try:
            _validated_update(variant, commit_sha=submission.commit_sha)
        except ValidationError as exc:
            return f"invalid commit_sha: {exc.errors()[0]['msg']}"
        return None

    def _validate_evaluate_acceptance(
        self, task: EvaluationTask, submission: EvaluationSubmission
    ) -> str | None:
        if submission.variant_id != task.payload.variant_id:
            return "submission variant_id does not match task's variant_id"
        variant = self._get_variant(submission.variant_id)
        if variant is None:
            return f"variant {submission.variant_id!r} does not exist"
        if variant.status != "starting":
            return f"variant {submission.variant_id!r} is {variant.status!r}, not 'starting'"
        if variant.commit_sha is None:
            return f"variant {submission.variant_id!r} has no commit_sha"
        if submission.evaluation is None:
            return "success submission requires metrics (03-roles.md §4.4)"
        try:
            self._validate_evaluation(submission.evaluation)
        except InvalidPrecondition as exc:
            return str(exc)
        # Dry-run the variant write so an invalid artifacts_uri (or
        # any other field) surfaces as validation_error instead of
        # crashing accept.
        try:
            _validated_update(
                variant,
                status="success",
                evaluation=dict(submission.evaluation),
                artifacts_uri=submission.artifacts_uri,
                completed_at=self._ts(),
            )
        except ValidationError as exc:
            return f"invalid variant update: {exc.errors()[0]['msg']}"
        return None

    def _validate_evaluate_error(
        self, task: EvaluationTask, submission: EvaluationSubmission
    ) -> str | None:
        """Validate fields that a `status=error` evaluate-task submission would write."""
        if submission.variant_id != task.payload.variant_id:
            return "submission variant_id does not match task's variant_id"
        variant = self._get_variant(submission.variant_id)
        if variant is None:
            return f"variant {submission.variant_id!r} does not exist"
        if submission.evaluation is not None:
            try:
                self._validate_evaluation(submission.evaluation)
            except InvalidPrecondition as exc:
                return str(exc)
        try:
            _validated_update(
                variant,
                status="error",
                evaluation=dict(submission.evaluation) if submission.evaluation else None,
                artifacts_uri=submission.artifacts_uri,
                completed_at=self._ts(),
            )
        except ValidationError as exc:
            return f"invalid variant update: {exc.errors()[0]['msg']}"
        return None

    def validate_evaluation(self, evaluation: dict[str, Any]) -> None:
        """Validate evaluation against the registered schema.

        Public entry point for both submit-time (``08-storage.md`` §4)
        and integration-time (``06-integrator.md`` §2) validation.
        Raises ``InvalidPrecondition`` on violation; no-op when the
        store was constructed without an ``evaluation_schema``.
        """
        self._validate_evaluation(evaluation)

    def _validate_evaluation(self, evaluation: dict[str, Any]) -> None:
        """Validate evaluation against the registered schema (``08-storage.md`` §4)."""
        if self._evaluation_schema is None:
            return
        schema = self._evaluation_schema.root
        for key, value in evaluation.items():
            if key not in schema:
                raise InvalidPrecondition(
                    f"evaluation key {key!r} is not in the experiment's evaluation_schema"
                )
            if value is None:
                continue
            mtype = schema[key]
            # Reject bools for integer/real per spec §1.3 (bool is a separate domain).
            if isinstance(value, bool):
                raise InvalidPrecondition(
                    f"evaluation key {key!r} is bool; declared type is {mtype!r}"
                )
            expected = _METRIC_PY_TYPES[mtype]
            if not isinstance(value, expected):
                raise InvalidPrecondition(
                    f"evaluation key {key!r} value {value!r} is not of declared type {mtype!r}"
                )
            # Non-finite floats (NaN, +inf, -inf) fail JSON round-trip
            # and can't be stored in the event log or evaluation manifest. The
            # ``real`` type in the evaluation schema implies "finite IEEE
            # 754 double" per the spec's JSON grounding.
            if mtype == "real" and not math.isfinite(value):
                raise InvalidPrecondition(
                    f"evaluation key {key!r} value {value!r} is not finite"
                )

    # ------------------------------------------------------------------
    # Require-or-raise helpers
    # ------------------------------------------------------------------

    def _find_starting_variant_for_implement_task(self, task: Task) -> Variant | None:
        assert isinstance(task, ExecutionTask)
        for variant in self._iter_variants():
            if (
                variant.idea_id == task.payload.idea_id
                and variant.status == "starting"
            ):
                return variant
        return None

    def _require_submission_kind_matches(self, task: Task, submission: Submission) -> None:
        expected: type[Submission]
        if task.kind == "ideation":
            expected = IdeaSubmission
        elif task.kind == "execution":
            expected = VariantSubmission
        else:
            expected = EvaluationSubmission
        if not isinstance(submission, expected):
            raise IllegalTransition(
                f"task {task.task_id!r} (kind={task.kind!r}) requires "
                f"{expected.__name__}, got {type(submission).__name__}"
            )

    def _require_no_task(self, task_id: str) -> None:
        if self._get_task(task_id) is not None:
            raise AlreadyExists(f"task {task_id!r}")

    def _require_task(self, task_id: str) -> Task:
        task = self._get_task(task_id)
        if task is None:
            raise NotFound(f"task {task_id!r}")
        return task

    def _require_idea(self, idea_id: str) -> Idea:
        idea = self._get_idea(idea_id)
        if idea is None:
            raise NotFound(f"idea {idea_id!r}")
        return idea

    def _require_variant(self, variant_id: str) -> Variant:
        variant = self._get_variant(variant_id)
        if variant is None:
            raise NotFound(f"variant {variant_id!r}")
        return variant

    # ------------------------------------------------------------------
    # Event + timestamp helpers
    # ------------------------------------------------------------------

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


def iter_events_by_type(events: Iterable[Event], type_: str) -> Iterator[Event]:
    """Yield events whose ``type`` equals ``type_``. Convenience for tests."""
    for event in events:
        if event.type == type_:
            yield event
