"""Backend-agnostic transition logic shared by every ``Store`` backend.

Every EDEN store backend — in-memory, SQLite, a future Postgres or
remote backend — has to enforce the same state-machine, the same
composite commits (``spec/v0/05-event-protocol.md`` §2.2), the same
token/idempotency/terminal-immutability rules, and the same metrics
validation. Factoring that logic out of any one backend and sharing
it is the only way to make "passes the same conformance suite"
mean what it says.

This module owns the shared core; the per-resource public operations
live in the ``_ops/`` mixin family (issue #114, plan
``docs/plans/refactor-f1-storebase-split.md``):

- ``_StoreCore`` — ``__init__``, the event-id factory, the
  ``_event`` / ``_ts`` / ``_maybe_ts`` helpers, the cross-resource
  read-side predicates (``_require_*`` /
  ``_find_starting_variant_for_implement_task`` /
  ``_validate_display_name`` / ``_validate_member_id`` /
  ``_validate_actor_id``), and the backend-primitive declarations
  every backend overrides. Each mixin in ``_ops/`` inherits this class.
- ``_StoreBase`` — the composite that flattens the mixin MRO atop
  ``_StoreCore``. It owns no methods of its own; the public surface
  (``claim``, ``submit``, ``accept``, ``reject``, ``reclaim``,
  ``create_*``, ``read_*``, ``list_*``, ``events``,
  ``validate_acceptance``, ``validate_terminal``, ``create_idea``,
  ``mark_idea_ready``, ``create_variant``,
  ``declare_variant_evaluation_error``, ``integrate_variant``, the
  worker/group registries, and the experiment-lifecycle ops) is
  inherited from the mixins. Backends subclass ``_StoreBase``.

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

import itertools
import re
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from eden_contracts import (
    ArtifactMetadata,
    EvaluationSchema,
    Event,
    ExecutionTask,
    Experiment,
    Group,
    Idea,
    ImportProvenance,
    Task,
    Variant,
    Worker,
)
from eden_contracts._common import _check_display_name

from .errors import (
    AlreadyExists,
    IllegalTransition,
    InvalidName,
    InvalidPrecondition,
    NotFound,
    ReservedIdentifier,
)
from .submissions import (
    Submission,
)

# Reserved *names* (NAME-space, case-sensitive against NFC form) that
# MUST be rejected by `register_worker` / `register_group` even though
# the display-name grammar admits them. Reserved values moved from
# id-space to name-space in the identity rename (issue #128): opaque
# ids are minted by the store, so they can never collide with a
# reserved literal — the collision surface is the operator-supplied
# `name`. See [`spec/v0/02-data-model.md`](../../../../spec/v0/02-data-model.md)
# §1.7 / §7.
#
# The deployment-admin bearer principal stays the literal token
# ``admin`` and is NOT a registered worker; rejecting ``admin`` as a
# worker *name* keeps the display label from shadowing that principal.
RESERVED_WORKER_NAMES: frozenset[str] = frozenset({"admin", "system", "internal"})
RESERVED_GROUP_NAMES: frozenset[str] = frozenset({"admins", "orchestrators"})

# Member-id grammar (a worker OR a group opaque id) used by
# add-group-member / register_group member validation. The §7.1
# disjoint-namespaces collision is now structurally impossible (ids
# are minted disjoint by prefix); this regex only checks shape.
_MEMBER_ID_RE = re.compile(r"^(wkr|grp)_[0-9a-hjkmnp-tv-z]{26}$")
_ACTOR_ID_RE = re.compile(r"^(admin|wkr_[0-9a-hjkmnp-tv-z]{26})$")

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
    # Baseline-variant seed (`02-data-model.md` §2.5, §9.4). Same
    # two-state one-tuple semantics as ``imported_from_update``: ``None``
    # means "no base_commit_sha write this commit"; a one-tuple wraps the
    # value to write (the inner value MAY be ``None``). Staged on
    # checkpoint import so the field round-trips (`10-checkpoints.md` §5).
    base_commit_sha_update: tuple[str | None] | None = None
    # Artifact metadata rows (issue #166). Keyed by opaque_id. Not bound
    # to any task/idea/variant transition and carry no event — the
    # artifact store is a separate store (`08-storage.md` §5) and a
    # deposit precedes the object that references its URI.
    artifacts: dict[str, ArtifactMetadata] = field(default_factory=dict)






# ----------------------------------------------------------------------
# Helpers for the §3.3 non-no-op variant check (used by
# `_StoreBase._validate_non_no_op_variant`). Split out so each gate of
# the rule reads as a named predicate.
# ----------------------------------------------------------------------












class _StoreCore:
    """Abstract core shared by every store backend.

    Owns ``__init__``, the event-id factory, the ``_event`` /
    ``_ts`` / ``_maybe_ts`` helpers, the cross-resource read-side
    predicates (``_require_*`` / ``_find_starting_variant_for_implement_task``
    / ``_validate_display_name`` / ``_validate_member_id`` /
    ``_validate_actor_id``), and the backend-primitive
    declarations every backend overrides (``_get_*`` / ``_iter_*`` /
    ``_atomic_operation`` / ``_apply_commit`` / ``_get_dispatch_mode``
    / ``_get_experiment``). Each per-resource mixin in ``_ops/``
    inherits this class so its method bodies resolve ``self._get_*``
    / ``self._event`` / ``self._apply_commit`` against these
    declarations; the composite ``_StoreBase`` flattens the mixin
    MRO atop it. See
    [`docs/plans/refactor-f1-storebase-split.md`](../../../../docs/plans/refactor-f1-storebase-split.md)
    (issue #114).
    """

    def __init__(
        self,
        experiment_id: str,
        *,
        name: str | None = None,
        evaluation_schema: EvaluationSchema | None = None,
        now: Callable[[], datetime] | None = None,
        event_id_factory: Callable[[], str] | None = None,
        tree_resolver: Callable[[str], str | None] | None = None,
        base_commit_sha: str | None = None,
    ) -> None:
        self._experiment_id = experiment_id
        # Optional operator-supplied display label for the experiment
        # (issue #128). ``experiment_id`` is supplied externally (minted
        # at setup in a later wave); the store only persists + reads
        # back the name on the wire-visible ``Experiment``. Set ONLY on
        # first creation of the experiment row; ignored on reopen (the
        # persisted name wins, mirroring evaluation_schema immutability).
        self._experiment_name = name
        self._evaluation_schema = evaluation_schema
        # Experiment seed commit (`02-data-model.md` §2.5). Recorded at
        # construction for natively-created experiments; the orchestrator
        # reads it via ``read_experiment`` to create the baseline variant
        # (§9.4). ``None`` when the deployment did not supply a seed (e.g.
        # a pre-field experiment); such experiments never acquire a
        # baseline. Persistent backends store it at experiment init and
        # this attribute mirrors the persisted value.
        self._base_commit_sha = base_commit_sha
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
        if getattr(factory, "__func__", None) is not _StoreCore._default_event_id:
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

    def _get_artifact(self, opaque_id: str) -> ArtifactMetadata | None:
        """Return the artifact metadata row, or ``None`` if absent (issue #166)."""
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

    def _require_task(self, task_id: str) -> Task:
        task = self._get_task(task_id)
        if task is None:
            raise NotFound(f"task {task_id!r}")
        return task

    def _require_no_task(self, task_id: str) -> None:
        if self._get_task(task_id) is not None:
            raise AlreadyExists(f"task {task_id!r}")

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

    def _find_starting_variant_for_implement_task(self, task: Task) -> Variant | None:
        assert isinstance(task, ExecutionTask)
        for variant in self._iter_variants():
            if (
                variant.idea_id == task.payload.idea_id
                and variant.status == "starting"
            ):
                return variant
        return None

    def _validate_display_name(self, name: str, *, kind: str) -> None:
        """Validate an operator-supplied display ``name`` (issue #128).

        ``kind`` is ``"worker"`` or ``"group"`` and selects the reserved-
        name set. Runs the shared NFC / length / whitespace / category
        validator from ``eden_contracts._common`` (the canonical
        display-name grammar in
        [`spec/v0/02-data-model.md`](../../../../spec/v0/02-data-model.md)
        §1.7); a grammar violation raises ``InvalidName`` (the wire binds
        it to 422 ``eden://error/invalid-name``). A well-formed name that
        matches a reserved literal raises ``ReservedIdentifier`` (409
        ``eden://error/reserved-identifier``).

        ``None`` is a no-op — name is optional; the store mints the
        opaque id regardless.
        """
        try:
            _check_display_name(name)
        except ValueError as exc:
            raise InvalidName(
                f"{kind} name {name!r} is not a well-formed display name: {exc}"
            ) from exc
        reserved = (
            RESERVED_WORKER_NAMES if kind == "worker" else RESERVED_GROUP_NAMES
        )
        if name in reserved:
            raise ReservedIdentifier(
                f"{kind} name {name!r} is reserved by the protocol"
            )

    def _validate_member_id(self, value: str) -> None:
        """Reject a member id that is not a well-formed opaque ``wkr_*``/``grp_*``.

        Used by ``register_group`` / ``add_to_group`` to validate the
        shape of each member reference (the §7.1 disjoint-namespaces
        collision is now structurally impossible — ids are minted
        disjoint by prefix — so this is a shape check only). Member
        *existence* is enforced by the caller's cross-namespace resolve.
        """
        if not _MEMBER_ID_RE.fullmatch(value):
            raise InvalidPrecondition(
                f"member id {value!r} does not match the wkr_*/grp_* opaque grammar"
            )

    def _validate_actor_id(self, value: str, *, kind: str) -> None:
        """Reject an actor id that is not ``admin`` or a well-formed ``wkr_*``.

        Used by the ``terminated_by`` / ``updated_by`` / ``reassigned_by``
        fields (ActorId = ``admin`` | opaque ``wkr_*``); see the contract
        §10 actor-id mapping.
        """
        if not _ACTOR_ID_RE.fullmatch(value):
            raise InvalidPrecondition(
                f"{kind} {value!r} does not match the admin|wkr_* actor grammar"
            )


    def resolve_worker_in_group(self, worker_id: str, group_id: str) -> bool:
        """Abstract stub; real body on ``_GroupOpsMixin`` (plan §D.4).

        Declared on the core so cross-mixin callers (notably
        ``_TaskOpsMixin.claim``) resolve the call under pyright; the
        composed backend's MRO routes to ``_GroupOpsMixin``'s
        implementation.
        """
        raise NotImplementedError

    def _validate_evaluation(self, evaluation: dict[str, Any]) -> None:
        """Abstract stub; real body on ``_ExperimentOpsMixin`` (plan §D.4).

        The evaluation-schema validator references the
        experiment-scoped ``self._evaluation_schema``, so its body
        lives on ``_ExperimentOpsMixin``. Declared here so cross-mixin
        callers (``_TaskOpsMixin``'s acceptance validators) resolve
        under pyright; MRO routes to the real implementation.
        """
        raise NotImplementedError


from ._ops.artifacts import _ArtifactOpsMixin  # noqa: E402
from ._ops.events import _EventOpsMixin  # noqa: E402
from ._ops.experiment import _ExperimentOpsMixin  # noqa: E402
from ._ops.groups import _GroupOpsMixin  # noqa: E402
from ._ops.ideas import _IdeaOpsMixin  # noqa: E402
from ._ops.tasks_create import _TaskCreateOpsMixin  # noqa: E402
from ._ops.tasks_lifecycle import _TaskLifecycleOpsMixin  # noqa: E402
from ._ops.variants import _VariantOpsMixin  # noqa: E402
from ._ops.workers import _WorkerOpsMixin  # noqa: E402


class _StoreBase(
    _TaskCreateOpsMixin,
    _TaskLifecycleOpsMixin,
    _IdeaOpsMixin,
    _VariantOpsMixin,
    _ArtifactOpsMixin,
    _EventOpsMixin,
    _ExperimentOpsMixin,
    _WorkerOpsMixin,
    _GroupOpsMixin,
    _StoreCore,
):
    """Composite of every per-resource mixin atop ``_StoreCore``.

    The public surface is unchanged from the pre-refactor monolith;
    every reference backend (``InMemoryStore`` / ``SqliteStore`` /
    ``PostgresStore``) keeps subclassing this class verbatim. MRO
    order is load-bearing: the per-resource seams don't overlap, but
    the abstract stubs on ``_StoreCore`` (``resolve_worker_in_group``,
    ``_validate_evaluation``) MUST be shadowed by the owning mixin,
    which the ordering below guarantees and the module-load
    assertion enforces. See
    [`docs/plans/refactor-f1-storebase-split.md`](../../../../docs/plans/refactor-f1-storebase-split.md)
    (issue #114).
    """


# Module-load-time MRO guard (plan §6 / §8.1): a future bases reorder
# fails loud on first import rather than as a subtle dispatch bug.
assert _StoreBase.__mro__[1:11] == (
    _TaskCreateOpsMixin,
    _TaskLifecycleOpsMixin,
    _IdeaOpsMixin,
    _VariantOpsMixin,
    _ArtifactOpsMixin,
    _EventOpsMixin,
    _ExperimentOpsMixin,
    _WorkerOpsMixin,
    _GroupOpsMixin,
    _StoreCore,
), "unexpected _StoreBase MRO; per-resource mixin order changed"
