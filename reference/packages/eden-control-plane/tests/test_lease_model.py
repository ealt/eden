"""Schema-parity for `ExperimentLease` against `spec/v0/schemas/lease.schema.json`.

Mirrors the eden-checkpoint pattern: the Pydantic model and the
JSON Schema MUST accept/reject the same set of inputs. CI runs this
on every commit so the two definitions cannot drift.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from eden_control_plane import ExperimentLease
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import ValidationError as PydanticValidationError

LEASE_SCHEMA: Path = (
    Path(__file__).resolve().parents[4]
    / "spec"
    / "v0"
    / "schemas"
    / "lease.schema.json"
)


def _schema() -> dict[str, Any]:
    return json.loads(LEASE_SCHEMA.read_text())


# Mirror eden-contracts/tests/conftest.py: register a strict date-time
# format checker. jsonschema's default accepts impossible dates like
# "2026-13-99T12:00:00Z" because the pattern just enumerates digit
# counts; the model side rejects via datetime.fromisoformat. CI parity
# requires both layers to enforce the same semantics.
_FORMAT_CHECKER = FormatChecker()


@_FORMAT_CHECKER.checks("date-time", raises=ValueError)
def _check_datetime(instance: Any) -> bool:
    if not isinstance(instance, str):
        return True
    datetime.fromisoformat(instance)
    return True


def _validator() -> Draft202012Validator:
    return Draft202012Validator(_schema(), format_checker=_FORMAT_CHECKER)


CANONICAL: dict[str, Any] = {
    "lease_id": "lease-abc-123",
    "experiment_id": "exp_0123456789abcdefghjkmnpqrs",
    "holder": "wkr_0123456789abcdefghjkmnpqrs",
    "holder_instance": "f1f2f3f4-f5f6-f7f8-f9fa-fbfcfdfeff00",
    "acquired_at": "2026-05-19T12:00:00Z",
    "expires_at": "2026-05-19T12:00:30Z",
    "renewed_at": "2026-05-19T12:00:00Z",
}


def test_schema_accepts_canonical() -> None:
    _validator().validate(CANONICAL)


def test_model_accepts_canonical() -> None:
    ExperimentLease.model_validate(CANONICAL)


def test_model_dump_roundtrips_through_schema() -> None:
    """A model -> dict -> schema cycle MUST validate cleanly."""
    lease = ExperimentLease.model_validate(CANONICAL)
    dumped = lease.model_dump(mode="json", exclude_none=True)
    _validator().validate(dumped)


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("lease_id", ""),
        ("experiment_id", ""),
        ("experiment_id", "exp-1"),  # legacy kebab grammar retired
        ("experiment_id", "wkr_0123456789abcdefghjkmnpqrs"),  # wrong prefix
        ("experiment_id", "exp_0123456789abcdefghjkmnpqr"),  # 25-char suffix
        ("experiment_id", "exp_0123456789abcdefghjkmnpqri"),  # 'i' not Crockford
        ("holder", "auto-orchestrator-1"),  # legacy kebab grammar retired
        ("holder", "grp_0123456789abcdefghjkmnpqrs"),  # group prefix, not worker
        ("holder", "wkr_0123456789abcdefghjkmnpqr"),  # 25-char suffix
        ("holder_instance", ""),
        ("acquired_at", "2026-05-19T12:00:00"),  # missing Z
        ("acquired_at", "2026-13-99T12:00:00Z"),  # impossible date
        ("expires_at", "not-a-timestamp"),
        ("renewed_at", "2026-05-19 12:00:00Z"),  # space instead of T
    ],
)
def test_invalid_value_is_rejected_by_both(field: str, bad_value: Any) -> None:
    """Every invalid value MUST be rejected by both the schema and the model."""
    bad = {**CANONICAL, field: bad_value}
    with pytest.raises(JsonSchemaValidationError):
        _validator().validate(bad)
    with pytest.raises(PydanticValidationError):
        ExperimentLease.model_validate(bad)


@pytest.mark.parametrize(
    "missing",
    [
        "lease_id",
        "experiment_id",
        "holder",
        "holder_instance",
        "acquired_at",
        "expires_at",
        "renewed_at",
    ],
)
def test_missing_required_field_is_rejected_by_both(missing: str) -> None:
    payload = {k: v for k, v in CANONICAL.items() if k != missing}
    with pytest.raises(JsonSchemaValidationError):
        _validator().validate(payload)
    with pytest.raises(PydanticValidationError):
        ExperimentLease.model_validate(payload)


def test_null_value_rejected_by_both() -> None:
    """Explicit null on a required string MUST be rejected by both layers."""
    payload = {**CANONICAL, "lease_id": None}
    with pytest.raises(JsonSchemaValidationError):
        _validator().validate(payload)
    with pytest.raises(PydanticValidationError):
        ExperimentLease.model_validate(payload)
