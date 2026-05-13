"""Variant — one in-flight or completed attempt to improve the objective.

Mirrors ``spec/v0/schemas/variant.schema.json``. The ``evaluation`` object's
shape is constrained by the experiment's ``evaluation_schema``; this model does
not reproduce that constraint because evaluation keys are per-experiment.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from ._common import CommitSha, DateTimeStr, NotNone, UriStr, WorkerId

VariantStatus = Literal["starting", "success", "error", "evaluation_error"]
"""Variant lifecycle statuses; ``success``, ``error``, ``evaluation_error`` are terminal."""

WORK_BRANCH_PATTERN = r"^work/.+$"
WorkBranch = Annotated[str, StringConstraints(pattern=WORK_BRANCH_PATTERN)]


class Variant(BaseModel):
    """One attempt, in flight or terminal."""

    model_config = ConfigDict(strict=True, extra="allow")

    variant_id: Annotated[str, Field(min_length=1)]
    experiment_id: Annotated[str, Field(min_length=1)]
    idea_id: Annotated[str, Field(min_length=1)]
    status: VariantStatus
    parent_commits: Annotated[list[CommitSha], Field(min_length=1)]
    branch: Annotated[WorkBranch | None, NotNone] = None
    commit_sha: Annotated[CommitSha | None, NotNone] = None
    variant_commit_sha: Annotated[CommitSha | None, NotNone] = None
    artifacts_uri: Annotated[UriStr | None, NotNone] = None
    description: Annotated[str | None, NotNone] = None
    evaluation: Annotated[dict[str, Any] | None, NotNone] = None
    started_at: DateTimeStr
    completed_at: Annotated[DateTimeStr | None, NotNone] = None
    executed_by: Annotated[WorkerId | None, NotNone] = None
    evaluated_by: Annotated[WorkerId | None, NotNone] = None
