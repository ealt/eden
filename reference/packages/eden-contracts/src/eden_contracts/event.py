"""Event — envelope appended to an EDEN event log.

Mirrors ``spec/v0/schemas/event.schema.json``. The top-level :class:`Event`
accepts any conforming envelope; registered ``type`` values additionally
constrain the ``data`` payload via :data:`RegisteredEventAdapter`, a
discriminated union keyed on ``type``. Unregistered types pass the
envelope check with an open ``data`` object (see spec §3.5).

Semantics live in ``spec/v0/05-event-protocol.md``.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    StringConstraints,
    TypeAdapter,
    model_serializer,
    model_validator,
)

from ._common import CommitSha, DateTimeStr, NotNone
from .config import DispatchModeValue
from .task import TaskTarget

EVENT_TYPE_PATTERN = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$"

# Reserved-id grammar from spec §6.1; used for the actor-id slot in
# attribution-bearing event payloads (``reassigned_by`` / ``updated_by``).
ACTOR_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"

TaskKind = Literal["ideation", "execution", "evaluation"]
FailReason = Literal["worker_error", "validation_error", "policy_limit"]
ReclaimCause = Literal["expired", "operator", "health_policy"]


class Event(BaseModel):
    """Event envelope. The ``data`` payload shape is fixed by ``type`` (§3)."""

    model_config = ConfigDict(strict=True, extra="allow")

    event_id: Annotated[str, Field(min_length=1)]
    type: Annotated[str, StringConstraints(pattern=EVENT_TYPE_PATTERN)]
    occurred_at: DateTimeStr
    experiment_id: Annotated[str, Field(min_length=1)]
    data: dict[str, Any]


class _RegisteredEventBase(BaseModel):
    """Fields shared across every registered-type event."""

    model_config = ConfigDict(strict=True, extra="allow")

    event_id: Annotated[str, Field(min_length=1)]
    occurred_at: DateTimeStr
    experiment_id: Annotated[str, Field(min_length=1)]


class _TaskCreatedData(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")
    task_id: Annotated[str, Field(min_length=1)]
    kind: TaskKind


class TaskCreatedEvent(_RegisteredEventBase):
    """``task.created`` — a new task entered the ``pending`` state."""

    type: Literal["task.created"]
    data: _TaskCreatedData


class _TaskClaimedData(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")
    task_id: Annotated[str, Field(min_length=1)]
    worker_id: Annotated[str, Field(min_length=1)]


class TaskClaimedEvent(_RegisteredEventBase):
    """``task.claimed`` — ``pending → claimed`` by a specific worker."""

    type: Literal["task.claimed"]
    data: _TaskClaimedData


class _TaskIdOnlyData(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")
    task_id: Annotated[str, Field(min_length=1)]


class TaskSubmittedEvent(_RegisteredEventBase):
    """``task.submitted`` — ``claimed → submitted`` by the claim-holder."""

    type: Literal["task.submitted"]
    data: _TaskIdOnlyData


class TaskCompletedEvent(_RegisteredEventBase):
    """``task.completed`` — ``submitted → completed`` by the orchestrator."""

    type: Literal["task.completed"]
    data: _TaskIdOnlyData


class _TaskFailedData(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")
    task_id: Annotated[str, Field(min_length=1)]
    reason: FailReason


class TaskFailedEvent(_RegisteredEventBase):
    """``task.failed`` — ``submitted → failed`` with a closed-set reason."""

    type: Literal["task.failed"]
    data: _TaskFailedData


class _TaskReclaimedData(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")
    task_id: Annotated[str, Field(min_length=1)]
    cause: ReclaimCause


class TaskReclaimedEvent(_RegisteredEventBase):
    """``task.reclaimed`` — ``claimed``/``submitted → pending`` with a cause."""

    type: Literal["task.reclaimed"]
    data: _TaskReclaimedData


class _TaskReassignedData(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")
    task_id: Annotated[str, Field(min_length=1)]
    # `new_target` is exactly the post-reassign value of `task.target`:
    # absent (any-worker) is encoded as JSON null, a worker / group
    # target as the standard `TaskTarget` shape.
    new_target: TaskTarget | None
    reason: Annotated[str, Field(min_length=1)]
    reassigned_by: Annotated[str, StringConstraints(pattern=ACTOR_ID_PATTERN)]

    # The event schema lists ``new_target`` in the required-set with a
    # nullable type. A bare ``model_dump(exclude_none=True)`` would
    # silently drop a ``null`` value and produce schema-invalid JSON;
    # the wrap-mode serializer below restores the key when the outer
    # dump's ``exclude_none=True`` filtered it out.
    @model_serializer(mode="wrap")
    def _keep_new_target_key(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        result = handler(self)
        if "new_target" not in result:
            result["new_target"] = None
        return result


class TaskReassignedEvent(_RegisteredEventBase):
    """``task.reassigned`` — operator-driven update of ``task.target``.

    Composite-commit per ``05-event-protocol.md`` §2.2: against a
    claimed task this event fires atomically with ``task.reclaimed``
    (``cause=operator``); against a pending task only this event
    fires.
    """

    type: Literal["task.reassigned"]
    data: _TaskReassignedData


class _ExperimentDispatchModeChangedData(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")
    # `dispatch_mode` is the post-update full state; `changed` is the
    # subset of keys actually mutated this call (the "diff"). Two
    # separate fields keep replay logic simple — consumers that only
    # care about "what is it now" read `dispatch_mode`; consumers that
    # need "what just flipped" read `changed`.
    dispatch_mode: dict[str, DispatchModeValue]
    changed: dict[str, DispatchModeValue]
    updated_by: Annotated[str, StringConstraints(pattern=ACTOR_ID_PATTERN)]


class ExperimentDispatchModeChangedEvent(_RegisteredEventBase):
    """``experiment.dispatch_mode_changed`` — admin flipped one or more keys."""

    type: Literal["experiment.dispatch_mode_changed"]
    data: _ExperimentDispatchModeChangedData


class _ExperimentTerminatedData(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")
    reason: str
    terminated_by: Annotated[str, StringConstraints(pattern=ACTOR_ID_PATTERN)]


class ExperimentTerminatedEvent(_RegisteredEventBase):
    """``experiment.terminated`` — ``running → terminated`` lifecycle transition.

    Emitted atomically with the state-field update per
    ``04-task-protocol.md`` §8.1. Both the operator wire op
    (``POST /v0/experiments/{E}/terminate``) and the orchestrator's
    policy-driven termination decision (``03-roles.md`` §6.2
    decision-type 0) route through the same Store-level
    ``terminate_experiment`` op; the winning call's ``reason`` is the
    one recorded (idempotent on already-terminated state).
    """

    type: Literal["experiment.terminated"]
    data: _ExperimentTerminatedData


class _ExperimentPolicyErrorData(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")
    policy_kind: Annotated[str, Field(min_length=1)]
    error_type: Annotated[str, Field(min_length=1)]
    error_message: str


class ExperimentPolicyErrorEvent(_RegisteredEventBase):
    """``experiment.policy_error`` — an orchestrator policy callable raised.

    Recorded so operators see the failure in the event log. Exempt from
    the ``05-event-protocol.md`` §2 transactional invariant: no
    protocol-owned state mutation pairs with it. v0 defines only
    ``policy_kind == "termination"``; the field is open so future
    decision types that introduce policy callables can reuse the
    event type.
    """

    type: Literal["experiment.policy_error"]
    data: _ExperimentPolicyErrorData


class _IdeaIdOnlyData(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")
    idea_id: Annotated[str, Field(min_length=1)]


class IdeaDraftedEvent(_RegisteredEventBase):
    """``idea.drafted`` — an idea entered the ``drafting`` state."""

    type: Literal["idea.drafted"]
    data: _IdeaIdOnlyData


class IdeaReadyEvent(_RegisteredEventBase):
    """``idea.ready`` — ``drafting → ready`` (ideator-declared)."""

    type: Literal["idea.ready"]
    data: _IdeaIdOnlyData


class _IdeaAndTaskData(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")
    idea_id: Annotated[str, Field(min_length=1)]
    task_id: Annotated[str, Field(min_length=1)]


class IdeaDispatchedEvent(_RegisteredEventBase):
    """``idea.dispatched`` — ``ready → dispatched`` with its execution task."""

    type: Literal["idea.dispatched"]
    data: _IdeaAndTaskData


class IdeaCompletedEvent(_RegisteredEventBase):
    """``idea.completed`` — ``dispatched → completed`` with its execution task."""

    type: Literal["idea.completed"]
    data: _IdeaAndTaskData


class _VariantStartedData(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")
    variant_id: Annotated[str, Field(min_length=1)]
    idea_id: Annotated[str | None, NotNone, Field(min_length=1)] = None
    """The producing idea. Absent for a ``kind == "baseline"`` variant, which
    has no producing idea (``05-event-protocol.md`` §3.3, ``02-data-model.md``
    §9.4)."""
    kind: Annotated[Literal["baseline"] | None, NotNone] = None
    """REQUIRED when the variant is a baseline so event-only subscribers get an
    explicit signal rather than inferring from a missing ``idea_id``; absent for
    an ordinary variant. Enforced by ``_kind_or_idea_id_present`` below."""

    @model_validator(mode="after")
    def _kind_or_idea_id_present(self) -> _VariantStartedData:
        # An ordinary variant.started carries idea_id; a baseline carries
        # kind == "baseline" and omits idea_id (05-event-protocol.md §3.3).
        if self.kind == "baseline":
            return self
        if self.idea_id is None:
            raise ValueError(
                "variant.started payload requires idea_id unless kind == 'baseline'"
            )
        return self


class VariantStartedEvent(_RegisteredEventBase):
    """``variant.started`` — a variant entered ``starting`` under an idea."""

    type: Literal["variant.started"]
    data: _VariantStartedData


class _VariantSucceededData(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")
    variant_id: Annotated[str, Field(min_length=1)]
    commit_sha: CommitSha


class VariantSucceededEvent(_RegisteredEventBase):
    """``variant.succeeded`` — ``starting → success`` with the measured commit."""

    type: Literal["variant.succeeded"]
    data: _VariantSucceededData


class _VariantIdOnlyData(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")
    variant_id: Annotated[str, Field(min_length=1)]


class VariantErroredEvent(_RegisteredEventBase):
    """``variant.errored`` — ``starting → error`` (worker-declared)."""

    type: Literal["variant.errored"]
    data: _VariantIdOnlyData


class VariantEvaluationErroredEvent(_RegisteredEventBase):
    """``variant.evaluation_errored`` — ``starting → evaluation_error`` (retry-exhausted)."""

    type: Literal["variant.evaluation_errored"]
    data: _VariantIdOnlyData


class _VariantIntegratedData(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")
    variant_id: Annotated[str, Field(min_length=1)]
    variant_commit_sha: CommitSha


class VariantIntegratedEvent(_RegisteredEventBase):
    """``variant.integrated`` — integrator wrote ``variant_commit_sha`` for the variant."""

    type: Literal["variant.integrated"]
    data: _VariantIntegratedData


RegisteredEvent = Annotated[
    TaskCreatedEvent
    | TaskClaimedEvent
    | TaskSubmittedEvent
    | TaskCompletedEvent
    | TaskFailedEvent
    | TaskReclaimedEvent
    | TaskReassignedEvent
    | ExperimentDispatchModeChangedEvent
    | ExperimentTerminatedEvent
    | ExperimentPolicyErrorEvent
    | IdeaDraftedEvent
    | IdeaReadyEvent
    | IdeaDispatchedEvent
    | IdeaCompletedEvent
    | VariantStartedEvent
    | VariantSucceededEvent
    | VariantErroredEvent
    | VariantEvaluationErroredEvent
    | VariantIntegratedEvent,
    Field(discriminator="type"),
]
"""Discriminated union of registered event types (spec §3.1–§3.3)."""

RegisteredEventAdapter: TypeAdapter[RegisteredEvent] = TypeAdapter(RegisteredEvent)
"""Pydantic adapter for validating a payload as a registered-type event."""

REGISTERED_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "task.created",
        "task.claimed",
        "task.submitted",
        "task.completed",
        "task.failed",
        "task.reclaimed",
        "task.reassigned",
        "experiment.dispatch_mode_changed",
        "experiment.terminated",
        "experiment.policy_error",
        "idea.drafted",
        "idea.ready",
        "idea.dispatched",
        "idea.completed",
        "variant.started",
        "variant.succeeded",
        "variant.errored",
        "variant.evaluation_errored",
        "variant.integrated",
    }
)
"""The v0 normative event registry (spec §3.1–§3.4)."""


__all__ = [
    "ACTOR_ID_PATTERN",
    "EVENT_TYPE_PATTERN",
    "Event",
    "ExperimentDispatchModeChangedEvent",
    "ExperimentPolicyErrorEvent",
    "ExperimentTerminatedEvent",
    "FailReason",
    "IdeaCompletedEvent",
    "IdeaDispatchedEvent",
    "IdeaDraftedEvent",
    "IdeaReadyEvent",
    "REGISTERED_EVENT_TYPES",
    "ReclaimCause",
    "RegisteredEvent",
    "RegisteredEventAdapter",
    "TaskClaimedEvent",
    "TaskCompletedEvent",
    "TaskCreatedEvent",
    "TaskFailedEvent",
    "TaskKind",
    "TaskReassignedEvent",
    "TaskReclaimedEvent",
    "TaskSubmittedEvent",
    "VariantErroredEvent",
    "VariantEvaluationErroredEvent",
    "VariantIntegratedEvent",
    "VariantStartedEvent",
    "VariantSucceededEvent",
]
