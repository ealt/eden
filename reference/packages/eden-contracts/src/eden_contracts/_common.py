"""Shared type aliases and validators for EDEN contract models.

Every pattern here mirrors a pattern in the spec/v0 JSON Schemas. When a
schema changes, the corresponding alias here changes in lockstep.
"""

from datetime import datetime
from typing import Annotated, Any

from pydantic import AfterValidator, BeforeValidator, StringConstraints
from rfc3986_validator import validate_rfc3986

DATETIME_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?Z$"

COMMIT_SHA_PATTERN = r"^[0-9a-f]{40}([0-9a-f]{24})?$"


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

UriStr = Annotated[str, AfterValidator(_check_uri)]
"""URI string — must have a scheme, per RFC 3986 (schema ``format: uri``)."""

NotNone = BeforeValidator(_reject_none)
"""Annotate optional fields to reject explicit ``null`` on input while allowing absence."""
