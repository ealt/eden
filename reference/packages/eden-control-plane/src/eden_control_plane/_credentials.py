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
from eden_storage.errors import (
    InvalidPrecondition,
    ReservedIdentifier,
)

RESERVED_IDENTIFIERS: frozenset[str] = frozenset({"admin", "system", "internal"})
"""Reserved identifiers from chapter 02 §6.1; rejected for worker/group ids."""

_WORKER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
"""Worker/group id grammar from chapter 02 §6.1."""


def validate_registry_id(value: str, *, kind: str) -> None:
    """Reject reserved or grammar-violating ids.

    `kind` differentiates "worker" / "group" / "member" only for the
    error message; all three share the §6.1 grammar.
    """
    if value in RESERVED_IDENTIFIERS:
        raise ReservedIdentifier(
            f"{kind} id {value!r} is reserved by the protocol"
        )
    if not _WORKER_ID_RE.fullmatch(value):
        raise InvalidPrecondition(
            f"{kind} id {value!r} does not match the chapter 02 §6.1 grammar"
        )


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
