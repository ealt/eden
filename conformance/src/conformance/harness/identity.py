"""Opaque-id minting for the conformance harness (identity rename #128).

After the identity rename, ``experiment_id`` / ``worker_id`` /
``group_id`` are opaque, system-minted ids of shape
``<prefix>_<26-char-ulid>`` (spec/v0/02-data-model.md §1.6). Worker
and group ids are minted server-side and flow back through the wire,
so the harness never mints those. Experiment ids, however, are chosen
by whoever starts the IUT (the ``--experiment-id`` the adapter passes,
and the ``X-Eden-Experiment-Id`` header the suite sends), so the
harness needs to mint a grammar-valid ``exp_*`` per scenario.

This module is intentionally self-contained — the conformance suite
stays IUT-agnostic (chapter 9 §6) and must NOT import any reference
package (``eden_contracts`` et al.). The minter below mirrors the
reference impl's Crockford-base32 ULID encoding for the suffix.
"""

from __future__ import annotations

import secrets
import time

# Crockford base32 lowercase alphabet (no i, l, o, u) — matches the
# ``[0-9a-hjkmnp-tv-z]`` char class the opaque-id grammar enforces.
_CROCKFORD = "0123456789abcdefghjkmnpqrstvwxyz"


def _encode_crockford(value: int, length: int) -> str:
    chars: list[str] = []
    for _ in range(length):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def mint_ulid() -> str:
    """26-char lowercase Crockford-base32 ULID (48-bit ms ts + 80-bit random)."""
    timestamp_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    value = (timestamp_ms << 80) | secrets.randbits(80)
    return _encode_crockford(value, 26)


def mint_experiment_id() -> str:
    """Mint a grammar-valid opaque experiment id (``exp_<ulid>``)."""
    return f"exp_{mint_ulid()}"
