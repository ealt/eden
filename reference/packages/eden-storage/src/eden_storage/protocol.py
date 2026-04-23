"""The ``Store`` Protocol — structural interface of a conforming EDEN store.

This module pins the task-side stores named in chapter 8
([`spec/v0/08-storage.md`](../../../../spec/v0/08-storage.md)) — task
store (§1), event log reads (§2.1 ``replay`` + ``read_range``), and
proposal/trial persistence (§1.7) — as a single Python `Protocol`.
A deployment MAY split the physical layout any way it likes (§7);
the Protocol constrains the observable contract.

**Out of scope at Phase 6:**

- The artifact store (§5) is deferred to Phase 10 when blob storage
  lands alongside the Compose stack.
- The streaming ``subscribe`` operation (§2.1) is deferred to Phase 8
  when the cross-process wire protocol exposes it over a transport
  that can actually stream; Phase 6's in-process / SQLite backends
  implement the pull-based ``replay`` + ``read_range`` siblings
  instead.

Why a single Protocol and not three:

- Chapter 8 §7 explicitly permits a single backend to serve all three
  stores, and the transactional invariant (§6.1, composite commits)
  is most naturally expressed when a caller talks to *one* object that
  atomically applies state changes and their events together. Two
  separate objects with an external commit coordinator would add
  complexity with no normative benefit.
- Every composite commit in
  [`spec/v0/05-event-protocol.md`](../../../../spec/v0/05-event-protocol.md)
  §2.2 spans more than one store (task + proposal, task + trial, task
  + trial, etc.); a Protocol that straddles those stores is the
  smallest surface that can express "these writes land together."

The Protocol is structural (`runtime_checkable=False`): a conforming
backend does not subclass it; matching the method signatures is
enough. Two reference backends satisfy it — ``InMemoryStore`` and
``SqliteStore`` — and conformance tests parametrize across both.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from eden_contracts import (
    EvaluateTask,
    Event,
    FailReason,
    ImplementTask,
    PlanTask,
    Proposal,
    ReclaimCause,
    Task,
    TaskClaim,
    Trial,
)

from .submissions import Submission

__all__ = ["Store"]


class Store(Protocol):
    """Union of task store, event log, and proposal/trial persistence.

    Every method is an atomic operation under chapter 8 §6: the
    state change it describes, any composite effects
    (``05-event-protocol.md`` §2.2), and the event append(s) all
    land together or not at all.

    A conforming backend MUST raise the error types from
    [`errors.py`](errors.py) on rejection:

    - ``NotFound`` — referenced entity does not exist.
    - ``AlreadyExists`` — insert collided with an existing id.
    - ``IllegalTransition`` — state transition not in the machine.
    - ``WrongToken`` — presented token ≠ current claim token.
    - ``ConflictingResubmission`` — resubmit disagreed with committed
      result.
    - ``InvalidPrecondition`` — referenced entity not in required
      state.

    The Protocol does not cover constructor signatures: backends differ
    in their constructor (an in-memory store takes no URL; SQLite takes
    a path). Callers should treat construction as backend-specific.
    """

    @property
    def experiment_id(self) -> str:
        """The experiment this store is scoped to."""
        ...

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def read_task(self, task_id: str) -> Task:
        """Return the current task object, or raise ``NotFound``."""
        ...

    def read_proposal(self, proposal_id: str) -> Proposal:
        """Return the current proposal, or raise ``NotFound``."""
        ...

    def read_trial(self, trial_id: str) -> Trial:
        """Return the current trial, or raise ``NotFound``."""
        ...

    def read_submission(self, task_id: str) -> Submission | None:
        """Return the committed submission for a task, or ``None``."""
        ...

    def list_tasks(
        self,
        *,
        kind: str | None = None,
        state: str | None = None,
    ) -> list[Task]:
        """Return tasks matching the optional ``kind`` / ``state`` filter.

        Ordering is implementation-defined per chapter 8 §1.1.
        """
        ...

    def list_proposals(self, *, state: str | None = None) -> list[Proposal]:
        """Return proposals matching the optional ``state`` filter."""
        ...

    def list_trials(self, *, status: str | None = None) -> list[Trial]:
        """Return trials matching the optional ``status`` filter."""
        ...

    def events(self) -> list[Event]:
        """Return an ordered snapshot of the event log.

        Backed by the log's total order per chapter 8 §2.2. Equivalent
        to ``replay()``; retained as the pre-Phase-6 convenience name.
        """
        ...

    def replay(self) -> list[Event]:
        """Return every event for this experiment in log order.

        Chapter 8 §2.1 / §4.4: the event log MUST serve full replay
        from the first event for the experiment's lifetime.
        """
        ...

    def read_range(self, cursor: int | None = None) -> list[Event]:
        """Return events after ``cursor`` in log order (chapter 8 §2.1).

        ``cursor`` is the **cumulative** count of events the caller
        has already observed — not the size of the last chunk. A
        caller polling in a loop advances ``cursor`` by the length
        of each returned chunk; passing the last chunk's size alone
        would skip past events already read. The reference backends'
        log order is total (chapter 8 §2.2), so indexing by
        cumulative count is stable. A ``None`` (or ``0``) cursor is
        equivalent to ``replay()``.
        """
        ...

    # ------------------------------------------------------------------
    # Task lifecycle — creation
    # ------------------------------------------------------------------

    def create_task(self, task: Task) -> Task:
        """Atomically insert a fully-formed task + ``task.created`` event.

        Chapter 8 §1.1: accepts a task object with ``state == "pending"``
        and an empty ``claim``. For ``implement`` tasks this is a
        composite commit that also transitions the referenced proposal
        ``ready → dispatched`` (``05-event-protocol.md`` §2.2).

        The typed helpers below (``create_plan_task``,
        ``create_implement_task``, ``create_evaluate_task``) are
        convenience constructors that build the task payload for the
        caller; they both route through the same commit path as
        ``create_task``.
        """
        ...

    def create_plan_task(self, task_id: str) -> PlanTask:
        """Atomically insert a ``plan`` task + ``task.created`` event."""
        ...

    def create_implement_task(self, task_id: str, proposal_id: str) -> ImplementTask:
        """Create an ``implement`` task; composite-commits ``proposal.dispatched``."""
        ...

    def create_evaluate_task(self, task_id: str, trial_id: str) -> EvaluateTask:
        """Create an ``evaluate`` task against a starting trial with commit_sha."""
        ...

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
        """Atomically transition ``pending → claimed``; issue a token."""
        ...

    def submit(self, task_id: str, token: str, submission: Submission) -> None:
        """Atomically transition ``claimed → submitted``.

        Idempotent per ``04-task-protocol.md`` §4.2.
        """
        ...

    def accept(self, task_id: str) -> None:
        """Atomically transition ``submitted → completed`` with composite effects."""
        ...

    def reject(self, task_id: str, reason: FailReason) -> None:
        """Atomically transition ``submitted → failed`` with composite effects."""
        ...

    def reclaim(self, task_id: str, cause: ReclaimCause) -> None:
        """Move ``claimed`` (or operator-reclaimed ``submitted``) back to ``pending``."""
        ...

    def validate_acceptance(self, task_id: str) -> str | None:
        """Return a validation-error reason for the current submission, or ``None``."""
        ...

    def validate_terminal(self, task_id: str) -> tuple[str, str | None]:
        """Decide how to terminalize: ``accept``, ``reject_worker``, or ``reject_validation``."""
        ...

    # ------------------------------------------------------------------
    # Proposal store
    # ------------------------------------------------------------------

    def create_proposal(self, proposal: Proposal) -> None:
        """Persist a new ``drafting`` proposal; emits ``proposal.drafted``."""
        ...

    def mark_proposal_ready(self, proposal_id: str) -> None:
        """Atomically transition ``drafting → ready``; emits ``proposal.ready``."""
        ...

    # ------------------------------------------------------------------
    # Trial store
    # ------------------------------------------------------------------

    def create_trial(self, trial: Trial) -> None:
        """Persist a new ``starting`` trial; emits ``trial.started``."""
        ...

    def declare_trial_eval_error(self, trial_id: str) -> None:
        """Retry-exhausted terminal ``starting → eval_error`` (``05-event-protocol.md`` §2.2)."""
        ...

    def integrate_trial(self, trial_id: str, trial_commit_sha: str) -> None:
        """Integrator promotion: write ``trial_commit_sha``; emits ``trial.integrated``."""
        ...
