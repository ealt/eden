"""Shared credential + identifier helpers for the control-plane backends.

Mirrors the helpers in `eden_storage._base` for the per-experiment
registry but lives in this package so the control plane does not
import from eden-storage internals. A future refactor MAY extract a
shared `eden-identity` package; for now, the duplication is bounded
to argon2id hashing, grammar validation, and reserved-identifier
checks — all small.
"""

from __future__ import annotations

import contextlib
import re
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from eden_contracts import mint_opaque_id
from eden_contracts._common import MEMBER_ID_PATTERN, _check_display_name
from eden_storage.errors import (
    InvalidName,
    InvalidPrecondition,
    ReservedIdentifier,
)

_MEMBER_ID_RE = re.compile(MEMBER_ID_PATTERN)
"""Group-member grammar: an opaque `wkr_*` OR `grp_*` (data-model §1.6)."""

__all__ = [
    "DEPLOYMENT_SCOPE_SENTINEL",
    "RESERVED_GROUP_NAMES",
    "RESERVED_WORKER_NAMES",
    "check_credential_hash",
    "constant_time_dummy_verify",
    "generate_credential_token",
    "hash_credential",
    "mint_opaque_id",
    "validate_display_name",
    "validate_group_name",
    "validate_member_id",
    "validate_worker_name",
]

DEPLOYMENT_SCOPE_SENTINEL: str = "exp_" + "0" * 26
"""Stable sentinel `exp_*` standing in for the deployment scope.

Deployment-scoped workers/groups (chapter 11 §6) are not bound to any
experiment, but the wire-visible `Worker` / `Group` shapes require an
`experiment_id` matching the §1.6 `exp_*` grammar. The reference impl
uses this fixed all-zeros opaque id (a valid `exp_*` value that is
never minted for a real experiment) to make the deployment scope
visible while satisfying the schema. Replaces the pre-rename
free-text `"<deployment>"` sentinel.
"""

RESERVED_WORKER_NAMES: frozenset[str] = frozenset({"admin", "system", "internal"})
"""Reserved worker NAMES from chapter 02 §2 (post-rename name-space).

The deployment-admin bearer principal stays the literal token ``admin``
but no ``worker_id`` is ever minted for it; these names are rejected
when supplied as an operator-chosen worker display name.
"""

RESERVED_GROUP_NAMES: frozenset[str] = frozenset({"admins", "orchestrators"})
"""Reserved group NAMES from chapter 02 §2 (post-rename name-space).

setup-experiment seeds the ``admins`` / ``orchestrators`` groups via the
privileged ``allow_reserved`` path; a later operator register with one of
these names collides and is rejected.
"""


def validate_display_name(value: str) -> str:
    """Validate an operator-supplied display name (NFC, 1..128, no control).

    Reuses the canonical `eden_contracts._common._check_display_name`
    so the control plane and the per-experiment registry share one
    grammar. A grammar violation raises `eden_storage.errors.InvalidName`,
    which the server layer maps to the wire 422
    ``eden://error/invalid-name``.
    """
    try:
        return _check_display_name(value)
    except ValueError as exc:
        raise InvalidName(f"invalid display name: {exc}") from exc


def validate_worker_name(name: str) -> str:
    """Validate a worker name and reject reserved worker names."""
    validated = validate_display_name(name)
    if validated in RESERVED_WORKER_NAMES:
        raise ReservedIdentifier(
            f"worker name {validated!r} is reserved by the protocol"
        )
    return validated


def validate_member_id(member_id: str) -> None:
    """Reject a group member that is not a real `wkr_*` / `grp_*` id.

    The cross-namespace check from chapter 02 §7.1: a member MUST
    resolve to an opaque worker or group id. A member id is not a
    display name, so a malformed one is an `InvalidPrecondition`,
    not an `InvalidName`.
    """
    if not _MEMBER_ID_RE.fullmatch(member_id):
        raise InvalidPrecondition(
            f"member id {member_id!r} does not match the member grammar "
            f"(expected wkr_* or grp_*)"
        )


def validate_group_name(name: str, *, allow_reserved: bool = False) -> str:
    """Validate a group name; reject reserved group names unless allowed.

    `allow_reserved=True` is the privileged setup-experiment seam that
    mints the reserved `admins` / `orchestrators` groups.
    """
    validated = validate_display_name(name)
    if not allow_reserved and validated in RESERVED_GROUP_NAMES:
        raise ReservedIdentifier(
            f"group name {validated!r} is reserved by the protocol"
        )
    return validated


def generate_credential_token() -> str:
    """Mint a fresh 256-bit registration token (URL-safe hex).

    `secrets.token_hex(32)` returns 64 hex chars / 256 bits of
    entropy. Hex is chosen over base64url so the token is safe to
    place after the `:` in the bearer format (chapter 07 §13.1) and
    safe to embed in command-line arguments / env vars without
    escape handling.
    """
    return secrets.token_hex(32)


# argon2id PasswordHasher with argon2-cffi's defaults. The slow-KDF
# properties are cited as the reference posture in chapter 07 §13.1.
_PASSWORD_HASHER = PasswordHasher()

# Dummy hash computed once at module-load so the unknown-worker branch
# of `verify_credential` can perform a real argon2id verify against it
# (constant-time compared to a real hit).
_UNKNOWN_WORKER_DUMMY_HASH: str = _PASSWORD_HASHER.hash(
    "eden-control-plane-unknown-worker-dummy"
)


def hash_credential(registration_token: str) -> str:
    """Return an argon2id-encoded PHC hash of `registration_token`."""
    return _PASSWORD_HASHER.hash(registration_token)


def check_credential_hash(registration_token: str, stored: str) -> bool:
    """Verify `registration_token` against `stored` (argon2id encoded)."""
    try:
        return _PASSWORD_HASHER.verify(stored, registration_token)
    except VerifyMismatchError:
        return False


def constant_time_dummy_verify(registration_token: str) -> None:
    """Run a verify against the class-level dummy hash, discard the result.

    Used by the unknown-worker branch of `verify_worker_credential`
    so the two failure modes (no such worker, wrong secret) incur
    the same argon2id cost.
    """
    with contextlib.suppress(VerifyMismatchError):
        _PASSWORD_HASHER.verify(_UNKNOWN_WORKER_DUMMY_HASH, registration_token)
