"""Wire-error classes for the control plane.

Chapter 07 §9 adds four new closed-vocabulary error codes for the
control-plane lease operations:

| `type`                                  | HTTP | Class               |
|-----------------------------------------|------|---------------------|
| `eden://error/lease-held-by-other`      | 409  | `LeaseHeldByOther`  |
| `eden://error/lease-not-held`           | 410  | `LeaseNotHeld`      |
| `eden://error/lease-expired`            | 410  | `LeaseExpired`      |
| `eden://error/lease-instance-mismatch`  | 409  | `LeaseInstanceMismatch` |

`raise_for_control_plane_envelope` is the client-side reconstructor:
when a control-plane response carries a problem+json body, it raises
the appropriate exception. The function defers to eden-wire's
`raise_for_envelope` for the existing closed-vocabulary codes
(`not-found`, `already-exists`, `invalid-precondition`,
`unauthorized`, `forbidden`, `bad-request`, etc.) so a single closed
vocabulary spans both the per-experiment and control-plane surfaces.
"""

from __future__ import annotations

from typing import Any

from eden_wire.errors import WireError
from eden_wire.errors import raise_for_envelope as _raise_for_wire_envelope

__all__ = [
    "LeaseError",
    "LeaseExpired",
    "LeaseHeldByOther",
    "LeaseInstanceMismatch",
    "LeaseNotHeld",
    "raise_for_control_plane_envelope",
]


class LeaseError(WireError):
    """Base class for the four chapter 11 §4.5 lease wire errors."""


class LeaseHeldByOther(LeaseError):
    """`acquire_lease` against an experiment whose lease is still active.

    Per `spec/v0/11-control-plane.md` §4.5: a fresh acquire MUST
    succeed only when no lease exists OR the existing lease is
    expired. Returns HTTP 409
    `eden://error/lease-held-by-other`.
    """


class LeaseNotHeld(LeaseError):
    """`renew_lease` / `release_lease` against a `lease_id` that has been replaced.

    Per `spec/v0/11-control-plane.md` §4.5: a renew or release MUST
    succeed only when the stored `lease_id` matches the caller's.
    Returns HTTP 410 `eden://error/lease-not-held`.
    """


class LeaseExpired(LeaseError):
    """`renew_lease` against a lease whose `expires_at < now`, not yet replaced.

    Distinct from `LeaseNotHeld` because the lease still nominally
    belongs to the caller — the replacement hasn't happened yet. Per
    `spec/v0/11-control-plane.md` §4.5. Returns HTTP 410
    `eden://error/lease-expired`.
    """


class LeaseInstanceMismatch(LeaseError):
    """A lease op's `holder_instance` does not match the stored value.

    The §4.7 fencing rule: a caller's `holder_instance` MUST match
    the lease's stored value or the op MUST be rejected. Returns
    HTTP 409 `eden://error/lease-instance-mismatch`.
    """


_LEASE_EXC_BY_TYPE: dict[str, type[LeaseError]] = {
    "eden://error/lease-held-by-other": LeaseHeldByOther,
    "eden://error/lease-not-held": LeaseNotHeld,
    "eden://error/lease-expired": LeaseExpired,
    "eden://error/lease-instance-mismatch": LeaseInstanceMismatch,
}


def raise_for_control_plane_envelope(body: dict[str, Any]) -> None:
    """Raise the exception described by a control-plane problem+json body.

    Routes the four chapter 11 lease error codes to this module's
    classes and every other code to eden-wire's existing dispatch.
    The chapter 07 §9 vocabulary is closed across both per-experiment
    and control-plane surfaces; a `type` outside the union is a
    binding-level violation that surfaces as `WireError`.
    """
    wire_type = body.get("type")
    if isinstance(wire_type, str):
        lease_cls = _LEASE_EXC_BY_TYPE.get(wire_type)
        if lease_cls is not None:
            detail = body.get("detail") or body.get("title") or wire_type
            raise lease_cls(detail)
    _raise_for_wire_envelope(body)
