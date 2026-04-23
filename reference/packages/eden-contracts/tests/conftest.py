"""Shared fixtures: schema registry, per-schema validators, model validators.

The parity tests need to run each case twice — once against the Pydantic
model and once against the hand-authored JSON Schema. This conftest wires
up both paths over the same case corpus so they stay in sync.

A custom ``FormatChecker`` enables ``uri`` and ``date-time`` format
validation on the schema side; jsonschema's default does not enforce
``format`` keywords unless a checker is supplied. The model side enforces
the same semantics in :mod:`eden_contracts._common`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from eden_contracts import (
    Event,
    ExperimentConfig,
    MetricsSchema,
    Proposal,
    TaskAdapter,
    Trial,
)
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012
from rfc3986_validator import validate_rfc3986

SCHEMAS_DIR: Path = Path(__file__).resolve().parents[4] / "spec" / "v0" / "schemas"
"""Resolves to ``<repo>/spec/v0/schemas``."""

BASE_URI: str = "https://eden.local/schemas/"
"""Synthetic base URI so relative ``$ref``s between schema files resolve."""

MODEL_NAMES: tuple[str, ...] = (
    "experiment-config",
    "task",
    "event",
    "proposal",
    "trial",
    "metrics-schema",
)


FORMAT_CHECKER = FormatChecker()

EXPLICIT_FORMATS: set[str] = set()
"""Formats with a custom handler registered below.

``jsonschema.FormatChecker()`` pre-registers permissive defaults for
many well-known formats (``uri``, ``email``, ``uuid``, …), so a naive
"is this format in the checker?" test green-lights any format at all.
We want parity to be explicit — every format the schemas use has a
handler we wrote, with matching enforcement on the model side — so
we track our deliberate registrations separately from the built-ins.
"""


def _register_format(name: str) -> Any:
    def decorator(func: Any) -> Any:
        EXPLICIT_FORMATS.add(name)
        FORMAT_CHECKER.checks(name, raises=ValueError)(func)
        return func

    return decorator


@_register_format("uri")
def _check_uri(instance: Any) -> bool:
    if not isinstance(instance, str):
        return True
    if not validate_rfc3986(instance, rule="URI"):
        raise ValueError(f"not a valid RFC 3986 URI: {instance!r}")
    return True


@_register_format("date-time")
def _check_datetime(instance: Any) -> bool:
    if not isinstance(instance, str):
        return True
    datetime.fromisoformat(instance)
    return True


def _build_registry() -> Registry:
    registry: Registry = Registry()
    for path in SCHEMAS_DIR.glob("*.schema.json"):
        schema = json.loads(path.read_text())
        resource = Resource.from_contents(schema, default_specification=DRAFT202012)
        registry = registry.with_resource(uri=f"{BASE_URI}{path.name}", resource=resource)
    return registry


_REGISTRY: Registry = _build_registry()


def schema_validator(name: str) -> Draft202012Validator:
    """Return a validator for ``<name>.schema.json`` with refs + formats enforced."""
    return Draft202012Validator(
        schema={"$ref": f"{BASE_URI}{name}.schema.json"},
        registry=_REGISTRY,
        format_checker=FORMAT_CHECKER,
    )


_MODEL_VALIDATORS: dict[str, Callable[[Any], object]] = {
    "experiment-config": ExperimentConfig.model_validate,
    "task": TaskAdapter.validate_python,
    "event": Event.model_validate,
    "proposal": Proposal.model_validate,
    "trial": Trial.model_validate,
    "metrics-schema": MetricsSchema.model_validate,
}


def model_validate(name: str, data: Any) -> object:
    """Validate ``data`` with the Pydantic model bound to schema ``name``."""
    return _MODEL_VALIDATORS[name](data)
