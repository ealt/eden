"""SHA-256 helpers for content-addressed artifacts.

The content-addressed scheme uses lowercase hex SHA-256 per
``spec/v0/10-checkpoints.md`` §7.
"""

from __future__ import annotations

import hashlib

_HEX_RE_LEN = 64
"""Length of a lowercase-hex SHA-256 digest."""


def sha256_hex(data: bytes) -> str:
    """Return the lowercase-hex SHA-256 digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


def is_valid_sha256_hex(value: str) -> bool:
    """Return True iff ``value`` is a 64-char lowercase-hex string."""
    if len(value) != _HEX_RE_LEN:
        return False
    return all(c in "0123456789abcdef" for c in value)
