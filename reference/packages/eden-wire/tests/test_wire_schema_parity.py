"""Schema-parity check for the wire models.

Every Pydantic model in ``eden_wire.models`` has a corresponding JSON
Schema under ``spec/v0/schemas/wire/``. The parity test asserts that
a valid instance of the Pydantic model serializes to something that
the schema also accepts, and — where the schemas list mandatory
fields — that the model rejects inputs missing them.

This is the wire-side analog of the ``eden-contracts`` schema-parity
tests. Drift between the model and the schema (seen concretely on
``claim-response.schema.json`` where an earlier draft permitted
``null`` on a ``NotNone`` field) surfaces here rather than in
production.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from eden_contracts import TaskTarget
from eden_wire.errors import ProblemJson
from eden_wire.models import (
    AddGroupMemberRequest,
    ClaimRequest,
    ClaimResponse,
    DispatchModeResponse,
    DispatchModeUpdateRequest,
    EventsResponse,
    IntegrateRequest,
    PolicyErrorRequest,
    ReassignRequest,
    ReclaimRequest,
    RegisterGroupRequest,
    RegisterWorkerRequest,
    RejectRequest,
    SubmitRequest,
    WhoamiResponse,
    WorkerRegistration,
)
from pydantic import ValidationError

SCHEMA_DIR = Path(__file__).resolve().parents[4] / "spec" / "v0" / "schemas" / "wire"

# Opaque, system-minted ids (spec/v0/02-data-model.md §1.6). Constant
# literals keep the parity assertions deterministic.
WKR = "wkr_01kt58n3epchs0jvkr5vyza0dw"
WKR_2 = "wkr_0000000000000000000000000a"
GRP = "grp_01kt58n3ep6vedj9ht1scnmb1m"
EXP = "exp_01kt58n3epdx0abxc691y9w3s3"


def _load_schema(name: str) -> dict[str, Any]:
    return json.loads((SCHEMA_DIR / name).read_text())


def _validate_against(schema_name: str, instance: dict[str, Any]) -> None:
    schema = _load_schema(schema_name)
    jsonschema.validate(
        instance,
        schema,
        cls=jsonschema.Draft202012Validator,
        format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
    )


class TestClaimRequestParity:
    def test_accept_empty(self) -> None:
        model = ClaimRequest()
        dumped = model.model_dump(mode="json", exclude_none=True)
        _validate_against("claim-request.schema.json", dumped)

    def test_accept_with_expires_at(self) -> None:
        model = ClaimRequest(expires_at="2026-04-24T00:00:00Z")
        _validate_against(
            "claim-request.schema.json", model.model_dump(mode="json", exclude_none=True)
        )


class TestClaimResponseParity:
    def test_accept_with_expires_at_absent(self) -> None:
        model = ClaimResponse(
            worker_id=WKR, claimed_at="2026-04-24T00:00:00Z"
        )
        dumped = model.model_dump(mode="json", exclude_none=True)
        assert "expires_at" not in dumped
        assert "token" not in dumped
        _validate_against("claim-response.schema.json", dumped)

    def test_accept_with_expires_at_present(self) -> None:
        model = ClaimResponse(
            worker_id=WKR,
            claimed_at="2026-04-24T00:00:00Z",
            expires_at="2026-04-24T01:00:00Z",
        )
        _validate_against(
            "claim-response.schema.json",
            model.model_dump(mode="json", exclude_none=True),
        )

    def test_schema_rejects_null(self) -> None:
        # The NotNone discipline is enforced on both sides: model rejects
        # explicit null, and the schema does too.
        with pytest.raises(jsonschema.ValidationError):
            _validate_against(
                "claim-response.schema.json",
                {
                    "worker_id": WKR,
                    "claimed_at": "2026-04-24T00:00:00Z",
                    "expires_at": None,
                },
            )

    def test_schema_rejects_legacy_token(self) -> None:
        """The pre-12a-1 ``token`` field is no longer permitted on the wire."""
        with pytest.raises(jsonschema.ValidationError):
            _validate_against(
                "claim-response.schema.json",
                {
                    "token": "tok",
                    "worker_id": WKR,
                    "claimed_at": "2026-04-24T00:00:00Z",
                },
            )


class TestSubmitRequestParity:
    def test_accept(self) -> None:
        model = SubmitRequest(payload={"kind": "ideation", "status": "success"})
        _validate_against(
            "submit-request.schema.json",
            model.model_dump(mode="json", exclude_none=True),
        )

    def test_schema_rejects_legacy_token(self) -> None:
        with pytest.raises(jsonschema.ValidationError):
            _validate_against(
                "submit-request.schema.json",
                {
                    "token": "tok",
                    "payload": {"kind": "ideation", "status": "success"},
                },
            )


class TestRegisterWorkerRequestParity:
    def test_accept_minimum(self) -> None:
        """The body carries no worker_id (server mints it); empty is valid."""
        model = RegisterWorkerRequest()
        dumped = model.model_dump(mode="json", exclude_none=True)
        assert "worker_id" not in dumped
        _validate_against("register-worker-request.schema.json", dumped)

    def test_accept_with_name(self) -> None:
        model = RegisterWorkerRequest(name="Eric")
        _validate_against(
            "register-worker-request.schema.json",
            model.model_dump(mode="json", exclude_none=True),
        )

    def test_accept_with_labels(self) -> None:
        model = RegisterWorkerRequest(name="eric", labels={"role": "ideator"})
        _validate_against(
            "register-worker-request.schema.json",
            model.model_dump(mode="json", exclude_none=True),
        )

    def test_reject_legacy_worker_id_field(self) -> None:
        """The caller no longer supplies a worker_id; the field is rejected."""
        with pytest.raises(ValidationError):
            RegisterWorkerRequest.model_validate({"worker_id": WKR})

    def test_accept_ill_formed_name(self) -> None:
        """An ill-formed name parses the request body; the Store enforces
        well-formedness server-side → 422 eden://error/invalid-name (not a
        400 request-validation failure). The request model's ``name`` is a
        plain string, so both the model and the request schema accept it."""
        model = RegisterWorkerRequest(name=" leading-space")
        _validate_against(
            "register-worker-request.schema.json",
            model.model_dump(mode="json", exclude_none=True),
        )


class TestWorkerRegistrationParity:
    def test_accept_with_token(self) -> None:
        model = WorkerRegistration(
            worker_id=WKR,
            name="Eric",
            experiment_id=EXP,
            registered_at="2026-04-24T00:00:00Z",
            registered_by="admin",
            registration_token="ab" * 32,
        )
        _validate_against(
            "worker-registration.schema.json",
            model.model_dump(mode="json", exclude_none=True),
        )

    def test_accept_idempotent_no_token(self) -> None:
        """A read of the record returns it without a token (and no name)."""
        model = WorkerRegistration(
            worker_id=WKR,
            experiment_id=EXP,
            registered_at="2026-04-24T00:00:00Z",
        )
        dumped = model.model_dump(mode="json", exclude_none=True)
        assert "registration_token" not in dumped
        assert "name" not in dumped
        _validate_against("worker-registration.schema.json", dumped)

    def test_reject_kebab_worker_id(self) -> None:
        with pytest.raises(ValidationError):
            WorkerRegistration(
                worker_id="eric",
                experiment_id=EXP,
                registered_at="2026-04-24T00:00:00Z",
            )


class TestWhoamiResponseParity:
    def test_accept(self) -> None:
        model = WhoamiResponse(worker_id=WKR)
        _validate_against(
            "whoami-response.schema.json",
            model.model_dump(mode="json", exclude_none=True),
        )

    def test_accept_with_name(self) -> None:
        model = WhoamiResponse(worker_id=WKR, name="Eric")
        _validate_against(
            "whoami-response.schema.json",
            model.model_dump(mode="json", exclude_none=True),
        )


class TestRegisterGroupRequestParity:
    def test_accept_no_members(self) -> None:
        """The body carries no group_id (server mints it); empty is valid."""
        model = RegisterGroupRequest()
        dumped = model.model_dump(mode="json", exclude_none=True)
        assert "group_id" not in dumped
        _validate_against("register-group-request.schema.json", dumped)

    def test_accept_with_name_and_members(self) -> None:
        model = RegisterGroupRequest(name="team-a", members=[WKR, GRP])
        _validate_against(
            "register-group-request.schema.json",
            model.model_dump(mode="json", exclude_none=True),
        )

    def test_reject_legacy_group_id_field(self) -> None:
        with pytest.raises(ValidationError):
            RegisterGroupRequest.model_validate({"group_id": GRP})

    def test_reject_kebab_member(self) -> None:
        with pytest.raises(ValidationError):
            RegisterGroupRequest(members=["eric"])


class TestAddGroupMemberRequestParity:
    def test_accept_worker_member(self) -> None:
        model = AddGroupMemberRequest(member_id=WKR)
        _validate_against(
            "add-group-member-request.schema.json",
            model.model_dump(mode="json", exclude_none=True),
        )

    def test_accept_group_member(self) -> None:
        model = AddGroupMemberRequest(member_id=GRP)
        _validate_against(
            "add-group-member-request.schema.json",
            model.model_dump(mode="json", exclude_none=True),
        )

    def test_reject_kebab_member(self) -> None:
        with pytest.raises(ValidationError):
            AddGroupMemberRequest(member_id="eric")


class TestRejectRequestParity:
    @pytest.mark.parametrize("reason", ["worker_error", "validation_error", "policy_limit"])
    def test_accept(self, reason: str) -> None:
        model = RejectRequest(reason=reason)
        _validate_against(
            "reject-request.schema.json",
            model.model_dump(mode="json", exclude_none=True),
        )

    def test_reject_out_of_vocabulary(self) -> None:
        with pytest.raises(ValidationError):
            RejectRequest(reason="anything_else")


class TestReclaimRequestParity:
    @pytest.mark.parametrize("cause", ["expired", "operator", "health_policy"])
    def test_accept(self, cause: str) -> None:
        model = ReclaimRequest(cause=cause)
        _validate_against(
            "reclaim-request.schema.json",
            model.model_dump(mode="json", exclude_none=True),
        )


class TestIntegrateRequestParity:
    def test_accept_sha1(self) -> None:
        model = IntegrateRequest(variant_commit_sha="a" * 40)
        _validate_against(
            "integrate-request.schema.json",
            model.model_dump(mode="json", exclude_none=True),
        )

    def test_accept_sha256(self) -> None:
        model = IntegrateRequest(variant_commit_sha="a" * 64)
        _validate_against(
            "integrate-request.schema.json",
            model.model_dump(mode="json", exclude_none=True),
        )

    def test_reject_malformed_sha(self) -> None:
        with pytest.raises(ValidationError):
            IntegrateRequest(variant_commit_sha="not-a-sha")


class TestEventsResponseParity:
    def test_accept_empty(self) -> None:
        model = EventsResponse(events=[], cursor=0)
        _validate_against(
            "events-response.schema.json",
            model.model_dump(mode="json", exclude_none=True),
        )


class TestReassignRequestParity:
    def test_accept_worker_target(self) -> None:
        model = ReassignRequest(
            new_target=TaskTarget(kind="worker", id=WKR),
            reason="operator",
        )
        dumped = model.model_dump(mode="json", exclude_none=True)
        _validate_against("reassign-request.schema.json", dumped)

    def test_accept_group_target(self) -> None:
        model = ReassignRequest(
            new_target=TaskTarget(kind="group", id=GRP),
            reason="route to humans",
        )
        _validate_against(
            "reassign-request.schema.json",
            model.model_dump(mode="json", exclude_none=True),
        )

    def test_accept_null_target_round_trips_key(self) -> None:
        """``new_target`` is required-nullable; the wrap-serializer keeps the key."""
        model = ReassignRequest(new_target=None, reason="open up to any worker")
        dumped = model.model_dump(mode="json", exclude_none=True)
        assert "new_target" in dumped
        assert dumped["new_target"] is None
        _validate_against("reassign-request.schema.json", dumped)

    def test_reject_empty_reason(self) -> None:
        with pytest.raises(ValidationError):
            ReassignRequest(new_target=None, reason="")

    def test_schema_rejects_missing_new_target(self) -> None:
        with pytest.raises(jsonschema.ValidationError):
            _validate_against(
                "reassign-request.schema.json",
                {"reason": "operator"},
            )

    def test_schema_rejects_reassigned_by_in_body(self) -> None:
        """Server stamps reassigned_by; the body MUST NOT carry it."""
        with pytest.raises(jsonschema.ValidationError):
            _validate_against(
                "reassign-request.schema.json",
                {
                    "new_target": None,
                    "reason": "operator",
                    "reassigned_by": "admin-eric",
                },
            )


class TestDispatchModeUpdateRequestParity:
    def test_accept_single_key(self) -> None:
        model = DispatchModeUpdateRequest(evaluation_dispatch="manual")
        dumped = model.model_dump(mode="json", exclude_none=True)
        assert dumped == {"evaluation_dispatch": "manual"}
        _validate_against("dispatch-mode-request.schema.json", dumped)

    def test_accept_full(self) -> None:
        model = DispatchModeUpdateRequest(
            ideation_creation="auto",
            execution_dispatch="auto",
            evaluation_dispatch="manual",
            integration="manual",
        )
        _validate_against(
            "dispatch-mode-request.schema.json",
            model.model_dump(mode="json", exclude_none=True),
        )

    def test_accept_empty(self) -> None:
        model = DispatchModeUpdateRequest()
        dumped = model.model_dump(mode="json", exclude_none=True)
        assert dumped == {}
        _validate_against("dispatch-mode-request.schema.json", dumped)

    def test_reject_invalid_value(self) -> None:
        with pytest.raises(ValidationError):
            DispatchModeUpdateRequest.model_validate({"ideation_creation": "paused"})

    def test_schema_rejects_invalid_value(self) -> None:
        with pytest.raises(jsonschema.ValidationError):
            _validate_against(
                "dispatch-mode-request.schema.json",
                {"ideation_creation": "paused"},
            )

    def test_schema_tolerates_unknown_keys(self) -> None:
        """§2.5: unknown keys are tolerated and round-trip through."""
        _validate_against(
            "dispatch-mode-request.schema.json",
            {"future_decision": "auto"},
        )


class TestDispatchModeResponseParity:
    def test_accept_default(self) -> None:
        model = DispatchModeResponse(
            termination="manual",
            ideation_creation="auto",
            execution_dispatch="auto",
            evaluation_dispatch="auto",
            integration="auto",
        )
        _validate_against(
            "dispatch-mode-response.schema.json",
            model.model_dump(mode="json", exclude_none=True),
        )

    def test_schema_rejects_missing_normative_key(self) -> None:
        with pytest.raises(jsonschema.ValidationError):
            _validate_against(
                "dispatch-mode-response.schema.json",
                {
                    "termination": "manual",
                    "ideation_creation": "auto",
                    "execution_dispatch": "auto",
                    "evaluation_dispatch": "auto",
                    # `integration` missing — required-set violation.
                },
            )


class TestPolicyErrorRequestParity:
    def test_accept(self) -> None:
        model = PolicyErrorRequest(
            policy_kind="termination",
            error_type="ValueError",
            error_message="policy callable raised: bad config",
        )
        _validate_against(
            "policy-error-request.schema.json",
            model.model_dump(mode="json", exclude_none=True),
        )

    def test_schema_rejects_missing_required_field(self) -> None:
        with pytest.raises(jsonschema.ValidationError):
            _validate_against(
                "policy-error-request.schema.json",
                {
                    "policy_kind": "termination",
                    "error_type": "X",
                    # `error_message` missing.
                },
            )

    def test_schema_rejects_empty_policy_kind(self) -> None:
        with pytest.raises(jsonschema.ValidationError):
            _validate_against(
                "policy-error-request.schema.json",
                {
                    "policy_kind": "",
                    "error_type": "X",
                    "error_message": "y",
                },
            )

    def test_schema_rejects_additional_keys(self) -> None:
        with pytest.raises(jsonschema.ValidationError):
            _validate_against(
                "policy-error-request.schema.json",
                {
                    "policy_kind": "termination",
                    "error_type": "X",
                    "error_message": "y",
                    "extra": "field",
                },
            )


class TestErrorEnvelopeParity:
    def test_accept(self) -> None:
        envelope = ProblemJson(
            type="eden://error/not-found",
            title="Not Found",
            status=404,
            detail="variant missing",
            instance="http://host/x",
        )
        _validate_against("error.schema.json", envelope.to_dict())

    def test_schema_rejects_unknown_type(self) -> None:
        with pytest.raises(jsonschema.ValidationError):
            _validate_against(
                "error.schema.json",
                {"type": "eden://error/made-up", "title": "x", "status": 400},
            )
