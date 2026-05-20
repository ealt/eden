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
import re
import secrets
from collections.abc import Callable, Iterable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from eden_contracts import (
    DispatchMode,
    EvaluationPayload,
    EvaluationSchema,
    EvaluationTask,
    Event,
    ExecutionPayload,
    ExecutionTask,
    Experiment,
    ExperimentState,
    FailReason,
    Group,
    Idea,
    IdeationPayload,
    IdeationTask,
    ImportProvenance,
    ReclaimCause,
    Task,
    TaskClaim,
    TaskTarget,
    Variant,
    Worker,
)
from pydantic import BaseModel, ValidationError

from .errors import (
    AlreadyExists,
    ConflictingResubmission,
    CycleDetected,
    IllegalTransition,
    InvalidPrecondition,
    NoOpVariant,
    NotClaimed,
    NotFound,
    ReservedIdentifier,
    WorkerNotEligible,
    WorkerNotRegistered,
    WrongClaimant,
)
from .submissions import (
    EvaluationSubmission,
    IdeaSubmission,
    Submission,
    VariantSubmission,
    submissions_equivalent,
)

# Reserved identifiers that MUST be rejected by `register_worker` /
# `register_group` even though the id grammar admits them. See
# [`spec/v0/02-data-model.md`](../../../../spec/v0/02-data-model.md) §6.1.
RESERVED_IDENTIFIERS: frozenset[str] = frozenset({"admin", "system", "internal"})

_WORKER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# Live task states for the 12a-2 §6.4 at-most-one-live invariants.
# A task in any of these states is "in-flight" and blocks a second
# create for the same idea / variant. Terminal states (completed,
# failed) do not block.
_LIVE_TASK_STATES: frozenset[str] = frozenset({"pending", "claimed", "submitted"})

# The default dispatch_mode on a freshly-initialized experiment per
# `02-data-model.md` §2.4. Backends seed this on first open. The four
# operational keys default to "auto"; `termination` (added in 12a-3)
# defaults to "manual" so pre-12a-3 deployments are unchanged by the
# new key — operators have to flip it on explicitly to opt in to
# policy-driven termination.
_DEFAULT_DISPATCH_MODE: dict[str, str] = {
    "termination": "manual",
    "ideation_creation": "auto",
    "execution_dispatch": "auto",
    "evaluation_dispatch": "auto",
    "integration": "auto",
}

# Experiment lifecycle states per `02-data-model.md` §2.5. The default
# at experiment creation is "running"; "terminated" is a one-way
# transition committed by `terminate_experiment`.
_DEFAULT_EXPERIMENT_STATE: str = "running"

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
    # Worker registry (12a-1 wave 2). The wire-visible `Worker` shape
    # carries no credential; per-worker `auth_credential_hash` lives in
    # `worker_credentials` keyed by `worker_id`. Backends persist the
    # two streams together so registration and credential issuance
    # commit atomically.
    workers: dict[str, Worker] = field(default_factory=dict)
    worker_credentials: dict[str, str] = field(default_factory=dict)
    worker_deletes: set[str] = field(default_factory=set)
    # Group registry. Same shape considerations as workers: the wire
    # `Group` carries the membership list; `groups` stages the
    # post-mutation Group object, and `group_deletes` removes a row.
    groups: dict[str, Group] = field(default_factory=dict)
    group_deletes: set[str] = field(default_factory=set)
    # 12a-2 dispatch_mode (`02-data-model.md` §2.4). A full post-update
    # state is staged when any key changes; backends overwrite the
    # persisted record atomically with the rest of the commit. ``None``
    # means "no dispatch_mode write this commit" — callers that only
    # update task / event state leave this field at its default.
    dispatch_mode: dict[str, str] | None = None
    # 12a-3 experiment lifecycle state (`02-data-model.md` §2.5).
    # ``None`` means "no experiment-state write this commit"; a literal
    # ``"running"`` / ``"terminated"`` stages the field for atomic
    # commit alongside ``experiment.terminated`` (or any future
    # lifecycle event).
    experiment_state: str | None = None
    # 12b experiment import-provenance (`02-data-model.md` §2.5,
    # `10-checkpoints.md` §10). Two-state semantics distinct from the
    # value space: ``None`` means "no imported_from write this commit"
    # (the field is unchanged); a one-tuple wraps the value to write
    # (where the inner value MAY itself be ``None`` for the "native
    # creation" state). The wrapper sidesteps the
    # absent-vs-explicit-null ambiguity at the staging layer.
    imported_from_update: tuple[ImportProvenance | None] | None = None


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


# ----------------------------------------------------------------------
# Helpers for the §3.3 non-no-op variant check (used by
# `_StoreBase._validate_non_no_op_variant`). Split out so each gate of
# the rule reads as a named predicate.
# ----------------------------------------------------------------------


def _no_op_check_inputs(
    task: Task,
    submission: Submission,
    get_idea: Callable[[str], Idea | None],
) -> tuple[str, list[str]] | None:
    """Return ``(commit_sha, parent_commits)`` when the §3.3 check should run.

    Returns ``None`` (silently skip the check) when the submission
    shape, idea attachment, or parent list rules out a no-op tree
    comparison.
    """
    if not isinstance(submission, VariantSubmission):
        return None
    if submission.status != "success":
        return None
    if submission.commit_sha is None:
        return None
    assert isinstance(task, ExecutionTask)
    idea = get_idea(task.payload.idea_id)
    if idea is None or not idea.parent_commits:
        return None
    return submission.commit_sha, list(idea.parent_commits)


def _all_parents_equal_sha(sha: str, parents: list[str]) -> bool:
    """True when ``sha`` is byte-equal to every parent (Layer 1 fast path)."""
    return all(p == sha for p in parents)


def _resolve_trees(
    resolver: Callable[[str], str | None],
    sha: str,
    parents: list[str],
) -> tuple[str, list[str]] | None:
    """Run the tree resolver against ``sha`` + every parent.

    Returns ``(submission_tree, parent_trees)`` when every resolver
    call yields a non-``None`` tree. Returns ``None`` when the
    resolver raises or returns ``None`` for any SHA — Layer 2 is
    silently disabled for this submission in that case (Layer 1's
    fast path still applies).
    """
    try:
        sub_tree = resolver(sha)
    except Exception:  # noqa: BLE001 — resolver is binding-supplied; contain errors
        return None
    if sub_tree is None:
        return None
    parent_trees: list[str] = []
    for p in parents:
        try:
            t = resolver(p)
        except Exception:  # noqa: BLE001
            return None
        if t is None:
            return None
        parent_trees.append(t)
    return sub_tree, parent_trees


def _sha_equality_message(task_id: str, sha: str) -> str:
    return (
        f"execution submission for task {task_id!r} has "
        f"commit_sha={sha!r} equal to every parent_commit; the "
        "variant tree is identical to the parent tree (no-op). "
        "spec/v0/03-roles.md §3.3 non-no-op invariant."
    )


def _tree_identity_message(task_id: str, sha: str, sub_tree: str) -> str:
    return (
        f"execution submission for task {task_id!r} has "
        f"commit_sha={sha!r} whose tree {sub_tree!r} is identical "
        "to the tree of every parent_commit; the variant "
        "contributes no change. spec/v0/03-roles.md §3.3 "
        "non-no-op invariant."
    )


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
        tree_resolver: Callable[[str], str | None] | None = None,
    ) -> None:
        self._experiment_id = experiment_id
        self._evaluation_schema = evaluation_schema
        self._now = now or (lambda: datetime.now(UTC))
        self._event_ids = itertools.count(1)
        self._event_id_factory = event_id_factory or self._default_event_id
        # 12a-1i: tree-of-commit resolver used to enforce the
        # `spec/v0/03-roles.md` §3.3 non-no-op variant invariant on
        # execution-task submit. When ``None`` (e.g. unit-test fixtures
        # without a real bare repo, or conformance harnesses that use
        # synthetic SHAs), the Store falls back to a SHA-equality check:
        # ``commit_sha`` equal to a parent SHA is still definitionally a
        # no-op (a commit's tree-of-self is itself's tree). When set,
        # the resolver is called for both the submission SHA and each
        # parent SHA; if every resolved parent tree equals the submission
        # tree, the submission is rejected with ``NoOpVariant``. The
        # resolver MUST return ``None`` for SHAs that don't resolve (so
        # the Store can degrade gracefully when a parent_commit names a
        # SHA absent from the resolver's repo) and MUST NOT raise.
        self._tree_resolver = tree_resolver

    def _default_event_id(self) -> str:
        return f"evt-{next(self._event_ids):06d}"

    def _reseed_default_event_counter(self) -> None:
        r"""Advance the default ``_event_ids`` counter past every persisted event.

        Called by :func:`eden_storage._checkpoint.import_checkpoint`
        AFTER a successful bulk-insert so the next emitted event_id does
        not collide with any imported ``evt-NNNNNN`` value (the
        12a-1 / 12b factory format). Scans the persisted event log for
        ids matching ``evt-(\d+)``; the counter restarts at
        ``max(seen) + 1``. Foreign IUTs whose event_ids don't match the
        pattern are ignored — the receiving counter restarts at 1
        regardless (foreign formats don't collide with ``evt-NNNNNN``
        emissions by construction).

        No-op when the caller supplied a custom ``event_id_factory``;
        responsibility for collision-avoidance falls on the caller in
        that case. Bound-method identity (``is``) is unreliable here
        because each ``self._default_event_id`` attribute access
        creates a fresh bound-method object; we compare against the
        underlying function via ``__func__`` so the equality holds
        across the init-time / reseed-time access.
        """
        factory = self._event_id_factory
        if getattr(factory, "__func__", None) is not _StoreBase._default_event_id:
            return
        max_seen = 0
        for event in self._iter_events():
            m = re.match(r"^evt-(\d+)$", event.event_id)
            if m is None:
                continue
            n = int(m.group(1))
            if n > max_seen:
                max_seen = n
        if max_seen > 0:
            self._event_ids = itertools.count(max_seen + 1)

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

    def _get_worker(self, worker_id: str) -> Worker | None:
        """Return the wire-visible Worker shape, or ``None`` if absent.

        MUST NOT include the credential hash on the returned object —
        the wire schema in
        [`spec/v0/schemas/worker.schema.json`](../../../../spec/v0/schemas/worker.schema.json)
        excludes it ([`spec/v0/02-data-model.md`](../../../../spec/v0/02-data-model.md)
        §6.2).
        """
        raise NotImplementedError

    def _get_worker_credential_hash(self, worker_id: str) -> str | None:
        """Return the stored credential hash for ``worker_id``, or ``None``.

        Used only by ``verify_worker_credential`` and the registration /
        rotation paths; never exposed through public reads.
        """
        raise NotImplementedError

    def _iter_workers(self) -> Iterable[Worker]:
        """Iterate registered workers (any order; backends sort by ``worker_id``)."""
        raise NotImplementedError

    def _get_group(self, group_id: str) -> Group | None:
        """Return the stored group, or ``None`` if absent."""
        raise NotImplementedError

    def _iter_groups(self) -> Iterable[Group]:
        """Iterate registered groups (any order; backends sort by ``group_id``)."""
        raise NotImplementedError

    def _get_dispatch_mode(self) -> dict[str, str]:
        """Return the persisted dispatch_mode (full state, every normative key).

        Backends MUST return a dict whose keys cover the normative
        decision-types (``termination`` from 12a-3 plus the four
        operational keys ``ideation_creation`` / ``execution_dispatch``
        / ``evaluation_dispatch`` / ``integration``); unknown keys
        previously written are preserved verbatim per
        [`spec/v0/02-data-model.md`](../../../../spec/v0/02-data-model.md)
        §2.4.
        """
        raise NotImplementedError

    def _get_experiment(self) -> Experiment:
        """Return the experiment runtime object (state + created_at).

        Backends MUST persist these fields across restart per
        [`spec/v0/02-data-model.md`](../../../../spec/v0/02-data-model.md)
        §2.5. On a freshly-initialized experiment, ``state`` is
        ``"running"`` and ``created_at`` is the timestamp the row was
        first inserted.
        """
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
            return self._insert_ideation_task(task)
        if task.kind == "execution":
            assert isinstance(task, ExecutionTask)
            return self._insert_execution_task(task)
        assert isinstance(task, EvaluationTask)
        return self._insert_evaluation_task(task)

    def _insert_ideation_task(self, task: IdeationTask) -> IdeationTask:
        if task.payload.experiment_id != self._experiment_id:
            raise InvalidPrecondition(
                f"ideation task payload.experiment_id={task.payload.experiment_id!r} "
                f"does not match store experiment {self._experiment_id!r}"
            )
        with self._atomic_operation():
            self._require_running()
            self._require_no_task(task.task_id)
            tx = _Tx()
            tx.tasks[task.task_id] = _deep(task)
            tx.events.append(
                self._event("task.created", {"task_id": task.task_id, "kind": "ideation"})
            )
            self._apply_commit(tx)
            return _deep(task)

    def _insert_execution_task(self, task: ExecutionTask) -> ExecutionTask:
        with self._atomic_operation():
            self._require_running()
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
            self._require_no_live_execution_task_for_idea(idea_id)
            # 12a-3 intended_executor flow-through (`03-roles.md` §6.2
            # decision-type 2): when the caller does not supply an
            # explicit task.target on the create payload, the resulting
            # task inherits the idea's routing hint. An explicit
            # task.target wins over the idea's hint (admin override).
            insert_task = task
            if task.target is None and idea.intended_executor is not None:
                insert_task = _validated_update(
                    task, target=idea.intended_executor
                )
            tx = _Tx()
            tx.tasks[insert_task.task_id] = _deep(insert_task)
            tx.ideas[idea_id] = _validated_update(idea, state="dispatched")
            tx.events.append(
                self._event(
                    "task.created",
                    {"task_id": insert_task.task_id, "kind": "execution"},
                )
            )
            tx.events.append(
                self._event(
                    "idea.dispatched",
                    {"idea_id": idea_id, "task_id": insert_task.task_id},
                )
            )
            self._apply_commit(tx)
            return _deep(insert_task)

    def _insert_evaluation_task(self, task: EvaluationTask) -> EvaluationTask:
        with self._atomic_operation():
            self._require_running()
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
            self._require_no_live_evaluation_task_for_variant(variant_id)
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
            self._require_running()
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

    def create_execution_task(
        self,
        task_id: str,
        idea_id: str,
        *,
        target: TaskTarget | None = None,
    ) -> ExecutionTask:
        """Create an ``execution`` task; composite-commits ``idea.dispatched``.

        Per ``05-event-protocol.md`` §2.2: creating the execution task
        and transitioning the referenced idea from ``ready`` to
        ``dispatched`` land in one atomic commit.

        Per ``03-roles.md`` §6.4 the Store enforces at-most-one live
        execution task per idea; a duplicate concurrent attempt
        observes the first commit and raises ``AlreadyExists`` here
        (in practice the idea-state precondition catches it first,
        since the first create transitions the idea to ``dispatched``,
        but the explicit live-task check makes the §6.4 invariant
        unambiguous in code).

        12a-3: when ``target`` is supplied, the resulting task uses it
        verbatim (admin override path per ``03-roles.md`` §6.5). When
        ``target`` is ``None``, the task inherits
        ``idea.intended_executor`` (the auto-orchestrator path per
        ``03-roles.md`` §6.2 decision-type 2). When both are unset
        the task is open to any registered executor-class worker.
        """
        with self._atomic_operation():
            self._require_running()
            self._require_no_task(task_id)
            idea = self._get_idea(idea_id)
            if idea is None:
                raise NotFound(f"idea {idea_id!r}")
            if idea.state != "ready":
                raise InvalidPrecondition(
                    f"idea {idea_id!r} must be 'ready' "
                    f"to dispatch, not {idea.state!r}"
                )
            self._require_no_live_execution_task_for_idea(idea_id)
            now = self._ts()
            # Explicit caller-supplied target wins; otherwise inherit
            # `idea.intended_executor` per the 12a-3 §6.2 decision-type 2
            # flow-through rule. The `NotNone` validator on
            # `_TaskBase.target` rejects explicit None — keep the kwarg
            # absent when no target applies.
            effective_target = (
                target if target is not None else idea.intended_executor
            )
            task_kwargs: dict[str, Any] = {
                "task_id": task_id,
                "kind": "execution",
                "state": "pending",
                "payload": ExecutionPayload(idea_id=idea_id),
                "created_at": now,
                "updated_at": now,
            }
            if effective_target is not None:
                task_kwargs["target"] = effective_target
            task = ExecutionTask(**task_kwargs)
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
        """Create an ``evaluation`` task against a starting variant with ``commit_sha``.

        Per ``03-roles.md`` §6.4 the Store enforces at-most-one live
        evaluation task per starting variant; a duplicate concurrent
        attempt raises ``AlreadyExists``. A previously-failed
        evaluation task for the same variant does NOT block a new one
        (terminal states are not live).
        """
        with self._atomic_operation():
            self._require_running()
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
            self._require_no_live_evaluation_task_for_variant(variant_id)
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
        """Transition a pending task to claimed.

        Per
        [`spec/v0/04-task-protocol.md`](../../../../spec/v0/04-task-protocol.md)
        §3.5, the claim enforces three preconditions atomically with
        the state write:

        1. The task is in ``pending`` state (§3.4) — else
           :class:`IllegalTransition`.
        2. ``worker_id`` is registered for this experiment (§3.5 step 2)
           — else :class:`WorkerNotRegistered`.
        3. ``worker_id`` satisfies ``task.target`` (§3.5 step 3): a
           ``worker`` target requires ``worker_id == target.id``; a
           ``group`` target requires transitive membership; absent
           target permits any registered worker. Failure raises
           :class:`WorkerNotEligible`.

        The Store trusts the supplied ``worker_id`` as data;
        authentication of the caller against that id is the binding's
        responsibility (§3.3).
        """
        with self._atomic_operation():
            # §3.5 step 0 (12a-3): terminated-experiment guard runs
            # before the state precondition so a `pending` task in a
            # terminated experiment is unreachable, not "claim
            # rejected because not pending".
            self._require_running()
            task = self._require_task(task_id)
            if task.state != "pending":
                raise IllegalTransition(
                    f"cannot claim task in state {task.state!r} (must be 'pending')"
                )
            # §3.5 step 2: registration check. The check runs even when
            # the target is null; an unregistered worker MUST NOT claim
            # an open task per the spec.
            if self._get_worker(worker_id) is None:
                raise WorkerNotRegistered(
                    f"worker_id={worker_id!r} is not registered for "
                    f"experiment {self._experiment_id!r}"
                )
            # §3.5 step 3: target eligibility.
            target = task.target
            if target is not None:
                if target.kind == "worker":
                    if worker_id != target.id:
                        raise WorkerNotEligible(
                            f"task.target requires worker_id={target.id!r}; "
                            f"caller is {worker_id!r}"
                        )
                else:  # target.kind == "group"
                    # The public resolver re-enters the atomic-operation
                    # context (RLock + `_in_txn` guard), so calling it
                    # from inside `claim`'s transaction is safe.
                    if not self.resolve_worker_in_group(
                        worker_id, target.id
                    ):
                        raise WorkerNotEligible(
                            f"task.target requires membership in group "
                            f"{target.id!r}; worker_id={worker_id!r} is not "
                            "transitively a member"
                        )
            claimed_at = self._ts()
            claim_kwargs: dict[str, Any] = {
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

    def submit(
        self, task_id: str, worker_id: str, submission: Submission
    ) -> None:
        """Transition a claimed task to submitted, persisting the result.

        Per ``04-task-protocol.md`` §4.1, the atomic claim-match
        ``task.claim.worker_id == worker_id`` runs **as part of the
        submit transition** — a non-atomic "read claim, compare, then
        write" sequence would introduce a TOCTOU race against reclaim.
        Mismatches raise ``WrongClaimant`` (claim exists, different
        worker) or ``NotClaimed`` (claim cleared / task not in
        ``claimed``).

        Idempotent per §4.2: a resubmit by the same claimant with a
        content-equivalent payload MUST succeed without mutating state;
        an inconsistent resubmit MUST be rejected. Authentication of
        the caller against ``worker_id`` is the binding's
        responsibility (§3.3); the Store trusts the parameter as data.

        Atomically writes ``task.submitted_by = worker_id`` so the
        claimant identity survives the terminal transitions that clear
        ``claim``.
        """
        with self._atomic_operation():
            task = self._require_task(task_id)
            if task.state not in {"claimed", "submitted"}:
                # NotClaimed covers reclaimed-or-terminal tasks; non-claimed
                # states all share the inverse of the §4.1 step-1 precondition.
                raise NotClaimed(
                    f"task {task_id!r} is in state {task.state!r}, not 'claimed'"
                )
            if task.claim is None:
                raise NotClaimed(
                    f"task {task_id!r} has no active claim (claim cleared by reclaim "
                    "or terminal transition)"
                )
            # §3.5 step 2 (extended to submit): the submitting worker_id MUST
            # still be a registered worker. A worker that was registered at
            # claim time and then removed from the registry MUST NOT be able
            # to submit. Surfaces as the same WorkerNotRegistered error the
            # claim path uses so the binding maps to the same wire status.
            if self._get_worker(worker_id) is None:
                raise WorkerNotRegistered(
                    f"submit by worker_id={worker_id!r} rejected: worker is "
                    f"not registered for experiment {self._experiment_id!r}"
                )
            if task.claim.worker_id != worker_id:
                raise WrongClaimant(
                    f"submit by worker_id={worker_id!r} does not match "
                    f"task.claim.worker_id={task.claim.worker_id!r}"
                )
            self._require_submission_kind_matches(task, submission)
            self._validate_submission_ref_binding(task, submission)

            if task.state == "submitted":
                # §4.2 idempotency precedes the no-op check: a
                # content-equivalent retry of an already-committed
                # submission MUST be accepted without re-evaluation.
                # Re-running `_validate_non_no_op_variant` here could
                # raise `NoOpVariant` on a previously-accepted submit
                # if the tree resolver's view of the SHAs has shifted
                # since the first commit (e.g. submit-time resolver
                # transient miss now resolved), which would violate
                # §4.2.
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

            # 03-roles.md §3.3 non-no-op invariant + §3.4 rejection
            # rule. Content-derived (depends only on submission fields
            # and the idea's parent_commits), so enforced at submit
            # time for fresh submissions. Resubmits against a still-
            # `submitted` task short-circuit at the §4.2 equivalence
            # check above, never reaching here.
            self._validate_non_no_op_variant(task, submission)

            now = self._ts()
            tx = _Tx()
            tx.tasks[task_id] = _validated_update(
                task, state="submitted", submitted_by=worker_id, updated_at=now
            )
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

    def reassign_task(
        self,
        task_id: str,
        new_target: TaskTarget | None,
        *,
        reason: str,
        reassigned_by: str,
    ) -> Task:
        """Atomically update a task's ``target`` per chapter 04 §6.

        ``pending`` → field update + ``task.reassigned`` event.
        ``claimed`` → composite-commit: clear the claim,
        emit ``task.reclaimed`` (``cause="operator"``), update target,
        emit ``task.reassigned``. The reclaim path follows the
        execution-task variant-side rule from ``reclaim``: if the
        claimant was an executor whose in-flight variant is still in
        ``starting``, the variant transitions to ``error`` atomically.
        ``submitted`` / terminal → ``InvalidPrecondition``; the spec
        forbids reassign past the claimed phase to preserve
        attribution-of-submission contracts.

        Reason text must be non-empty (``05-event-protocol.md`` §3.1
        carries it in the event payload). ``reassigned_by`` must match
        the §6.1 grammar; binding-layer authorization is the caller's
        responsibility.
        """
        if not reason:
            raise InvalidPrecondition("reassign_task requires a non-empty reason")
        self._validate_registry_id(reassigned_by, kind="actor")
        with self._atomic_operation():
            task = self._require_task(task_id)
            if task.state not in {"pending", "claimed"}:
                raise InvalidPrecondition(
                    f"cannot reassign task {task_id!r} in state {task.state!r}; "
                    "only pending or claimed tasks may be reassigned "
                    "(04-task-protocol.md §6.1)"
                )
            # No-op idempotency: same target shape → emit nothing.
            current_target = task.target
            if self._targets_equal(current_target, new_target) and task.state == "pending":
                return _deep(task)

            now = self._ts()
            tx = _Tx()
            # `target=None` removes the field; `_validated_update`'s
            # None-as-absent convention handles that. The state itself
            # always lands at "pending" after a reassign — either it
            # was already pending (no transition) or the composite
            # reclaim-then-reassign path took it from claimed→pending.
            updated_kwargs: dict[str, Any] = {
                "target": new_target,
                "state": "pending",
                "claim": None,
                "updated_at": now,
            }
            tx.tasks[task_id] = _validated_update(task, **updated_kwargs)

            if task.state == "claimed":
                # Composite-commit: reclaim first, then reassign.
                # Order in the event log mirrors the causal order; both
                # land in the same `read_range` slice and no
                # intermediate state is observable.
                tx.events.append(
                    self._event(
                        "task.reclaimed",
                        {"task_id": task_id, "cause": "operator"},
                    )
                )
                # Per `reclaim`: an execution task whose claimant's
                # in-flight variant is still `starting` transitions
                # that variant to `error` atomically (`05-event-protocol.md`
                # §2.2). The reassign-on-claimed path inherits this so
                # an operator who reassigns to a different executor
                # doesn't leave an orphaned `starting` variant.
                if task.kind == "execution":
                    variant = self._find_starting_variant_for_implement_task(task)
                    if variant is not None:
                        tx.variants[variant.variant_id] = _validated_update(
                            variant, status="error", completed_at=now
                        )
                        tx.events.append(
                            self._event(
                                "variant.errored",
                                {"variant_id": variant.variant_id},
                            )
                        )
                # A submission MUST NOT survive the reclaim half of the
                # composite (a claimed task can't yet have a recorded
                # submission, but be defensive in case the state model
                # ever permits it).
                if self._get_submission(task_id) is not None:
                    tx.task_deletes_submission.add(task_id)

            new_target_payload: TaskTarget | None
            if new_target is None:
                new_target_payload = None
            else:
                # Re-dump through the model so the event payload's
                # shape is identical to the wire schema.
                new_target_payload = TaskTarget.model_validate(
                    new_target.model_dump(mode="json", exclude_none=True)
                )

            tx.events.append(
                self._event(
                    "task.reassigned",
                    {
                        "task_id": task_id,
                        "new_target": (
                            None
                            if new_target_payload is None
                            else new_target_payload.model_dump(
                                mode="json", exclude_none=True
                            )
                        ),
                        "reason": reason,
                        "reassigned_by": reassigned_by,
                    },
                )
            )
            self._apply_commit(tx)
            return _deep(tx.tasks[task_id])

    @staticmethod
    def _targets_equal(
        a: TaskTarget | None, b: TaskTarget | None
    ) -> bool:
        if a is None and b is None:
            return True
        if a is None or b is None:
            return False
        return a.kind == b.kind and a.id == b.id

    # ------------------------------------------------------------------
    # Dispatch mode (12a-2)
    # ------------------------------------------------------------------

    def read_dispatch_mode(self) -> DispatchMode:
        """Return the experiment's current dispatch_mode (every key).

        Defaults to all-``auto`` on the four operational keys and
        ``"manual"`` on ``termination`` for a freshly-initialized
        experiment ([`spec/v0/02-data-model.md`](../../../../spec/v0/02-data-model.md)
        §2.4). Unknown keys persisted by older writes are returned via
        the model's ``extra="allow"`` carry-through.
        """
        with self._atomic_operation():
            return DispatchMode.model_validate(self._get_dispatch_mode())

    def read_experiment(self) -> Experiment:
        """Return the experiment runtime object (state + created_at).

        Per [`spec/v0/02-data-model.md`](../../../../spec/v0/02-data-model.md)
        §2.5. The runtime object is distinct from the declarative
        ``experiment-config``; it carries only observed runtime state.
        """
        with self._atomic_operation():
            return _deep(self._get_experiment())

    def read_experiment_state(self) -> ExperimentState:
        """Return the experiment's current lifecycle state.

        Defaults to ``"running"`` on a freshly-initialized experiment;
        becomes ``"terminated"`` after :meth:`terminate_experiment` or
        the policy-driven termination branch commits the transition.
        """
        with self._atomic_operation():
            return self._get_experiment().state

    def update_experiment_state(self, new_state: ExperimentState) -> Experiment:
        """Internal primitive: atomically update the experiment lifecycle state.

        Not a public wire op in v0 (per
        [`spec/v0/04-task-protocol.md`](../../../../spec/v0/04-task-protocol.md)
        §8.3). Used by :meth:`terminate_experiment` and the
        orchestrator's policy-driven termination branch
        ([`spec/v0/03-roles.md`](../../../../spec/v0/03-roles.md)
        §6.2 decision-type 0). v0 defines exactly one legal transition
        (``"running" → "terminated"``); other values raise
        ``IllegalTransition``.

        This method does NOT emit ``experiment.terminated`` on its own
        — composite commit with the appropriate event is the caller's
        responsibility. Use :meth:`terminate_experiment` for the
        normal public-op shape.
        """
        if new_state not in ("running", "terminated"):
            raise InvalidPrecondition(
                f"experiment.state value {new_state!r} is not "
                "'running' or 'terminated'"
            )
        with self._atomic_operation():
            current = self._get_experiment()
            if current.state == new_state:
                return _deep(current)
            if not (current.state == "running" and new_state == "terminated"):
                raise IllegalTransition(
                    f"cannot transition experiment state "
                    f"{current.state!r} → {new_state!r}"
                )
            tx = _Tx()
            tx.experiment_state = new_state
            self._apply_commit(tx)
            return _validated_update(current, state=new_state)

    def terminate_experiment(
        self, *, reason: str, terminated_by: str
    ) -> Experiment:
        """Atomically commit the ``running → terminated`` lifecycle transition.

        Per [`spec/v0/04-task-protocol.md`](../../../../spec/v0/04-task-protocol.md)
        §8.1: the state field update and the ``experiment.terminated``
        event are a single transaction. Idempotent on the terminated
        state — a second call returns success without committing a
        second transition and without appending a second event; the
        winning caller's ``reason`` (the first commit) is the one
        recorded.

        Authority enforcement (caller in ``admins``) is the binding's
        responsibility; the Store trusts ``terminated_by`` as data.
        Composite-commits the state update and the event per
        [`spec/v0/05-event-protocol.md`](../../../../spec/v0/05-event-protocol.md)
        §2.
        """
        self._validate_registry_id(terminated_by, kind="actor")
        with self._atomic_operation():
            current = self._get_experiment()
            if current.state == "terminated":
                # §8.1 idempotency: success, no second event, prior
                # reason preserved.
                return _deep(current)
            tx = _Tx()
            tx.experiment_state = "terminated"
            tx.events.append(
                self._event(
                    "experiment.terminated",
                    {"reason": reason, "terminated_by": terminated_by},
                )
            )
            self._apply_commit(tx)
            return _validated_update(current, state="terminated")

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
        but EXEMPT from the §2 transactional invariant (no
        protocol-owned state mutation pairs with it).
        """
        if not policy_kind:
            raise InvalidPrecondition("policy_kind MUST be non-empty")
        if not error_type:
            raise InvalidPrecondition("error_type MUST be non-empty")
        with self._atomic_operation():
            tx = _Tx()
            tx.events.append(
                self._event(
                    "experiment.policy_error",
                    {
                        "policy_kind": policy_kind,
                        "error_type": error_type,
                        "error_message": error_message,
                    },
                )
            )
            self._apply_commit(tx)

    def _require_running(self) -> None:
        """Raise :class:`IllegalTransition` if the experiment is terminated.

        Called from every ``create_task`` entry point and from
        :meth:`claim` to enforce the terminated-experiment guard per
        [`spec/v0/02-data-model.md`](../../../../spec/v0/02-data-model.md)
        §2.5 and [`spec/v0/04-task-protocol.md`](../../../../spec/v0/04-task-protocol.md)
        §2 / §3.5 step 0. Already-claimed tasks may still complete;
        the guard applies only to new claim and create-task attempts.
        """
        state = self._get_experiment().state
        if state != "running":
            raise IllegalTransition(
                f"experiment {self._experiment_id!r} is "
                f"{state!r}; new tasks and claims are forbidden "
                "(02-data-model.md §2.5)"
            )

    def update_dispatch_mode(
        self,
        updates: DispatchMode | dict[str, str],
        *,
        updated_by: str,
    ) -> DispatchMode:
        """Atomically merge ``updates`` into the experiment's dispatch_mode.

        Spec: [`spec/v0/04-task-protocol.md`](../../../../spec/v0/04-task-protocol.md)
        §7 + [`spec/v0/05-event-protocol.md`](../../../../spec/v0/05-event-protocol.md)
        §3.4. Omitted keys are preserved; unknown keys in ``updates``
        round-trip through (§2.5 tolerance). When no key actually
        changes value, NO event fires (the spec records changes, not
        idempotent no-ops).
        """
        self._validate_registry_id(updated_by, kind="actor")
        if isinstance(updates, DispatchMode):
            update_map = updates.model_dump(mode="json", exclude_none=True)
        else:
            update_map = dict(updates)
        # Reject values that are not in the closed value-set; tolerate
        # unknown keys per §2.5 but keep the value-grammar strict.
        for key, value in update_map.items():
            if value not in {"auto", "manual"}:
                raise InvalidPrecondition(
                    f"dispatch_mode.{key} value {value!r} is not 'auto' or 'manual'"
                )
        with self._atomic_operation():
            current = dict(self._get_dispatch_mode())
            changed: dict[str, str] = {}
            for key, value in update_map.items():
                if current.get(key) != value:
                    changed[key] = value
            if not changed:
                # No-op flip: no event, no write. The committed state
                # is exactly what we read.
                return DispatchMode.model_validate(current)
            new_state = {**current, **changed}
            tx = _Tx()
            tx.dispatch_mode = new_state
            tx.events.append(
                self._event(
                    "experiment.dispatch_mode_changed",
                    {
                        "dispatch_mode": new_state,
                        "changed": changed,
                        "updated_by": updated_by,
                    },
                )
            )
            self._apply_commit(tx)
            return DispatchMode.model_validate(new_state)

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
                f"cannot accept ideation task {task.task_id!r}: {reason}"
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
        # 12a-1: write executed_by atomically with the variant's
        # status transition out of "starting" per data-model §9.
        # task.submitted_by was set on the submit transition (§4.1)
        # and is the canonical claimant identity for attribution.
        executor_worker_id = task.submitted_by
        tx.variants[variant.variant_id] = _validated_update(
            variant,
            commit_sha=submission.commit_sha,
            executed_by=executor_worker_id,
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
            # §9: executed_by records the worker whose execution-task
            # submission produced the variant — and is preserved on
            # the error path too. The accept and reject branches both
            # stamp it from task.submitted_by (the canonical claimant
            # identity set at §4.1 submit time).
            tx.variants[variant_for_error.variant_id] = _validated_update(
                variant_for_error,
                status="error",
                completed_at=now,
                executed_by=task.submitted_by,
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
        # 12a-1: write evaluated_by atomically with the variant's
        # status transition to "success" per data-model §9.
        # task.submitted_by was set on the submit transition (§4.1)
        # and is the canonical claimant identity for attribution.
        evaluator_worker_id = task.submitted_by
        tx.variants[variant.variant_id] = _validated_update(
            variant,
            status="success",
            evaluation=dict(submission.evaluation) if submission.evaluation else None,
            artifacts_uri=submission.artifacts_uri,
            completed_at=now,
            evaluated_by=evaluator_worker_id,
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
            # §9: evaluated_by records the evaluator whose submission
            # produced the result — and is preserved on the error
            # path too. The accept and reject branches both stamp it
            # from task.submitted_by (the canonical claimant identity
            # set at §4.1 submit time).
            update_kwargs: dict[str, Any] = {
                "status": "error",
                "completed_at": now,
                "evaluated_by": task.submitted_by,
            }
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

    def _validate_non_no_op_variant(
        self, task: Task, submission: Submission
    ) -> None:
        """Reject an execution-task success submission whose tree matches every parent.

        ``spec/v0/03-roles.md`` §3.3 non-no-op invariant + §3.4
        rejection rule. The rule fires only on ``execution`` tasks with
        ``status == "success"`` and a non-empty ``parent_commits``.

        Enforcement runs two layers:

        1. **SHA-equality fast path** (always on, no git dependency).
           If ``commit_sha`` is byte-equal to the only parent SHA, the
           submission is rejected. (A commit's tree-of-self is itself's
           tree, so SHA equality is unconditionally a no-op when there
           is exactly one parent.) For multi-parent ideas, byte-equality
           with *every* parent is a stronger condition than the
           tree-identity rule but is the only sound SHA-only check
           (an empty merge can have a SHA distinct from any parent).
        2. **Tree-identity check** (when a ``tree_resolver`` is wired).
           If the resolver resolves every parent SHA AND the submission
           SHA to a non-``None`` tree, and every parent's tree equals
           the submission's tree, reject. A resolver that returns
           ``None`` for any SHA leaves the deeper check disabled for
           this submission (e.g. fixture SHAs absent from a real repo);
           the SHA-equality fast path still applies.
        """
        check = _no_op_check_inputs(task, submission, self._get_idea)
        if check is None:
            return
        sha, parents = check

        if _all_parents_equal_sha(sha, parents):
            raise NoOpVariant(_sha_equality_message(task.task_id, sha))

        resolver = self._tree_resolver
        if resolver is None:
            return
        trees = _resolve_trees(resolver, sha, parents)
        if trees is None:
            return
        sub_tree, parent_trees = trees
        if all(t == sub_tree for t in parent_trees):
            raise NoOpVariant(
                _tree_identity_message(task.task_id, sha, sub_tree)
            )

    def _validate_submission_ref_binding(
        self, task: Task, submission: Submission
    ) -> None:
        """Reject a submission whose referenced IDs don't match the task.

        Per ``04-task-protocol.md`` §4.1, the submission's result
        payload is scoped to the task it is being submitted against.
        An ideation-task submission's idea_ids must reference existing
        ideas (03-roles §2.4); an execution-task submission's
        variant_id must refer to a variant under the task's idea
        (03-roles §3.4); an evaluation-task submission's variant_id must
        equal the task payload's variant_id (03-roles §4.4).
        """
        if isinstance(submission, IdeaSubmission):
            for pid in submission.idea_ids:
                idea = self._get_idea(pid)
                if idea is None:
                    raise IllegalTransition(
                        f"ideation-task submission references unknown idea {pid!r}"
                    )
                if idea.state == "drafting":
                    raise IllegalTransition(
                        f"ideation-task submission references drafting idea {pid!r}; "
                        "ideator MUST NOT submit while ideas are in drafting "
                        "(03-roles.md §2.4)"
                    )
        elif isinstance(submission, VariantSubmission):
            assert isinstance(task, ExecutionTask)
            variant = self._get_variant(submission.variant_id)
            if variant is None:
                raise IllegalTransition(
                    f"execution-task submission references unknown variant "
                    f"{submission.variant_id!r}"
                )
            if variant.idea_id != task.payload.idea_id:
                raise IllegalTransition(
                    f"execution-task submission variant_id={submission.variant_id!r} "
                    f"belongs to idea {variant.idea_id!r}, not the "
                    f"task's idea {task.payload.idea_id!r}"
                )
        elif isinstance(submission, EvaluationSubmission):
            assert isinstance(task, EvaluationTask)
            if submission.variant_id != task.payload.variant_id:
                raise IllegalTransition(
                    f"evaluation-task submission variant_id={submission.variant_id!r} "
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
        # 03-roles.md §3.3 enforcement runs at submit time only (see
        # `_validate_non_no_op_variant` call from `submit`). An
        # accept-time recheck would re-evaluate against a potentially-
        # shifted local clone view (e.g., a SHA that wasn't resolvable
        # at submit but is now); if it raises after the executor has
        # already published `work/*` refs upstream, those refs leak —
        # the recheck path doesn't have a clean-up channel. The
        # canonical enforcement is the executor's pre-submit
        # `_is_no_op_variant` check against its own controlled clone;
        # the server's submit-time SHA-equality fast path catches the
        # literal case for any wire-side caller. spec/v0/03-roles.md
        # §3.4 explicitly permits IUT-distributed enforcement.
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
        """Validate fields that a `status=error` evaluation-task submission would write."""
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

    def _require_no_live_execution_task_for_idea(self, idea_id: str) -> None:
        """Raise ``AlreadyExists`` if a live execution task already targets ``idea_id``.

        ``03-roles.md`` §6.4: the Store enforces at-most-one live
        execution task per idea so concurrent orchestrator invocations
        collapse to a single dispatch. "Live" ≡ state in
        ``_LIVE_TASK_STATES``; terminal tasks do not block.
        """
        for existing in self._iter_tasks(kind="execution"):
            if existing.state in _LIVE_TASK_STATES and (
                isinstance(existing, ExecutionTask)
                and existing.payload.idea_id == idea_id
            ):
                raise AlreadyExists(
                    f"a live execution task already exists for idea {idea_id!r} "
                    f"(task_id={existing.task_id!r}, state={existing.state!r})"
                )

    def _require_no_live_evaluation_task_for_variant(self, variant_id: str) -> None:
        """Raise ``AlreadyExists`` if a live evaluation task already targets ``variant_id``.

        ``03-roles.md`` §6.4: at-most-one live evaluation task per
        starting variant. A previously-failed (terminal) evaluation
        task does NOT block a retry.
        """
        for existing in self._iter_tasks(kind="evaluation"):
            if existing.state in _LIVE_TASK_STATES and (
                isinstance(existing, EvaluationTask)
                and existing.payload.variant_id == variant_id
            ):
                raise AlreadyExists(
                    f"a live evaluation task already exists for variant "
                    f"{variant_id!r} (task_id={existing.task_id!r}, "
                    f"state={existing.state!r})"
                )

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
    # Worker registry (12a-1)
    # ------------------------------------------------------------------

    def register_worker(
        self,
        worker_id: str,
        *,
        labels: dict[str, str] | None = None,
        registered_by: str | None = None,
    ) -> tuple[Worker, str | None]:
        """Register ``worker_id`` for this experiment.

        Returns ``(worker, registration_token)``. On first registration
        the second element is the freshly-minted plaintext token (≥256
        bits); on idempotent re-registration of an existing
        ``worker_id`` it is ``None`` and the existing record is
        returned unchanged. The plaintext token is returned ONLY by
        this call and ``reissue_credential``; subsequent reads MUST NOT
        return it.

        Raises ``ReservedIdentifier`` for ``admin`` / ``system`` /
        ``internal`` or any id that violates the §6.1 grammar from
        [`spec/v0/02-data-model.md`](../../../../spec/v0/02-data-model.md).
        """
        self._validate_registry_id(worker_id, kind="worker")
        with self._atomic_operation():
            existing = self._get_worker(worker_id)
            if existing is not None:
                return (_deep(existing), None)
            # §7.1 disjoint-namespaces: a group with the same id MUST
            # NOT exist. Otherwise the resolver in §7.2 can't tell
            # whether a string in `Group.members` resolves through the
            # worker registry (leaf) or the group registry (recursive
            # descent).
            if self._get_group(worker_id) is not None:
                raise AlreadyExists(
                    f"id {worker_id!r} is already registered as a group; "
                    f"worker / group namespaces MUST be disjoint per "
                    f"chapter 02 §7.1"
                )
            token = self._generate_credential_token()
            credential_hash = self._hash_credential(token)
            # Build via dict so optional fields whose value is None are
            # omitted entirely. The `NotNone` validators on Worker
            # reject explicit-null inputs (mirroring the JSON-schema
            # absent-vs-null distinction in `_common.py`).
            worker_data: dict[str, Any] = {
                "worker_id": worker_id,
                "experiment_id": self._experiment_id,
                "registered_at": self._ts(),
            }
            if registered_by is not None:
                worker_data["registered_by"] = registered_by
            if labels:
                worker_data["labels"] = dict(labels)
            worker = Worker.model_validate(worker_data)
            tx = _Tx()
            tx.workers[worker_id] = _deep(worker)
            tx.worker_credentials[worker_id] = credential_hash
            self._apply_commit(tx)
            return (_deep(worker), token)

    def reissue_credential(self, worker_id: str) -> str:
        """Mint a fresh credential for ``worker_id``; invalidates the prior one.

        Returns the new plaintext registration token. Atomic with the
        write that replaces the stored hash. Raises ``NotFound`` if
        ``worker_id`` is not registered.
        """
        with self._atomic_operation():
            worker = self._get_worker(worker_id)
            if worker is None:
                raise NotFound(f"worker {worker_id!r}")
            token = self._generate_credential_token()
            credential_hash = self._hash_credential(token)
            tx = _Tx()
            # The wire-visible Worker shape is unchanged on reissue —
            # only the credential hash rotates. Stage an empty Worker
            # delta keyed off the existing record so a backend that
            # binds credential rotation to the row update commits both
            # in one statement; backends that store creds separately
            # ignore the workers-side stage and apply only the
            # credential update.
            tx.workers[worker_id] = _deep(worker)
            tx.worker_credentials[worker_id] = credential_hash
            self._apply_commit(tx)
            return token

    def verify_worker_credential(
        self, worker_id: str, registration_token: str
    ) -> bool:
        """Return ``True`` iff ``registration_token`` is the current credential.

        Returns ``False`` for unknown ``worker_id`` (rather than
        raising) so binding-layer callers can collapse "no such
        worker" and "wrong secret" into a single unauthorized outcome
        without leaking which arm hit. The unknown-worker branch
        verifies against a class-level dummy hash so the two failure
        modes incur the same argon2id cost — a timing oracle MUST NOT
        be able to distinguish "worker absent" from "secret wrong".
        """
        with self._atomic_operation():
            stored = self._get_worker_credential_hash(worker_id)
            if stored is None:
                # Constant-time defence: run verify against a dummy
                # hash so the unknown-worker path takes the same time
                # as a wrong-secret check. Discard the result.
                self._check_credential_hash(
                    registration_token, self._UNKNOWN_WORKER_DUMMY_HASH
                )
                return False
            return self._check_credential_hash(registration_token, stored)

    def read_worker(self, worker_id: str) -> Worker:
        """Return the wire-visible Worker, or raise ``NotFound``."""
        with self._atomic_operation():
            worker = self._get_worker(worker_id)
            if worker is None:
                raise NotFound(f"worker {worker_id!r}")
            return _deep(worker)

    def list_workers(self) -> list[Worker]:
        """Return all registered workers (deep copies, sorted by ``worker_id``)."""
        with self._atomic_operation():
            return [_deep(w) for w in self._iter_workers()]

    # ------------------------------------------------------------------
    # Group registry (12a-1)
    # ------------------------------------------------------------------

    def register_group(
        self,
        group_id: str,
        *,
        members: Iterable[str] | None = None,
        created_by: str | None = None,
    ) -> Group:
        """Register a new group, optionally with initial members.

        Cycles are detected at write time per
        [`spec/v0/02-data-model.md`](../../../../spec/v0/02-data-model.md)
        §7.3; a mutation that would close a cycle raises ``CycleDetected``.
        ``register_group`` of an existing ``group_id`` raises
        ``AlreadyExists`` (groups are not idempotent on re-register —
        unlike workers, group creation is operator-driven and a second
        call most likely indicates a config mistake).
        """
        self._validate_registry_id(group_id, kind="group")
        # §7 "group is a recursively-resolved set": dedup the input
        # members in stable order so the durable store's
        # `(group_id, member_id)` uniqueness constraint never sees a
        # duplicate (chapter 02 §7 + R9-1). Preserves first-occurrence
        # order so the resolver's walk is deterministic.
        member_list: list[str] = []
        seen: set[str] = set()
        for member in members or ():
            if member in seen:
                continue
            seen.add(member)
            member_list.append(member)
        for member in member_list:
            self._validate_registry_id(member, kind="member")
        with self._atomic_operation():
            if self._get_group(group_id) is not None:
                raise AlreadyExists(f"group {group_id!r}")
            # §7.1 disjoint-namespaces: a worker with the same id MUST
            # NOT exist. See the symmetric check in register_worker.
            if self._get_worker(group_id) is not None:
                raise AlreadyExists(
                    f"id {group_id!r} is already registered as a worker; "
                    f"worker / group namespaces MUST be disjoint per "
                    f"chapter 02 §7.1"
                )
            group_data: dict[str, Any] = {
                "group_id": group_id,
                "experiment_id": self._experiment_id,
                "members": member_list,
                "created_at": self._ts(),
            }
            if created_by is not None:
                group_data["created_by"] = created_by
            group = Group.model_validate(group_data)
            self._require_no_cycle_after({group_id: group})
            tx = _Tx()
            tx.groups[group_id] = _deep(group)
            self._apply_commit(tx)
            return _deep(group)

    def add_to_group(self, group_id: str, member_id: str) -> Group:
        """Add ``member_id`` to ``group_id``. Idempotent on already-member."""
        self._validate_registry_id(member_id, kind="member")
        with self._atomic_operation():
            group = self._get_group(group_id)
            if group is None:
                raise NotFound(f"group {group_id!r}")
            if member_id in group.members:
                return _deep(group)
            new_members = [*group.members, member_id]
            updated = _validated_update(group, members=new_members)
            self._require_no_cycle_after({group_id: updated})
            tx = _Tx()
            tx.groups[group_id] = _deep(updated)
            self._apply_commit(tx)
            return _deep(updated)

    def remove_from_group(self, group_id: str, member_id: str) -> Group:
        """Remove ``member_id`` from ``group_id``. Idempotent on absent member."""
        with self._atomic_operation():
            group = self._get_group(group_id)
            if group is None:
                raise NotFound(f"group {group_id!r}")
            if member_id not in group.members:
                return _deep(group)
            new_members = [m for m in group.members if m != member_id]
            updated = _validated_update(group, members=new_members)
            tx = _Tx()
            tx.groups[group_id] = _deep(updated)
            self._apply_commit(tx)
            return _deep(updated)

    def delete_group(self, group_id: str) -> None:
        """Delete ``group_id``.

        Other groups that reference it as a member retain the dangling
        reference; resolution simply treats the missing id as ``False``
        per §7.1.
        """
        with self._atomic_operation():
            if self._get_group(group_id) is None:
                raise NotFound(f"group {group_id!r}")
            tx = _Tx()
            tx.group_deletes.add(group_id)
            self._apply_commit(tx)

    def read_group(self, group_id: str) -> Group:
        """Return the group, or raise ``NotFound``."""
        with self._atomic_operation():
            group = self._get_group(group_id)
            if group is None:
                raise NotFound(f"group {group_id!r}")
            return _deep(group)

    def list_groups(self) -> list[Group]:
        """Return all groups (deep copies, sorted by ``group_id``)."""
        with self._atomic_operation():
            return [_deep(g) for g in self._iter_groups()]

    def resolve_worker_in_group(self, worker_id: str, group_id: str) -> bool:
        """Return ``True`` iff ``worker_id`` is transitively in ``group_id``.

        Implements [`spec/v0/02-data-model.md`](../../../../spec/v0/02-data-model.md)
        §7.2: a worker is a member of a group if it appears directly in
        ``members``, or appears in any group that is itself a member
        (transitive closure). Cycles cannot exist (§7.3 forbids them at
        write time), so a topo-walk over the group DAG is safe.

        Per §7.1 "a reference to a non-existent worker / group
        resolves to membership=false", short-circuit when the
        candidate ``worker_id`` is not itself a registered worker:
        an unregistered name in some group's ``members`` does NOT
        make that name a member, even though the literal §7.2 first
        bullet would otherwise admit it. Dangling group references
        in ``members`` are likewise skipped (the walk just doesn't
        descend through them).
        """
        with self._atomic_operation():
            if self._get_worker(worker_id) is None:
                return False
            visited: set[str] = set()
            stack: list[str] = [group_id]
            while stack:
                current = stack.pop()
                if current in visited:
                    continue
                visited.add(current)
                group = self._get_group(current)
                if group is None:
                    # Dangling reference; treat as empty membership.
                    continue
                if worker_id in group.members:
                    return True
                for member in group.members:
                    if self._get_group(member) is not None and member not in visited:
                        stack.append(member)
            return False

    # ------------------------------------------------------------------
    # Registry helpers
    # ------------------------------------------------------------------

    def _validate_registry_id(self, value: str, *, kind: str) -> None:
        """Reject reserved or grammar-violating ids.

        ``kind`` differentiates "worker", "group", "member" only for
        the error message; all three share the §6.1 grammar.
        """
        if value in RESERVED_IDENTIFIERS:
            raise ReservedIdentifier(
                f"{kind} id {value!r} is reserved by the protocol"
            )
        if not _WORKER_ID_RE.fullmatch(value):
            raise InvalidPrecondition(
                f"{kind} id {value!r} does not match the §6.1 grammar"
            )

    def _generate_credential_token(self) -> str:
        """Mint a fresh ≥256-bit registration token (URL-safe hex).

        ``secrets.token_hex(32)`` returns 64 hex chars / 256 bits of
        entropy. Hex is chosen over urlsafe_b64 so the token is safe to
        place after the ``:`` in the bearer format from
        [`spec/v0/07-wire-protocol.md`](../../../../spec/v0/07-wire-protocol.md)
        §13.1 without escape handling — the bearer parser splits on the
        first colon, and hex contains no ``:`` characters.
        """
        return secrets.token_hex(32)

    # argon2id PasswordHasher with the RFC 9106 SECOND-CHOICE-LOW-MEMORY
    # parameters (`time_cost=3, memory_cost=64 MiB, parallelism=4`) —
    # argon2-cffi's defaults as of v23. The slow-KDF properties are
    # cited as the spec posture in chapter 07 §13.1 and chapter 08 §7.
    _PASSWORD_HASHER = PasswordHasher()

    # Dummy hash computed once at class-load so the unknown-worker
    # branch of ``verify_worker_credential`` can perform a real
    # argon2id verify against it (constant-time compared to a hit;
    # see §13.4 / chunk-review item #4).
    _UNKNOWN_WORKER_DUMMY_HASH: str = _PASSWORD_HASHER.hash("eden-unknown-worker-dummy")

    def _hash_credential(self, registration_token: str) -> str:
        """Return an argon2id-encoded hash of ``registration_token``.

        Per chapter 07 §13.1 / chapter 08 §7, the reference backend
        uses argon2id as the credential KDF. The encoded form is the
        standard PHC string (carries algorithm, params, salt, and
        digest together) so a single column stores everything needed
        for verification.
        """
        return self._PASSWORD_HASHER.hash(registration_token)

    def _check_credential_hash(self, registration_token: str, stored: str) -> bool:
        """Verify ``registration_token`` against ``stored`` (argon2id encoded).

        Returns ``True`` on match, ``False`` otherwise.
        ``argon2-cffi``'s verify is itself constant-time (the only
        timing-meaningful difference is the brief decode-fail path for
        a malformed ``stored``; legitimate hashes always reach the KDF
        comparison).
        """
        try:
            return self._PASSWORD_HASHER.verify(stored, registration_token)
        except VerifyMismatchError:
            return False
        except Exception:
            # Malformed stored encoding (corrupted record, wrong
            # column type, etc.). Treat as mismatch rather than
            # propagate; the credential check contract is binary.
            return False

    def _require_no_cycle_after(self, staged_groups: dict[str, Group]) -> None:
        """Raise ``CycleDetected`` if ``staged_groups`` would close a cycle.

        ``staged_groups`` is the post-mutation membership for any groups
        about to be written; persisted groups not in ``staged_groups``
        are read from the store. The DFS treats edges as
        group → group-member; a worker member is a leaf.
        """

        def members_of(gid: str) -> list[str]:
            if gid in staged_groups:
                return list(staged_groups[gid].members)
            persisted = self._get_group(gid)
            return list(persisted.members) if persisted is not None else []

        def dfs(
            node: str,
            visited: set[str],
            on_stack: set[str],
        ) -> bool:
            if node in on_stack:
                return True
            if node in visited:
                return False
            visited.add(node)
            on_stack.add(node)
            for member in members_of(node):
                # Only traverse member ids that name another GROUP,
                # not a worker. A worker member is a leaf in this
                # graph; no group-id edges leave it.
                is_group = (
                    member in staged_groups or self._get_group(member) is not None
                )
                if is_group and dfs(member, visited, on_stack):
                    return True
            on_stack.discard(node)
            return False

        # DFS from every staged group looking for a back-edge to itself
        # or to another node we're currently exploring (a cycle).
        for start in staged_groups:
            if dfs(start, set(), set()):
                raise CycleDetected(
                    f"group mutation on {start!r} would introduce a cycle"
                )

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

    # ------------------------------------------------------------------
    # Portable-checkpoint export / import (12b, `10-checkpoints.md`)
    # ------------------------------------------------------------------

    def export_checkpoint(
        self,
        stream: Any,
        *,
        experiment_config: str | bytes = "",
        repo_bundle: bytes = b"",
        exporter_info: Any | None = None,
    ) -> Any:
        """Write a portable-checkpoint archive of the store's state.

        Delegates to :func:`eden_storage._checkpoint.export_checkpoint`;
        see that function's docstring for the full contract. Runs inside
        :meth:`_atomic_operation` so the snapshot is transactionally
        consistent per ``spec/v0/10-checkpoints.md`` §6.

        Returns the :class:`CheckpointManifest` written into the archive.
        """
        from ._checkpoint import export_checkpoint as _export

        return _export(
            self,
            stream,
            experiment_config=experiment_config,
            repo_bundle=repo_bundle,
            exporter_info=exporter_info,
        )

    def import_checkpoint(
        self,
        stream: Any,
        *,
        as_experiment_id: str | None = None,
        extract_dir: Any | None = None,
    ) -> Any:
        """Bulk-insert a portable-checkpoint archive into the store.

        Delegates to :func:`eden_storage._checkpoint.import_checkpoint`;
        see that function's docstring for the full contract. The store
        MUST be empty (chapter 10 §11 collision rule) and the manifest's
        ``spec_version`` MUST match this binding's
        :data:`CHECKPOINT_SPEC_VERSION`. Returns an
        :class:`ImportResult` carrying the substrate-external pieces the
        caller must wire (experiment_config text, git bundle path,
        artifact digests).
        """
        from ._checkpoint import import_checkpoint as _import

        return _import(
            self,
            stream,
            as_experiment_id=as_experiment_id,
            extract_dir=extract_dir,
        )


def iter_events_by_type(events: Iterable[Event], type_: str) -> Iterator[Event]:
    """Yield events whose ``type`` equals ``type_``. Convenience for tests."""
    for event in events:
        if event.type == type_:
            yield event
