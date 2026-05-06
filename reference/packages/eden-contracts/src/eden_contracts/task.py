"""Task — unit of work dispatched to a role.

Mirrors ``spec/v0/schemas/task.schema.json``. The ``kind`` field
discriminates the payload shape, and the ``claim`` field is present iff
``state`` is ``claimed`` or ``submitted`` (the state-dependent presence
is enforced by a model validator because JSON Schema expresses it via
``if/then/else`` at the root).

State-machine semantics are defined in spec/v0/04-task-protocol.md.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from ._common import DateTimeStr, NotNone

TaskKind = Literal["ideation", "execution", "evaluation"]
TaskState = Literal["pending", "claimed", "submitted", "completed", "failed"]

_CLAIM_STATES: frozenset[str] = frozenset({"claimed", "submitted"})


class TaskClaim(BaseModel):
    """Claim token issued by the task store when a worker claims a task."""

    model_config = ConfigDict(strict=True, extra="allow")

    token: Annotated[str, Field(min_length=1)]
    worker_id: Annotated[str, Field(min_length=1)]
    claimed_at: DateTimeStr
    expires_at: Annotated[DateTimeStr | None, NotNone] = None


class IdeationPayload(BaseModel):
    """Payload for ``kind=ideation`` tasks."""

    model_config = ConfigDict(strict=True, extra="allow")

    experiment_id: Annotated[str, Field(min_length=1)]


class ExecutionPayload(BaseModel):
    """Payload for ``kind=execution`` tasks."""

    model_config = ConfigDict(strict=True, extra="allow")

    idea_id: Annotated[str, Field(min_length=1)]


class EvaluationPayload(BaseModel):
    """Payload for ``kind=evaluation`` tasks."""

    model_config = ConfigDict(strict=True, extra="allow")

    variant_id: Annotated[str, Field(min_length=1)]


class _TaskBase(BaseModel):
    """Fields shared across every task kind."""

    model_config = ConfigDict(strict=True, extra="allow")

    task_id: Annotated[str, Field(min_length=1)]
    state: TaskState
    claim: Annotated[TaskClaim | None, NotNone] = None
    created_at: DateTimeStr
    updated_at: DateTimeStr

    @model_validator(mode="after")
    def _claim_presence(self) -> Self:
        if self.state in _CLAIM_STATES:
            if self.claim is None:
                raise ValueError(
                    f"claim is required when state is {self.state!r}"
                )
        elif self.claim is not None:
            raise ValueError(
                f"claim is forbidden when state is {self.state!r}"
            )
        return self


class IdeationTask(_TaskBase):
    """Task of kind ``ideation``."""

    kind: Literal["ideation"]
    payload: IdeationPayload


class ExecutionTask(_TaskBase):
    """Task of kind ``execution``."""

    kind: Literal["execution"]
    payload: ExecutionPayload


class EvaluationTask(_TaskBase):
    """Task of kind ``evaluation``."""

    kind: Literal["evaluation"]
    payload: EvaluationPayload


Task = Annotated[
    IdeationTask | ExecutionTask | EvaluationTask,
    Field(discriminator="kind"),
]
"""Discriminated union keyed by ``kind`` — use :data:`TaskAdapter` to validate untyped input."""

TaskAdapter: TypeAdapter[Task] = TypeAdapter(Task)
"""Pydantic adapter for validating arbitrary task objects into the union."""
