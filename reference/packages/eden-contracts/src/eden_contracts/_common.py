"""Shared type aliases and validators for EDEN contract models.

Every pattern here mirrors a pattern in the spec/v0 JSON Schemas. When a
schema changes, the corresponding alias here changes in lockstep.
"""

import secrets
import time
import unicodedata
from datetime import datetime, timedelta
from typing import Annotated, Any

from pydantic import AfterValidator, BeforeValidator, Field, StringConstraints, TypeAdapter
from rfc3986_validator import validate_rfc3986

DATETIME_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?Z$"

COMMIT_SHA_PATTERN = r"^[0-9a-f]{40}([0-9a-f]{24})?$"

# --- opaque entity-identifier grammars (spec/v0/02-data-model.md §1.6) ---
# Each id is a stable type-prefix + "_" + 26-char lowercase Crockford-base32
# ULID suffix. The Crockford lowercase alphabet excludes i/l/o/u.
EXPERIMENT_ID_PATTERN = r"^exp_[0-9a-hjkmnp-tv-z]{26}$"
"""Grammar for `experiment_id` (spec/v0/02-data-model.md §1.6)."""
WORKER_ID_PATTERN = r"^wkr_[0-9a-hjkmnp-tv-z]{26}$"
"""Grammar for `worker_id` (spec/v0/02-data-model.md §1.6)."""
GROUP_ID_PATTERN = r"^grp_[0-9a-hjkmnp-tv-z]{26}$"
"""Grammar for `group_id` (spec/v0/02-data-model.md §1.6)."""
ACTOR_ID_PATTERN = r"^(admin|wkr_[0-9a-hjkmnp-tv-z]{26})$"
"""A caller that may be the admin principal or a worker (spec §1.6)."""
MEMBER_ID_PATTERN = r"^(wkr|grp)_[0-9a-hjkmnp-tv-z]{26}$"
"""A worker or group id — group members / Task.target.id (spec §1.6)."""


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

ExperimentId = Annotated[str, StringConstraints(pattern=EXPERIMENT_ID_PATTERN)]
"""Opaque, system-minted experiment identifier (spec §1.6)."""

WorkerId = Annotated[str, StringConstraints(pattern=WORKER_ID_PATTERN)]
"""Opaque, system-minted worker identifier (spec §1.6)."""

GroupId = Annotated[str, StringConstraints(pattern=GROUP_ID_PATTERN)]
"""Opaque, system-minted group identifier (spec §1.6)."""

ActorId = Annotated[str, StringConstraints(pattern=ACTOR_ID_PATTERN)]
"""Attribution caller: the admin principal or a worker (spec §1.6)."""

MemberId = Annotated[str, StringConstraints(pattern=MEMBER_ID_PATTERN)]
"""Group member / target id: a worker or a group (spec §1.6)."""


_NAME_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cs", "Cn", "Co"})


def _check_display_name(value: str) -> str:
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError("display name must be NFC-normalized")
    if not 1 <= len(value) <= 128:
        raise ValueError("display name must be 1..128 code points")
    if value != value.strip() or not value.strip():
        raise ValueError("display name must not lead/trail with, or consist only of, whitespace")
    for ch in value:
        if unicodedata.category(ch) in _NAME_FORBIDDEN_CATEGORIES:
            raise ValueError(
                "display name must not contain control / surrogate / unassigned / "
                "private-use code points"
            )
    return value


DisplayName = Annotated[str, AfterValidator(_check_display_name)]
"""Operator-supplied display label (spec/v0/02-data-model.md §1.7)."""


# --- opaque-id minting (Crockford base32 ULID, spec §1.6) ---
_CROCKFORD = "0123456789abcdefghjkmnpqrstvwxyz"


def _encode_crockford(value: int, length: int) -> str:
    chars: list[str] = []
    for _ in range(length):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def mint_ulid() -> str:
    """Mint a 26-char lowercase Crockford-base32 ULID.

    48-bit millisecond timestamp followed by 80 bits of randomness, so that
    lexicographic order approximates creation order (spec §1.6).
    """
    timestamp_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    value = (timestamp_ms << 80) | secrets.randbits(80)
    return _encode_crockford(value, 26)


def mint_opaque_id(prefix: str) -> str:
    """Mint an opaque id ``<prefix>_<26-char-ulid>`` (prefix in exp/wkr/grp)."""
    return f"{prefix}_{mint_ulid()}"


UriStr = Annotated[str, AfterValidator(_check_uri)]
"""URI string — must have a scheme, per RFC 3986 (schema ``format: uri``)."""

DurationStr = Annotated[str, Field(min_length=1), AfterValidator(_check_duration)]
"""ISO-8601 duration string (e.g. ``"PT2H"``); must parse to a positive ``timedelta``.

Mirrors the JSON Schema's ``format: duration`` field. Stored as a string for
schema↔model round-trip parity; the orchestrator's ``build_termination_policy``
re-parses to a ``timedelta`` at the boundary (schema ``format: duration``)."""

NotNone = BeforeValidator(_reject_none)
"""Annotate optional fields to reject explicit ``null`` on input while allowing absence."""
