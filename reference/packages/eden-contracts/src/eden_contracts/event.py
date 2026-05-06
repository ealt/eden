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

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter

from ._common import CommitSha, DateTimeStr

EVENT_TYPE_PATTERN = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$"

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
    """``idea.dispatched`` — ``ready → dispatched`` with its execute task."""

    type: Literal["idea.dispatched"]
    data: _IdeaAndTaskData


class IdeaCompletedEvent(_RegisteredEventBase):
    """``idea.completed`` — ``dispatched → completed`` with its execute task."""

    type: Literal["idea.completed"]
    data: _IdeaAndTaskData


class _VariantStartedData(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")
    variant_id: Annotated[str, Field(min_length=1)]
    idea_id: Annotated[str, Field(min_length=1)]


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


class VariantEvalErroredEvent(_RegisteredEventBase):
    """``variant.eval_errored`` — ``starting → eval_error`` (retry-exhausted)."""

    type: Literal["variant.eval_errored"]
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
    | IdeaDraftedEvent
    | IdeaReadyEvent
    | IdeaDispatchedEvent
    | IdeaCompletedEvent
    | VariantStartedEvent
    | VariantSucceededEvent
    | VariantErroredEvent
    | VariantEvalErroredEvent
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
        "idea.drafted",
        "idea.ready",
        "idea.dispatched",
        "idea.completed",
        "variant.started",
        "variant.succeeded",
        "variant.errored",
        "variant.eval_errored",
        "variant.integrated",
    }
)
"""The v0 normative event registry (spec §3.1–§3.3)."""
