"""Proposal — planner output; implementation-work dispatch metadata.

Mirrors ``spec/v0/schemas/proposal.schema.json``. Lifecycle semantics are
in spec/v0/04-task-protocol.md.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from ._common import CommitSha, DateTimeStr, UriStr

ProposalState = Literal["drafting", "ready", "dispatched", "completed"]
"""Proposal lifecycle states; ``completed`` is terminal and covers both success and failure."""

SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]*$"
Slug = Annotated[str, StringConstraints(pattern=SLUG_PATTERN)]


class Proposal(BaseModel):
    """Planner-produced proposal."""

    model_config = ConfigDict(strict=True, extra="allow")

    proposal_id: Annotated[str, Field(min_length=1)]
    experiment_id: Annotated[str, Field(min_length=1)]
    slug: Slug
    priority: float
    parent_commits: Annotated[list[CommitSha], Field(min_length=1)]
    artifacts_uri: UriStr
    state: ProposalState
    created_at: DateTimeStr
