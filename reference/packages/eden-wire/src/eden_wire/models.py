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
from eden_contracts._common import CommitSha, DateTimeStr, NotNone, WorkerId
from pydantic import BaseModel, ConfigDict, Field


class _WireBase(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


OptionalDateTime = Annotated[DateTimeStr | None, NotNone]


class ClaimRequest(_WireBase):
    """Body for ``POST /v0/experiments/{E}/tasks/{T}/claim``.

    The claimant ``worker_id`` is taken from the authenticated bearer
    (``07-wire-protocol.md`` §2.3 + §13). The body contains only the
    optional ``expires_at`` timestamp. (12a-1 dropped the body's
    ``worker_id`` field; the bearer is now authoritative.)
    """

    expires_at: OptionalDateTime = None


class ClaimResponse(_WireBase):
    """Body for a successful claim response.

    Mirrors ``schemas/task.schema.json`` §3.4 — no ``token`` field
    since 12a-1 (claim ownership is now identity-keyed).
    """

    worker_id: WorkerId
    claimed_at: DateTimeStr
    expires_at: OptionalDateTime = None


class SubmitRequest(_WireBase):
    """Body for ``POST /v0/experiments/{E}/tasks/{T}/submit``.

    The submitting ``worker_id`` is taken from the authenticated bearer
    (``07-wire-protocol.md`` §2.4 + §13). The body contains only the
    role-specific ``payload``. The Store performs the §4.1 atomic
    claim-match (``WrongClaimant`` / ``NotClaimed``).
    """

    payload: dict[str, Any]


class RejectRequest(_WireBase):
    """Body for ``POST /v0/experiments/{E}/tasks/{T}/reject``."""

    reason: str = Field(pattern=r"^(worker_error|validation_error|policy_limit)$")


class ReclaimRequest(_WireBase):
    """Body for ``POST /v0/experiments/{E}/tasks/{T}/reclaim``."""

    cause: str = Field(pattern=r"^(expired|operator|health_policy)$")


class IntegrateRequest(_WireBase):
    """Body for ``POST /v0/experiments/{E}/variants/{T}/integrate``."""

    variant_commit_sha: CommitSha


class EventsResponse(_WireBase):
    """Body for ``GET /v0/experiments/{E}/events[/subscribe]``."""

    events: list[Event]
    cursor: int = Field(ge=0)


class ValidateTerminalResponse(_WireBase):
    """Body for the ``/_reference/`` validate-terminal helper."""

    decision: str = Field(pattern=r"^(accept|reject_worker|reject_validation)$")
    reason: str | None = None


class ValidateEvaluationRequest(_WireBase):
    """Body for the ``/_reference/`` validate-evaluation helper."""

    evaluation: dict[str, Any]


# ---------------------------------------------------------------------
# Worker registry (12a-1)
# ---------------------------------------------------------------------


class RegisterWorkerRequest(_WireBase):
    """Body for ``POST /v0/experiments/{E}/workers`` (§6.1)."""

    worker_id: WorkerId
    labels: dict[str, str] | None = None


class WorkerRegistration(_WireBase):
    """Response from ``register_worker`` / ``reissue_credential``.

    ``registration_token`` is the freshly-minted plaintext credential
    (returned exactly once); on idempotent re-registration of an
    existing ``worker_id`` it is omitted entirely.
    """

    worker_id: WorkerId
    experiment_id: str = Field(min_length=1)
    registered_at: DateTimeStr
    registered_by: Annotated[str | None, NotNone, Field(min_length=1)] = None
    labels: dict[str, str] | None = None
    registration_token: Annotated[str | None, NotNone, Field(min_length=1)] = None


class WhoamiResponse(_WireBase):
    """Body for ``GET /v0/experiments/{E}/whoami`` (§6.4)."""

    worker_id: WorkerId


# ---------------------------------------------------------------------
# Group registry (12a-1)
# ---------------------------------------------------------------------


class RegisterGroupRequest(_WireBase):
    """Body for ``POST /v0/experiments/{E}/groups`` (§7.1)."""

    group_id: WorkerId  # group ids share the §6.1 grammar
    members: list[WorkerId] | None = None


class AddGroupMemberRequest(_WireBase):
    """Body for ``POST /v0/experiments/{E}/groups/{G}/members`` (§7.2)."""

    member_id: WorkerId


__all__ = [
    "AddGroupMemberRequest",
    "ClaimRequest",
    "ClaimResponse",
    "EventsResponse",
    "IntegrateRequest",
    "ReclaimRequest",
    "RegisterGroupRequest",
    "RegisterWorkerRequest",
    "RejectRequest",
    "SubmitRequest",
    "ValidateEvaluationRequest",
    "ValidateTerminalResponse",
    "WhoamiResponse",
    "WorkerRegistration",
]
