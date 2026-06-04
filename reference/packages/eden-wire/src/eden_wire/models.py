"""Pydantic request/response types for the EDEN wire binding.

Every model here matches one of the JSON Schemas under
``spec/v0/schemas/wire/*.schema.json`` and is round-trip-validated
against that schema in the wire-schema parity tests. Shape drift
between the schema and the model surfaces as a test failure.

The models apply the same ``strict=True`` / ``NotNone`` /
``DateTimeStr`` discipline as ``eden_contracts``.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from eden_contracts import DispatchModeValue, Event, TaskTarget
from eden_contracts._common import (
    ActorId,
    CommitSha,
    DateTimeStr,
    DisplayName,
    ExperimentId,
    MemberId,
    NotNone,
    WorkerId,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
)


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
    """Body for ``POST /v0/experiments/{E}/workers`` (§6.1).

    The server mints the opaque ``worker_id``; the caller supplies only
    an OPTIONAL display ``name`` and deployment ``labels``. Reserved
    worker names (``admin`` / ``system`` / ``internal``) are rejected by
    the Store.

    The ``name`` field is a plain string here (NOT ``DisplayName``) so an
    ill-formed name parses the request body and reaches the Store's
    ``_validate_display_name``, which raises ``InvalidName`` → 422
    ``eden://error/invalid-name`` (``07-wire-protocol.md`` §6.1,
    ``02-data-model.md`` §1.7). Typing it ``DisplayName`` here would fail
    Pydantic request validation → 400 ``bad-request``, the wrong status.
    """

    name: Annotated[str | None, NotNone] = None
    labels: Annotated[dict[str, str] | None, NotNone] = None


class WorkerRegistration(_WireBase):
    """Response from ``register_worker`` / ``reissue_credential``.

    ``registration_token`` is the freshly-minted plaintext credential
    (returned exactly once). Since #128 every ``register_worker`` mints a
    new worker, so it is always present on a register response; it is
    omitted only on the ``reissue_credential`` response shape's reuse
    paths where no new token was issued.
    """

    worker_id: WorkerId
    name: Annotated[DisplayName | None, NotNone] = None
    experiment_id: ExperimentId
    registered_at: DateTimeStr
    registered_by: Annotated[ActorId | None, NotNone] = None
    labels: dict[str, str] | None = None
    registration_token: Annotated[str | None, NotNone, Field(min_length=1)] = None


class WhoamiResponse(_WireBase):
    """Body for ``GET /v0/experiments/{E}/whoami`` (§6.4)."""

    worker_id: WorkerId
    name: Annotated[DisplayName | None, NotNone] = None


# ---------------------------------------------------------------------
# Group registry (12a-1)
# ---------------------------------------------------------------------


class RegisterGroupRequest(_WireBase):
    """Body for ``POST /v0/experiments/{E}/groups`` (§7.1).

    The server mints the opaque ``group_id``; the caller supplies only
    an OPTIONAL display ``name`` and an OPTIONAL initial ``members``
    list (each a worker_id or group_id). Reserved group names
    (``admins`` / ``orchestrators``) are rejected by the Store unless
    the caller is the deployment admin (the setup-experiment bootstrap
    path).

    The ``name`` field is a plain string here (NOT ``DisplayName``) so an
    ill-formed name parses the request body and reaches the Store's
    ``_validate_display_name``, which raises ``InvalidName`` → 422
    ``eden://error/invalid-name`` (``07-wire-protocol.md`` §6.1,
    ``02-data-model.md`` §1.7). Typing it ``DisplayName`` here would fail
    Pydantic request validation → 400 ``bad-request``, the wrong status.
    """

    name: Annotated[str | None, NotNone] = None
    members: Annotated[list[MemberId] | None, NotNone] = None


class AddGroupMemberRequest(_WireBase):
    """Body for ``POST /v0/experiments/{E}/groups/{G}/members`` (§7.2)."""

    member_id: MemberId


# ---------------------------------------------------------------------
# Reassign / dispatch_mode (12a-2 wave 3)
# ---------------------------------------------------------------------


class ReassignRequest(_WireBase):
    """Body for ``POST /v0/experiments/{E}/tasks/{T}/reassign`` (§2.7).

    ``new_target`` is the post-reassign value of ``task.target``: ``None``
    (encoded as JSON ``null``) opens the task to any registered worker;
    a ``TaskTarget`` scopes it to a worker or group. ``reason`` is a
    free-form audit string carried into the ``task.reassigned`` event.

    The server stamps ``reassigned_by`` from the authenticated principal
    per [`spec/v0/07-wire-protocol.md`](../../../../spec/v0/07-wire-protocol.md)
    §2.7 / §13.3, so it is NOT carried in the request body.
    """

    new_target: TaskTarget | None
    reason: Annotated[str, Field(min_length=1)]

    # ``new_target`` is required-nullable on the wire (the schema's
    # ``required`` list pins it). ``model_dump(exclude_none=True)`` would
    # drop the ``null`` value; the wrap serializer below restores the
    # key. Same shape as ``_TaskReassignedData`` in eden_contracts.
    @model_serializer(mode="wrap")
    def _keep_new_target_key(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        result = handler(self)
        if "new_target" not in result:
            result["new_target"] = None
        return result


class DispatchModeUpdateRequest(_WireBase):
    """Body for ``PATCH /v0/experiments/{E}/dispatch_mode`` (§2.8).

    A partial dispatch_mode object — any subset of the four normative
    keys (``ideation_creation`` / ``execution_dispatch`` /
    ``evaluation_dispatch`` / ``integration``, plus the 12a-3
    ``termination`` key). Unknown keys are tolerated per
    [`02-data-model.md`](../../../../spec/v0/02-data-model.md) §2.4
    and round-trip through via ``extra="allow"``. The server stamps
    ``updated_by`` from the authenticated principal.
    """

    model_config = ConfigDict(strict=True, extra="allow")

    termination: Annotated[DispatchModeValue | None, NotNone] = None
    ideation_creation: Annotated[
        DispatchModeValue | None, NotNone
    ] = None
    execution_dispatch: Annotated[
        DispatchModeValue | None, NotNone
    ] = None
    evaluation_dispatch: Annotated[
        DispatchModeValue | None, NotNone
    ] = None
    integration: Annotated[DispatchModeValue | None, NotNone] = None


class DispatchModeResponse(_WireBase):
    """Body for ``GET`` / ``PATCH`` ``/v0/experiments/{E}/dispatch_mode``.

    Full post-update state, all five normative keys present. Unknown
    keys persisted by older writes round-trip via ``extra="allow"``.
    """

    model_config = ConfigDict(strict=True, extra="allow")

    termination: DispatchModeValue
    ideation_creation: DispatchModeValue
    execution_dispatch: DispatchModeValue
    evaluation_dispatch: DispatchModeValue
    integration: DispatchModeValue


class TerminateRequest(_WireBase):
    """Body for ``POST /v0/experiments/{E}/terminate`` (§2.9).

    Admin-group-gated per [`07-wire-protocol.md`](../../../../spec/v0/07-wire-protocol.md)
    §2.9 / §13.3. The server stamps ``terminated_by`` from the
    authenticated principal; the body MUST NOT carry it (``extra="forbid"``
    inherited from ``_WireBase`` rejects unknown keys).
    """

    reason: str


class ExperimentStateResponse(_WireBase):
    """Body for ``GET /v0/experiments/{E}/state`` (§2.9 companion read).

    Per [`02-data-model.md`](../../../../spec/v0/02-data-model.md) §2.5
    the runtime ``state`` field is one of ``"running"`` or
    ``"terminated"``.
    """

    state: Literal["running", "terminated"]


class PolicyErrorRequest(_WireBase):
    """Body for ``POST /v0/experiments/{E}/policy-errors`` (12a-3 wave 7).

    The orchestrator posts here when a termination-policy callable
    raises, so the registered ``experiment.policy_error`` event
    ([`05-event-protocol.md`](../../../../spec/v0/05-event-protocol.md)
    §3.4) lands in the event log per chapter 03 §6.2 decision-type 0's
    fault-tolerance subsection. The event is exempt from the §2
    transactional invariant: there is no protocol-owned state mutation
    pairs with it; this is a single-event append.

    Three fields per the registered schema: ``policy_kind`` (v0
    defines ``"termination"``; future decision types MAY add new
    values), ``error_type`` (the exception class name), and
    ``error_message`` (the exception's ``str()`` representation).
    """

    policy_kind: Annotated[str, Field(min_length=1)]
    error_type: Annotated[str, Field(min_length=1)]
    error_message: str


__all__ = [
    "AddGroupMemberRequest",
    "ClaimRequest",
    "ClaimResponse",
    "DispatchModeResponse",
    "DispatchModeUpdateRequest",
    "EventsResponse",
    "ExperimentStateResponse",
    "IntegrateRequest",
    "PolicyErrorRequest",
    "ReassignRequest",
    "ReclaimRequest",
    "RegisterGroupRequest",
    "RegisterWorkerRequest",
    "RejectRequest",
    "SubmitRequest",
    "TerminateRequest",
    "ValidateEvaluationRequest",
    "ValidateTerminalResponse",
    "WhoamiResponse",
    "WorkerRegistration",
]
