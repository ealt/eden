"""Group — named set of workers and/or other groups within an experiment.

Mirrors ``spec/v0/schemas/group.schema.json``. Membership resolves
transitively (spec/v0/02-data-model.md §7.2); cycles MUST be rejected
at write time by the store.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from ._common import DateTimeStr, NotNone, WorkerId

GroupMember = WorkerId
"""A group member is either a `worker_id` or another `group_id`; same grammar."""


class Group(BaseModel):
    """A named, recursively-resolved set of workers and groups."""

    model_config = ConfigDict(strict=True, extra="allow")

    group_id: WorkerId
    experiment_id: Annotated[str, Field(min_length=1)]
    members: list[GroupMember]
    created_at: DateTimeStr
    created_by: Annotated[str | None, NotNone, Field(min_length=1)] = None
