"""Round-trip parity: every accept fixture dumps to schema-valid JSON.

``model_validate(data) → model_dump(mode="json", exclude_none=True)`` MUST
produce output that is accepted by the JSON Schema. ``exclude_none=True``
is the idiomatic dump mode for wire-format emission — JSON Schema treats
optional fields as absent-or-present, not nullable, so a naive dump that
includes ``null`` keys would fail schema validation.
"""

from __future__ import annotations

from typing import Any

from eden_contracts import (
    REGISTERED_EVENT_TYPES,
    Event,
    ExperimentConfig,
    MetricsSchema,
    Proposal,
    RegisteredEventAdapter,
    TaskAdapter,
    Trial,
)

from .cases import ALL_CASES
from .conftest import schema_validator


def _dump_event(data: Any) -> Any:
    """Dump through the registered-type model when ``type`` is registered.

    An envelope-only dump would discard per-type payload validation, which
    would mask round-trip drift for registered events.
    """
    envelope = Event.model_validate(data)
    if envelope.type in REGISTERED_EVENT_TYPES:
        typed = RegisteredEventAdapter.validate_python(data)
        return RegisteredEventAdapter.dump_python(typed, mode="json", exclude_none=True)
    return envelope.model_dump(mode="json", exclude_none=True)


_MODEL_DUMPERS = {
    "experiment-config": lambda d: ExperimentConfig.model_validate(d).model_dump(
        mode="json", exclude_none=True
    ),
    "task": lambda d: TaskAdapter.dump_python(
        TaskAdapter.validate_python(d), mode="json", exclude_none=True
    ),
    "event": _dump_event,
    "proposal": lambda d: Proposal.model_validate(d).model_dump(mode="json", exclude_none=True),
    "trial": lambda d: Trial.model_validate(d).model_dump(mode="json", exclude_none=True),
    "metrics-schema": lambda d: MetricsSchema.model_validate(d).model_dump(
        mode="json", exclude_none=True
    ),
}


def test_accept_fixtures_roundtrip_schema_valid() -> None:
    """Every accept case dumps back to JSON that the schema validates."""
    for schema_name, cases in ALL_CASES.items():
        validator = schema_validator(schema_name)
        dump = _MODEL_DUMPERS[schema_name]
        for case in cases:
            if not case.should_pass:
                continue
            dumped = dump(case.data)
            validator.validate(dumped)
