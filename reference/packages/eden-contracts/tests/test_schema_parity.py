"""Schema-parity test: Pydantic models and JSON Schemas accept/reject in lockstep.

For every case in :mod:`cases`, the Pydantic model bound to that schema
and the JSON Schema itself MUST either both accept the input or both
reject it. Drift here means the reference bindings disagree with the
authoritative wire format.
"""

from __future__ import annotations

from typing import Any

import pytest
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import ValidationError as PydanticValidationError

from .cases import ALL_CASES, Case
from .conftest import model_validate, schema_validator


def _schema_accepts(schema_name: str, data: Any) -> bool:
    validator = schema_validator(schema_name)
    try:
        validator.validate(data)
    except JsonSchemaValidationError:
        return False
    return True


def _model_accepts(schema_name: str, data: Any) -> bool:
    try:
        model_validate(schema_name, data)
    except PydanticValidationError:
        return False
    return True


def _all_cases() -> list[tuple[str, Case]]:
    return [(schema, case) for schema, cases in ALL_CASES.items() for case in cases]


@pytest.mark.parametrize(
    ("schema_name", "case"),
    _all_cases(),
    ids=lambda v: v.name if isinstance(v, Case) else v,
)
def test_schema_and_model_agree(schema_name: str, case: Case) -> None:
    """Every case MUST be accepted (or rejected) by both the schema and the model."""
    schema_ok = _schema_accepts(schema_name, case.data)
    model_ok = _model_accepts(schema_name, case.data)
    assert schema_ok == case.should_pass, (
        f"{schema_name}::{case.name}: schema accept={schema_ok}, "
        f"expected={case.should_pass}"
    )
    assert model_ok == case.should_pass, (
        f"{schema_name}::{case.name}: model accept={model_ok}, "
        f"expected={case.should_pass}"
    )
    assert schema_ok == model_ok, (
        f"{schema_name}::{case.name}: schema/model disagreement "
        f"(schema={schema_ok}, model={model_ok})"
    )
