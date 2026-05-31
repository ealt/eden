"""Shared type aliases and validators for EDEN contract models.

Every pattern here mirrors a pattern in the spec/v0 JSON Schemas. When a
schema changes, the corresponding alias here changes in lockstep.
"""

from datetime import datetime, timedelta
from typing import Annotated, Any

from pydantic import AfterValidator, BeforeValidator, Field, StringConstraints, TypeAdapter
from rfc3986_validator import validate_rfc3986

DATETIME_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?Z$"

COMMIT_SHA_PATTERN = r"^[0-9a-f]{40}([0-9a-f]{24})?$"

WORKER_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
"""Grammar for `worker_id` and `group_id` (spec/v0/02-data-model.md §6.1)."""


def _check_datetime(value: str) -> str:
    # The regex shape is a necessary pre-filter, but accepts impossible values
    # like month 99. fromisoformat (3.11+) accepts trailing "Z" and parses the
    # components strictly, so it catches non-real dates.
    datetime.fromisoformat(value)
    return value


def _check_uri(value: str) -> str:
    if not validate_rfc3986(value, rule="URI"):
        raise ValueError(f"not a valid RFC 3986 URI: {value!r}")
    return value


_DURATION_ADAPTER: TypeAdapter[timedelta] = TypeAdapter(timedelta)


def _check_duration(value: str) -> str:
    # A `timedelta` field on a `ConfigDict(strict=True)` model rejects
    # ISO-8601 strings like "PT2H"; strict mode allows only timedelta
    # instances or integer seconds. So `duration` is stored as a string
    # (matching the JSON Schema's string-typed `duration` field) and the
    # ISO-8601 form is validated here via a bare-`timedelta` TypeAdapter,
    # which is NOT bound to a strict model and so accepts the string. The
    # parsed value is discarded — the storage shape stays string for
    # round-trip parity; callers (e.g. the orchestrator's
    # `build_termination_policy`) re-parse with the same adapter at the
    # boundary. Zero / negative durations are rejected so the validation
    # surfaces at the config boundary, not only inside the policy factory.
    delta = _DURATION_ADAPTER.validate_python(value)
    if delta.total_seconds() <= 0:
        raise ValueError(f"duration must be positive (got {value!r} = {delta!r})")
    return value


def _reject_none(value: Any) -> Any:
    if value is None:
        raise ValueError(
            "field may be absent but MUST NOT be null; JSON Schema "
            "rejects explicit null for optional typed fields"
        )
    return value


DateTimeStr = Annotated[
    str,
    StringConstraints(pattern=DATETIME_PATTERN),
    AfterValidator(_check_datetime),
]
"""UTC ISO-8601 datetime string with a trailing ``Z``; must be a real datetime."""

CommitSha = Annotated[str, StringConstraints(pattern=COMMIT_SHA_PATTERN)]
"""Lowercase hex SHA-1 (40 chars) or SHA-256 (64 chars) commit identifier."""

WorkerId = Annotated[
    str,
    StringConstraints(pattern=WORKER_ID_PATTERN, min_length=1, max_length=64),
]
"""Worker / group identifier (spec/v0/02-data-model.md §6.1)."""

UriStr = Annotated[str, AfterValidator(_check_uri)]
"""URI string — must have a scheme, per RFC 3986 (schema ``format: uri``)."""

DurationStr = Annotated[str, Field(min_length=1), AfterValidator(_check_duration)]
"""ISO-8601 duration string (e.g. ``"PT2H"``); must parse to a positive ``timedelta``.

Mirrors the JSON Schema's ``format: duration`` field. Stored as a string for
schema↔model round-trip parity; the orchestrator's ``build_termination_policy``
re-parses to a ``timedelta`` at the boundary (schema ``format: duration``)."""

NotNone = BeforeValidator(_reject_none)
"""Annotate optional fields to reject explicit ``null`` on input while allowing absence."""
