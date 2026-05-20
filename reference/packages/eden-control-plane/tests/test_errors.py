"""`raise_for_control_plane_envelope` routes the chapter 07 §9 vocabulary.

The four chapter 11 §4.5 lease error codes route to the typed
exceptions in `eden_control_plane.errors`; every other code defers
to `eden_wire.errors.raise_for_envelope` so a single closed
vocabulary spans the per-experiment and control-plane surfaces.
"""

from __future__ import annotations

import pytest
from eden_control_plane import (
    LeaseExpired,
    LeaseHeldByOther,
    LeaseInstanceMismatch,
    LeaseNotHeld,
    raise_for_control_plane_envelope,
)
from eden_storage.errors import AlreadyExists, InvalidPrecondition, NotFound
from eden_wire.errors import Forbidden, Unauthorized, WireError


@pytest.mark.parametrize(
    ("wire_type", "exc"),
    [
        ("eden://error/lease-held-by-other", LeaseHeldByOther),
        ("eden://error/lease-not-held", LeaseNotHeld),
        ("eden://error/lease-expired", LeaseExpired),
        ("eden://error/lease-instance-mismatch", LeaseInstanceMismatch),
    ],
)
def test_lease_codes_route_to_typed_exceptions(
    wire_type: str, exc: type[Exception]
) -> None:
    body = {
        "type": wire_type,
        "title": "x",
        "status": 409,
        "detail": "some detail",
    }
    with pytest.raises(exc) as exc_info:
        raise_for_control_plane_envelope(body)
    assert "some detail" in str(exc_info.value)


@pytest.mark.parametrize(
    ("wire_type", "exc"),
    [
        ("eden://error/not-found", NotFound),
        ("eden://error/already-exists", AlreadyExists),
        ("eden://error/invalid-precondition", InvalidPrecondition),
        ("eden://error/unauthorized", Unauthorized),
        ("eden://error/forbidden", Forbidden),
    ],
)
def test_existing_vocabulary_defers_to_eden_wire(
    wire_type: str, exc: type[Exception]
) -> None:
    body = {"type": wire_type, "title": "x", "status": 403, "detail": "d"}
    with pytest.raises(exc):
        raise_for_control_plane_envelope(body)


def test_unknown_type_raises_wire_error() -> None:
    body = {"type": "eden://error/no-such-thing", "title": "x", "status": 500}
    with pytest.raises(WireError):
        raise_for_control_plane_envelope(body)


def test_envelope_missing_type_raises_value_error() -> None:
    with pytest.raises(ValueError, match="missing 'type'"):
        raise_for_control_plane_envelope({"title": "x", "status": 500})


def test_lease_error_hierarchy() -> None:
    """All four lease errors share the LeaseError base class."""
    from eden_control_plane import LeaseError

    assert issubclass(LeaseHeldByOther, LeaseError)
    assert issubclass(LeaseNotHeld, LeaseError)
    assert issubclass(LeaseExpired, LeaseError)
    assert issubclass(LeaseInstanceMismatch, LeaseError)
    assert issubclass(LeaseError, WireError)
