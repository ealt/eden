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
from eden_wire.errors import ProblemJson
from eden_wire.models import (
    ClaimRequest,
    ClaimResponse,
    EventsResponse,
    IntegrateRequest,
    ReclaimRequest,
    RejectRequest,
    SubmitRequest,
)
from pydantic import ValidationError

SCHEMA_DIR = Path(__file__).resolve().parents[4] / "spec" / "v0" / "schemas" / "wire"


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
    def test_accept_minimum(self) -> None:
        model = ClaimRequest(worker_id="worker-1")
        dumped = model.model_dump(mode="json", exclude_none=True)
        _validate_against("claim-request.schema.json", dumped)

    def test_accept_with_expires_at(self) -> None:
        model = ClaimRequest(worker_id="w", expires_at="2026-04-24T00:00:00Z")
        _validate_against(
            "claim-request.schema.json", model.model_dump(mode="json", exclude_none=True)
        )

    def test_reject_missing_worker(self) -> None:
        with pytest.raises(ValidationError):
            ClaimRequest.model_validate({})


class TestClaimResponseParity:
    def test_accept_with_expires_at_absent(self) -> None:
        model = ClaimResponse(
            token="tok", worker_id="w", claimed_at="2026-04-24T00:00:00Z"
        )
        dumped = model.model_dump(mode="json", exclude_none=True)
        assert "expires_at" not in dumped
        _validate_against("claim-response.schema.json", dumped)

    def test_accept_with_expires_at_present(self) -> None:
        model = ClaimResponse(
            token="tok",
            worker_id="w",
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
                    "token": "tok",
                    "worker_id": "w",
                    "claimed_at": "2026-04-24T00:00:00Z",
                    "expires_at": None,
                },
            )


class TestSubmitRequestParity:
    def test_accept(self) -> None:
        model = SubmitRequest(token="tok", payload={"kind": "plan", "status": "success"})
        _validate_against(
            "submit-request.schema.json",
            model.model_dump(mode="json", exclude_none=True),
        )


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
        model = IntegrateRequest(trial_commit_sha="a" * 40)
        _validate_against(
            "integrate-request.schema.json",
            model.model_dump(mode="json", exclude_none=True),
        )

    def test_accept_sha256(self) -> None:
        model = IntegrateRequest(trial_commit_sha="a" * 64)
        _validate_against(
            "integrate-request.schema.json",
            model.model_dump(mode="json", exclude_none=True),
        )

    def test_reject_malformed_sha(self) -> None:
        with pytest.raises(ValidationError):
            IntegrateRequest(trial_commit_sha="not-a-sha")


class TestEventsResponseParity:
    def test_accept_empty(self) -> None:
        model = EventsResponse(events=[], cursor=0)
        _validate_against(
            "events-response.schema.json",
            model.model_dump(mode="json", exclude_none=True),
        )


class TestErrorEnvelopeParity:
    def test_accept(self) -> None:
        envelope = ProblemJson(
            type="eden://error/not-found",
            title="Not Found",
            status=404,
            detail="trial missing",
            instance="http://host/x",
        )
        _validate_against("error.schema.json", envelope.to_dict())

    def test_schema_rejects_unknown_type(self) -> None:
        with pytest.raises(jsonschema.ValidationError):
            _validate_against(
                "error.schema.json",
                {"type": "eden://error/made-up", "title": "x", "status": 400},
            )
