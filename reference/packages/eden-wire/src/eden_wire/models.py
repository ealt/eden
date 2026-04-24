"""Pydantic request/response types for the EDEN wire binding.

Every model here matches one of the JSON Schemas under
``spec/v0/schemas/wire/*.schema.json`` and is round-trip-validated
against that schema in the wire-schema parity tests. Shape drift
between the schema and the model surfaces as a test failure.

The models apply the same ``strict=True`` / ``NotNone`` /
``DateTimeStr`` discipline as ``eden_contracts``.
"""

from __future__ import annotations

from typing import Annotated, Any

from eden_contracts import Event
from eden_contracts._common import CommitSha, DateTimeStr, NotNone
from pydantic import BaseModel, ConfigDict, Field


class _WireBase(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


OptionalDateTime = Annotated[DateTimeStr | None, NotNone]


class ClaimRequest(_WireBase):
    """Body for ``POST /v0/experiments/{E}/tasks/{T}/claim``."""

    worker_id: str = Field(min_length=1)
    expires_at: OptionalDateTime = None


class ClaimResponse(_WireBase):
    """Body for a successful claim response."""

    token: str = Field(min_length=1)
    worker_id: str = Field(min_length=1)
    claimed_at: DateTimeStr
    expires_at: OptionalDateTime = None


class SubmitRequest(_WireBase):
    """Body for ``POST /v0/experiments/{E}/tasks/{T}/submit``."""

    token: str = Field(min_length=1)
    payload: dict[str, Any]


class RejectRequest(_WireBase):
    """Body for ``POST /v0/experiments/{E}/tasks/{T}/reject``."""

    reason: str = Field(pattern=r"^(worker_error|validation_error|policy_limit)$")


class ReclaimRequest(_WireBase):
    """Body for ``POST /v0/experiments/{E}/tasks/{T}/reclaim``."""

    cause: str = Field(pattern=r"^(expired|operator|health_policy)$")


class IntegrateRequest(_WireBase):
    """Body for ``POST /v0/experiments/{E}/trials/{T}/integrate``."""

    trial_commit_sha: CommitSha


class EventsResponse(_WireBase):
    """Body for ``GET /v0/experiments/{E}/events[/subscribe]``."""

    events: list[Event]
    cursor: int = Field(ge=0)


class ValidateTerminalResponse(_WireBase):
    """Body for the ``/_reference/`` validate-terminal helper."""

    decision: str = Field(pattern=r"^(accept|reject_worker|reject_validation)$")
    reason: str | None = None


class ValidateMetricsRequest(_WireBase):
    """Body for the ``/_reference/`` validate-metrics helper."""

    metrics: dict[str, Any]


__all__ = [
    "ClaimRequest",
    "ClaimResponse",
    "EventsResponse",
    "IntegrateRequest",
    "ReclaimRequest",
    "RejectRequest",
    "SubmitRequest",
    "ValidateMetricsRequest",
    "ValidateTerminalResponse",
]
