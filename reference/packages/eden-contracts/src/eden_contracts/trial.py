"""Trial — one in-flight or completed attempt to improve the objective.

Mirrors ``spec/v0/schemas/trial.schema.json``. The ``metrics`` object's shape
is constrained by the experiment's ``metrics_schema``; this model does not
reproduce that constraint because metrics keys are per-experiment.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from ._common import CommitSha, DateTimeStr, NotNone, UriStr

TrialStatus = Literal["starting", "success", "error", "eval_error"]
"""Trial lifecycle statuses; ``success``, ``error``, ``eval_error`` are terminal."""

WORK_BRANCH_PATTERN = r"^work/.+$"
WorkBranch = Annotated[str, StringConstraints(pattern=WORK_BRANCH_PATTERN)]


class Trial(BaseModel):
    """One attempt, in flight or terminal."""

    model_config = ConfigDict(strict=True, extra="allow")

    trial_id: Annotated[str, Field(min_length=1)]
    experiment_id: Annotated[str, Field(min_length=1)]
    proposal_id: Annotated[str, Field(min_length=1)]
    status: TrialStatus
    parent_commits: Annotated[list[CommitSha], Field(min_length=1)]
    branch: Annotated[WorkBranch | None, NotNone] = None
    commit_sha: Annotated[CommitSha | None, NotNone] = None
    trial_commit_sha: Annotated[CommitSha | None, NotNone] = None
    artifacts_uri: Annotated[UriStr | None, NotNone] = None
    description: Annotated[str | None, NotNone] = None
    metrics: Annotated[dict[str, Any] | None, NotNone] = None
    started_at: DateTimeStr
    completed_at: Annotated[DateTimeStr | None, NotNone] = None
