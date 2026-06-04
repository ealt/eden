"""The ``Store`` Protocol — structural interface of a conforming EDEN store.

This module pins the task-side stores named in chapter 8
([`spec/v0/08-storage.md`](../../../../spec/v0/08-storage.md)) — task
store (§1), event log reads (§2.1 ``replay`` + ``read_range``), and
idea/variant persistence (§1.7) — as a single Python `Protocol`.
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
  §2.2 spans more than one store (task + idea, task + variant, task
  + variant, etc.); a Protocol that straddles those stores is the
  smallest surface that can express "these writes land together."

The Protocol is structural (`runtime_checkable=False`): a conforming
backend does not subclass it; matching the method signatures is
enough. Two reference backends satisfy it — ``InMemoryStore`` and
``SqliteStore`` — and conformance tests parametrize across both.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, Protocol

from eden_contracts import (
    DispatchMode,
    EvaluationTask,
    Event,
    ExecutionTask,
    Experiment,
    ExperimentState,
    FailReason,
    Group,
    Idea,
    IdeationTask,
    ReclaimCause,
    Task,
    TaskClaim,
    TaskTarget,
    Variant,
    Worker,
)

from .submissions import Submission

if TYPE_CHECKING:
    from eden_checkpoint import CheckpointManifest, ExporterInfo

    from ._checkpoint import ImportResult

__all__ = ["Store"]


class Store(Protocol):
    """Union of task store, event log, and idea/variant persistence.

    Every method is an atomic operation under chapter 8 §6: the
    state change it describes, any composite effects
    (``05-event-protocol.md`` §2.2), and the event append(s) all
    land together or not at all.

    A conforming backend MUST raise the error types from
    [`errors.py`](errors.py) on rejection:

    - ``NotFound`` — referenced entity does not exist.
    - ``AlreadyExists`` — insert collided with an existing id.
    - ``IllegalTransition`` — state transition not in the machine.
    - ``WrongClaimant`` — submit's ``worker_id`` ≠ ``task.claim.worker_id``
      (atomic claim-match per
      [`spec/v0/04-task-protocol.md`](../../../../spec/v0/04-task-protocol.md)
      §4.1).
    - ``NotClaimed`` — submit against a task whose ``claim`` has been
      cleared (reclaim or terminal).
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

    def read_idea(self, idea_id: str) -> Idea:
        """Return the current idea, or raise ``NotFound``."""
        ...

    def read_variant(self, variant_id: str) -> Variant:
        """Return the current variant, or raise ``NotFound``."""
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

    def list_ideas(self, *, state: str | None = None) -> list[Idea]:
        """Return ideas matching the optional ``state`` filter."""
        ...

    def list_variants(self, *, status: str | None = None) -> list[Variant]:
        """Return variants matching the optional ``status`` filter."""
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
        and an empty ``claim``. For ``execution`` tasks this is a
        composite commit that also transitions the referenced idea
        ``ready → dispatched`` (``05-event-protocol.md`` §2.2).

        The typed helpers below (``create_ideation_task``,
        ``create_execution_task``, ``create_evaluation_task``) are
        convenience constructors that build the task payload for the
        caller; they both route through the same commit path as
        ``create_task``.
        """
        ...

    def create_ideation_task(self, task_id: str) -> IdeationTask:
        """Atomically insert an ``ideation`` task + ``task.created`` event."""
        ...

    def create_execution_task(
        self,
        task_id: str,
        idea_id: str,
        *,
        target: TaskTarget | None = None,
    ) -> ExecutionTask:
        """Create an ``execution`` task; composite-commits ``idea.dispatched``.

        12a-3: ``target`` is the optional admin-supplied override on
        the resulting ``task.target``. When omitted, the task inherits
        the referenced idea's ``intended_executor``; an explicit
        ``target`` wins over the idea's hint per
        [`spec/v0/03-roles.md`](../../../../spec/v0/03-roles.md) §6.5.
        """
        ...

    def create_evaluation_task(
        self,
        task_id: str,
        variant_id: str,
        *,
        target: TaskTarget | None = None,
    ) -> EvaluationTask:
        """Create an ``evaluation`` task against a starting variant with commit_sha.

        Issue #165: ``target`` is the optional admin-supplied override
        on the resulting ``task.target``. When omitted, the task
        inherits the originating idea's ``intended_evaluator``
        (resolved via ``variant.idea_id``); an explicit ``target`` wins
        over the idea's hint per
        [`spec/v0/03-roles.md`](../../../../spec/v0/03-roles.md) §6.5.
        """
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
        """Atomically transition ``pending → claimed``.

        Records ``worker_id`` as the claim owner per chapter 04 §3.2;
        the §3.5 ladder (state / registration / target eligibility)
        runs atomically with the claim write. The pre-12a-1 per-claim
        opaque token has been removed — claim ownership is
        identity-keyed and authentication is the binding's job.
        """
        ...

    def submit(
        self, task_id: str, worker_id: str, submission: Submission
    ) -> None:
        """Atomically transition ``claimed → submitted``.

        ``worker_id`` is the claimant on whose behalf the submit is
        being made (typically supplied by the binding from the
        authenticated identity per
        [`spec/v0/04-task-protocol.md`](../../../../spec/v0/04-task-protocol.md)
        §3.3 / §4.1). The Store atomically checks
        ``task.claim.worker_id == worker_id`` and writes
        ``task.submitted_by = worker_id`` as part of the same
        transition. Idempotent per §4.2.
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

    def reassign_task(
        self,
        task_id: str,
        new_target: TaskTarget | None,
        *,
        reason: str,
        reassigned_by: str,
    ) -> Task:
        """Atomically update a task's ``target`` (12a-2).

        Per [`spec/v0/04-task-protocol.md`](../../../../spec/v0/04-task-protocol.md)
        §6:

        - ``pending`` — just update the target; emit ``task.reassigned``.
        - ``claimed`` — composite-commit (``05-event-protocol.md`` §2.2):
          atomically clear the claim (``task.reclaimed`` with
          ``cause="operator"``) AND update the target AND emit
          ``task.reassigned``. The two events appear in the same
          ``read_range`` slice; no intermediate state is observable.
        - ``submitted`` / ``completed`` / ``failed`` — raise
          ``InvalidPrecondition``; reassign is not permitted on a task
          past the claimed phase.

        Authority enforcement (caller in ``admins``) is the binding's
        responsibility; the Store trusts ``reassigned_by`` as data.
        Returns the post-update task. ``StoreClient`` ships this as a
        wave-3 stub that raises ``NotImplementedError`` until the wire
        endpoint lands.
        """
        ...

    def read_experiment(self) -> Experiment:
        """Return the experiment runtime object (state + created_at).

        Per [`spec/v0/02-data-model.md`](../../../../spec/v0/02-data-model.md)
        §2.5. Distinct from the declarative ``experiment-config`` —
        this object carries only observed runtime state.
        """
        ...

    def read_experiment_state(self) -> ExperimentState:
        """Return the experiment's current lifecycle state.

        Convenience shorthand for ``self.read_experiment().state``.
        """
        ...

    def update_experiment_state(self, new_state: ExperimentState) -> Experiment:
        """Internal primitive: atomically update the experiment lifecycle state.

        Not a public wire op in v0 (per
        [`spec/v0/04-task-protocol.md`](../../../../spec/v0/04-task-protocol.md)
        §8.3). Used by :meth:`terminate_experiment` and the
        orchestrator's policy-driven termination branch. v0 defines
        exactly one legal transition (``"running" → "terminated"``).
        """
        ...

    def terminate_experiment(
        self, *, reason: str, terminated_by: str
    ) -> Experiment:
        """Atomically commit the ``running → terminated`` lifecycle transition.

        Per [`spec/v0/04-task-protocol.md`](../../../../spec/v0/04-task-protocol.md)
        §8.1: the state field update and the ``experiment.terminated``
        event are a single transaction. Idempotent on the terminated
        state — a second call returns success without committing a
        second transition and without appending a second event; the
        winning caller's ``reason`` is the one recorded.
        """
        ...

    def emit_policy_error(
        self,
        *,
        policy_kind: str,
        error_type: str,
        error_message: str,
    ) -> None:
        """Append an ``experiment.policy_error`` event (12a-3).

        Per [`spec/v0/03-roles.md`](../../../../spec/v0/03-roles.md)
        §6.2 decision-type 0 fault-tolerance: when an orchestrator
        policy callable raises, the orchestrator MUST emit a
        registered ``experiment.policy_error`` event so operators see
        the failure in the admin event log. The event is registered
        but EXEMPT from the
        [`spec/v0/05-event-protocol.md`](../../../../spec/v0/05-event-protocol.md)
        §2 transactional invariant — there is no protocol-owned
        state mutation paired with it; this is a single-event append
        rather than a composite commit.

        v0 defines only ``policy_kind == "termination"``; future
        decision types that introduce policy callables MAY add new
        values.
        """
        ...

    def read_dispatch_mode(self) -> DispatchMode:
        """Return the experiment's current dispatch_mode (12a-2).

        Every key defaults to ``"auto"`` on a freshly-initialized
        experiment ([`spec/v0/02-data-model.md`](../../../../spec/v0/02-data-model.md)
        §2.5). The four normative keys are
        ``ideation_creation`` / ``execution_dispatch`` /
        ``evaluation_dispatch`` / ``integration``.
        """
        ...

    def update_dispatch_mode(
        self,
        updates: DispatchMode | dict[str, str],
        *,
        updated_by: str,
    ) -> DispatchMode:
        """Atomically merge ``updates`` into the experiment's dispatch_mode.

        Partial-merge semantics: omitted keys are preserved at their
        prior value. Emits exactly one
        ``experiment.dispatch_mode_changed`` event carrying the new
        full state plus the ``changed`` diff
        ([`spec/v0/04-task-protocol.md`](../../../../spec/v0/04-task-protocol.md)
        §7, [`spec/v0/05-event-protocol.md`](../../../../spec/v0/05-event-protocol.md)
        §3.4). When ``updates`` is empty OR every key already holds
        the requested value, NO event fires — flips with no diff are
        not observable, matching the spec's "the event records a
        change" wording.

        Authority enforcement is the binding's responsibility; the
        Store trusts ``updated_by`` as data.
        """
        ...

    def validate_acceptance(self, task_id: str) -> str | None:
        """Return a validation-error reason for the current submission, or ``None``."""
        ...

    def validate_terminal(self, task_id: str) -> tuple[str, str | None]:
        """Decide how to terminalize: ``accept``, ``reject_worker``, or ``reject_validation``."""
        ...

    # ------------------------------------------------------------------
    # Idea store
    # ------------------------------------------------------------------

    def create_idea(self, idea: Idea) -> None:
        """Persist a new ``drafting`` idea; emits ``idea.drafted``."""
        ...

    def mark_idea_ready(self, idea_id: str) -> None:
        """Atomically transition ``drafting → ready``; emits ``idea.ready``."""
        ...

    # ------------------------------------------------------------------
    # Variant store
    # ------------------------------------------------------------------

    def create_variant(self, variant: Variant) -> None:
        """Persist a new ``starting`` variant; emits ``variant.started``."""
        ...

    def declare_variant_evaluation_error(self, variant_id: str) -> None:
        """Retry-exhausted: ``starting → evaluation_error`` (``05-event-protocol.md`` §2.2)."""
        ...

    def integrate_variant(self, variant_id: str, variant_commit_sha: str) -> None:
        """Integrator integration: write ``variant_commit_sha``; emits ``variant.integrated``."""
        ...

    # ------------------------------------------------------------------
    # Shared validators
    # ------------------------------------------------------------------

    def validate_evaluation(self, evaluation: dict[str, Any]) -> None:
        """Validate metrics against the experiment's ``evaluation_schema``.

        Chapter 8 §4 (submit-time) and chapter 6 §2 (integration-time)
        both depend on this guard. Raises ``InvalidPrecondition`` on
        violation; no-op when the store has no registered schema.
        """
        ...

    # ------------------------------------------------------------------
    # Worker registry (12a-1)
    # ------------------------------------------------------------------

    def register_worker(
        self,
        name: str | None = None,
        *,
        labels: dict[str, str] | None = None,
        registered_by: str | None = None,
    ) -> tuple[Worker, str | None]:
        """Mint a worker; return ``(worker, registration_token)`` (issue #128).

        Mints an opaque ``worker_id`` (``wkr_<ulid>``) and takes an
        optional operator-supplied display ``name``. The token is ALWAYS
        a freshly-minted plaintext credential — there is no id-based
        idempotency, so every call mints a new row + credential even
        when ``name`` collides. Raises ``ReservedIdentifier`` for a
        reserved worker name (``admin`` / ``system`` / ``internal``) and
        ``InvalidName`` for a name that violates the display-name grammar
        (`02-data-model.md` §1.7).
        """
        ...

    def reissue_credential(self, worker_id: str) -> str:
        """Mint a fresh credential; invalidates the prior one. Returns plaintext token."""
        ...

    def verify_worker_credential(
        self, worker_id: str, registration_token: str
    ) -> bool:
        """Return whether the presented token is the worker's current credential."""
        ...

    def read_worker(self, worker_id: str) -> Worker:
        """Return the wire-visible Worker shape, or raise ``NotFound``."""
        ...

    def list_workers(self, name: str | None = None) -> list[Worker]:
        """Return registered workers sorted by ``worker_id``.

        When ``name`` is supplied, returns only workers whose display
        ``name`` matches exactly (case-sensitive) — 0..N matches;
        ``name=None`` returns all (issue #128).
        """
        ...

    # ------------------------------------------------------------------
    # Group registry (12a-1)
    # ------------------------------------------------------------------

    def register_group(
        self,
        name: str | None = None,
        *,
        members: Iterable[str] | None = None,
        created_by: str | None = None,
        allow_reserved: bool = False,
    ) -> Group:
        """Mint a group; raises ``CycleDetected`` on cycle (issue #128).

        Mints an opaque ``group_id`` (``grp_<ulid>``) and takes an
        optional display ``name``. Members MUST be opaque ``wkr_*`` /
        ``grp_*`` ids. Reserved group names (``admins`` /
        ``orchestrators``) raise ``ReservedIdentifier`` unless
        ``allow_reserved=True`` (the privileged setup-experiment seed
        path). Ill-formed names raise ``InvalidName``.
        """
        ...

    def add_to_group(self, group_id: str, member_id: str) -> Group:
        """Add ``member_id`` to ``group_id``; raises ``CycleDetected`` on cycle."""
        ...

    def remove_from_group(self, group_id: str, member_id: str) -> Group:
        """Remove ``member_id`` from ``group_id``."""
        ...

    def delete_group(self, group_id: str) -> None:
        """Delete ``group_id``; dangling references in other groups resolve to ``False``."""
        ...

    def read_group(self, group_id: str) -> Group:
        """Return the group, or raise ``NotFound``."""
        ...

    def list_groups(self, name: str | None = None) -> list[Group]:
        """Return groups sorted by ``group_id``.

        When ``name`` is supplied, returns only groups whose display
        ``name`` matches exactly (case-sensitive) — 0..N matches;
        ``name=None`` returns all (issue #128).
        """
        ...

    def resolve_worker_in_group(self, worker_id: str, group_id: str) -> bool:
        """Return whether ``worker_id`` is transitively in ``group_id``."""
        ...

    # ------------------------------------------------------------------
    # Portable-checkpoint export / import (12b)
    # ------------------------------------------------------------------

    def export_checkpoint(
        self,
        stream: BinaryIO,
        *,
        experiment_config: str | bytes = "",
        repo_bundle: bytes = b"",
        exporter_info: ExporterInfo | None = None,
    ) -> CheckpointManifest:
        """Write a portable-checkpoint archive of the store's state to ``stream``.

        Implements the ``Store.export_checkpoint`` operation defined in
        [`spec/v0/08-storage.md`](../../../../spec/v0/08-storage.md) §1.9
        and the format in
        [`spec/v0/10-checkpoints.md`](../../../../spec/v0/10-checkpoints.md).
        The snapshot is transactionally consistent per chapter 10 §6.

        ``experiment_config`` and ``repo_bundle`` are caller-supplied
        substrate-external pieces; the format carries them alongside
        the Store-managed JSONL data so a receiver has everything it
        needs to re-materialize the experiment.
        """
        ...

    def import_checkpoint(
        self,
        stream: BinaryIO,
        *,
        as_experiment_id: str | None = None,
        extract_dir: Path | None = None,
    ) -> ImportResult:
        """Read a portable-checkpoint archive and bulk-insert into the store.

        Implements the ``Store.import_checkpoint`` operation defined in
        [`spec/v0/08-storage.md`](../../../../spec/v0/08-storage.md) §1.9.
        Preconditions:

        - The manifest's ``spec_version`` MUST match this binding's
          target.
        - The store's ``experiment_id`` MUST equal the manifest's id
          (or ``as_experiment_id`` if supplied), or the call raises
          ``ExperimentIdMismatch``.
        - The store MUST be empty (no Store-managed mutations beyond
          defaults), or the call raises ``ExperimentIdConflict`` per
          [`spec/v0/10-checkpoints.md`](../../../../spec/v0/10-checkpoints.md)
          §11.

        On success commits a single atomic transaction containing every
        Store-managed entity + the imported experiment's runtime state +
        ``imported_from`` per chapter 10 §10.
        """
        ...
