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

TaskKind = Literal["plan", "implement", "evaluate"]
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


class _ProposalIdOnlyData(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")
    proposal_id: Annotated[str, Field(min_length=1)]


class ProposalDraftedEvent(_RegisteredEventBase):
    """``proposal.drafted`` — a proposal entered the ``drafting`` state."""

    type: Literal["proposal.drafted"]
    data: _ProposalIdOnlyData


class ProposalReadyEvent(_RegisteredEventBase):
    """``proposal.ready`` — ``drafting → ready`` (planner-declared)."""

    type: Literal["proposal.ready"]
    data: _ProposalIdOnlyData


class _ProposalAndTaskData(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")
    proposal_id: Annotated[str, Field(min_length=1)]
    task_id: Annotated[str, Field(min_length=1)]


class ProposalDispatchedEvent(_RegisteredEventBase):
    """``proposal.dispatched`` — ``ready → dispatched`` with its implement task."""

    type: Literal["proposal.dispatched"]
    data: _ProposalAndTaskData


class ProposalCompletedEvent(_RegisteredEventBase):
    """``proposal.completed`` — ``dispatched → completed`` with its implement task."""

    type: Literal["proposal.completed"]
    data: _ProposalAndTaskData


class _TrialStartedData(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")
    trial_id: Annotated[str, Field(min_length=1)]
    proposal_id: Annotated[str, Field(min_length=1)]


class TrialStartedEvent(_RegisteredEventBase):
    """``trial.started`` — a trial entered ``starting`` under a proposal."""

    type: Literal["trial.started"]
    data: _TrialStartedData


class _TrialSucceededData(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")
    trial_id: Annotated[str, Field(min_length=1)]
    commit_sha: CommitSha


class TrialSucceededEvent(_RegisteredEventBase):
    """``trial.succeeded`` — ``starting → success`` with the measured commit."""

    type: Literal["trial.succeeded"]
    data: _TrialSucceededData


class _TrialIdOnlyData(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")
    trial_id: Annotated[str, Field(min_length=1)]


class TrialErroredEvent(_RegisteredEventBase):
    """``trial.errored`` — ``starting → error`` (worker-declared)."""

    type: Literal["trial.errored"]
    data: _TrialIdOnlyData


class TrialEvalErroredEvent(_RegisteredEventBase):
    """``trial.eval_errored`` — ``starting → eval_error`` (retry-exhausted)."""

    type: Literal["trial.eval_errored"]
    data: _TrialIdOnlyData


class _TrialIntegratedData(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")
    trial_id: Annotated[str, Field(min_length=1)]
    trial_commit_sha: CommitSha


class TrialIntegratedEvent(_RegisteredEventBase):
    """``trial.integrated`` — integrator wrote ``trial_commit_sha`` for the trial."""

    type: Literal["trial.integrated"]
    data: _TrialIntegratedData


RegisteredEvent = Annotated[
    TaskCreatedEvent
    | TaskClaimedEvent
    | TaskSubmittedEvent
    | TaskCompletedEvent
    | TaskFailedEvent
    | TaskReclaimedEvent
    | ProposalDraftedEvent
    | ProposalReadyEvent
    | ProposalDispatchedEvent
    | ProposalCompletedEvent
    | TrialStartedEvent
    | TrialSucceededEvent
    | TrialErroredEvent
    | TrialEvalErroredEvent
    | TrialIntegratedEvent,
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
        "proposal.drafted",
        "proposal.ready",
        "proposal.dispatched",
        "proposal.completed",
        "trial.started",
        "trial.succeeded",
        "trial.errored",
        "trial.eval_errored",
        "trial.integrated",
    }
)
"""The v0 normative event registry (spec §3.1–§3.3)."""
