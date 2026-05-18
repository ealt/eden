"""Idea — ideator output; implementation-work dispatch metadata.

Mirrors ``spec/v0/schemas/idea.schema.json``. Lifecycle semantics are
in spec/v0/04-task-protocol.md.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from ._common import CommitSha, DateTimeStr, NotNone, UriStr, WorkerId
from .task import TaskTarget

IdeaState = Literal["drafting", "ready", "dispatched", "completed"]
"""Idea lifecycle states; ``completed`` is terminal and covers both success and failure."""

SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]*$"
Slug = Annotated[str, StringConstraints(pattern=SLUG_PATTERN)]


class Idea(BaseModel):
    """Ideator-produced idea."""

    model_config = ConfigDict(strict=True, extra="allow")

    idea_id: Annotated[str, Field(min_length=1)]
    experiment_id: Annotated[str, Field(min_length=1)]
    slug: Slug
    priority: float
    parent_commits: Annotated[list[CommitSha], Field(min_length=1)]
    artifacts_uri: UriStr
    state: IdeaState
    created_at: DateTimeStr
    created_by: Annotated[WorkerId | None, NotNone] = None
    # 12a-3 routing hint: when the ideator names a preferred executor
    # (worker or group), the orchestrator's execution_dispatch decision
    # copies it to the resulting execution task's ``target`` field per
    # ``03-roles.md`` §6.2 decision-type 2. Absent (or admin-overridden
    # via ``create_task``'s body-level ``target``) means "any registered executor".
    intended_executor: Annotated[TaskTarget | None, NotNone] = None
