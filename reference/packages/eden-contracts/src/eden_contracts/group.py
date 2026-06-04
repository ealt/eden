"""Group — named set of workers and/or other groups within an experiment.

Mirrors ``spec/v0/schemas/group.schema.json``. Membership resolves
transitively (spec/v0/02-data-model.md §7.2); cycles MUST be rejected
at write time by the store.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict

from ._common import ActorId, DateTimeStr, DisplayName, ExperimentId, GroupId, MemberId, NotNone

GroupMember = MemberId
"""A group member is either a `worker_id` (wkr_*) or a `group_id` (grp_*)."""


class Group(BaseModel):
    """A named, recursively-resolved set of workers and groups."""

    model_config = ConfigDict(strict=True, extra="allow")

    group_id: GroupId
    name: Annotated[DisplayName | None, NotNone] = None
    experiment_id: ExperimentId
    members: list[GroupMember]
    created_at: DateTimeStr
    created_by: Annotated[ActorId | None, NotNone] = None
