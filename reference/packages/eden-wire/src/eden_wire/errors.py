"""Problem+json serde for the EDEN wire binding (spec/v0 chapter 7 §7).

Every non-2xx response from a conforming server carries a body
matching ``spec/v0/schemas/wire/error.schema.json``. This module
is the single source of truth for the two-way mapping between
``eden_storage.errors.StorageError`` subclasses and the
``eden://error/<name>`` URIs the binding specifies.

The server uses :func:`envelope_for_error` to serialize; the client
uses :func:`raise_for_envelope` to reconstruct. Round-trip stability
is a CI invariant.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from eden_storage.errors import (
    AlreadyExists,
    ConflictingResubmission,
    IllegalTransition,
    InvalidPrecondition,
    NotFound,
    StorageError,
    WrongToken,
)

__all__ = [
    "BadRequest",
    "ExperimentIdMismatch",
    "ProblemJson",
    "WireReferenceError",
    "Unauthorized",
    "WireError",
    "envelope_for_error",
    "envelope_for_reference_error",
    "raise_for_envelope",
]


class WireError(Exception):
    """Base class for wire-level errors that have no direct store analog."""


class BadRequest(WireError):
    """Request body failed schema validation."""


class ExperimentIdMismatch(WireError):
    """Header ``X-Eden-Experiment-Id`` disagreed with the URL segment."""


class WireReferenceError(Exception):
    """Base class for reference-only errors outside the normative vocabulary.

    Errors under this hierarchy live under ``eden://reference-error/…``
    and are not part of the ``07-wire-protocol.md`` §7 closed vocabulary
    (§12 "Reference-only extensions"). A conforming server is free to
    use a different auth scheme or none at all.
    """


class Unauthorized(WireReferenceError):
    """Reference shared-token middleware rejected the request."""


@dataclass(frozen=True)
class ProblemJson:
    """The RFC 7807 problem+json envelope in structured form."""

    type: str
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the envelope as a plain dict suitable for JSON serialization."""
        body: dict[str, Any] = {
            "type": self.type,
            "title": self.title,
            "status": self.status,
        }
        if self.detail is not None:
            body["detail"] = self.detail
        if self.instance is not None:
            body["instance"] = self.instance
        return body


_TYPE_BY_EXC: dict[type[Exception], tuple[str, int, str]] = {
    NotFound: ("eden://error/not-found", 404, "Not Found"),
    AlreadyExists: ("eden://error/already-exists", 409, "Already Exists"),
    IllegalTransition: (
        "eden://error/illegal-transition",
        409,
        "Illegal Transition",
    ),
    WrongToken: ("eden://error/wrong-token", 403, "Wrong Token"),
    ConflictingResubmission: (
        "eden://error/conflicting-resubmission",
        409,
        "Conflicting Resubmission",
    ),
    InvalidPrecondition: (
        "eden://error/invalid-precondition",
        409,
        "Invalid Precondition",
    ),
    BadRequest: ("eden://error/bad-request", 400, "Bad Request"),
    ExperimentIdMismatch: (
        "eden://error/experiment-id-mismatch",
        400,
        "Experiment ID Mismatch",
    ),
}

_EXC_BY_TYPE: dict[str, type[Exception]] = {
    wire_type: exc for exc, (wire_type, _, _) in _TYPE_BY_EXC.items()
}


def envelope_for_error(exc: Exception, *, instance: str | None = None) -> ProblemJson:
    """Build a :class:`ProblemJson` envelope for ``exc``.

    Raises ``ValueError`` for exceptions the binding does not cover.
    """
    entry = _TYPE_BY_EXC.get(type(exc))
    if entry is None:
        msg = f"no wire binding for exception {type(exc).__name__!r}"
        raise ValueError(msg)
    wire_type, status, title = entry
    detail = str(exc) if str(exc) else None
    return ProblemJson(
        type=wire_type,
        title=title,
        status=status,
        detail=detail,
        instance=instance,
    )


_REF_TYPE_BY_EXC: dict[type[Exception], tuple[str, int, str]] = {
    Unauthorized: (
        "eden://reference-error/unauthorized",
        401,
        "Unauthorized",
    ),
}

_REF_EXC_BY_TYPE: dict[str, type[Exception]] = {
    wire_type: exc for exc, (wire_type, _, _) in _REF_TYPE_BY_EXC.items()
}


def envelope_for_reference_error(
    exc: Exception, *, instance: str | None = None
) -> ProblemJson:
    """Build a :class:`ProblemJson` envelope for a reference-only error.

    Kept separate from :func:`envelope_for_error` so no refactor can
    accidentally leak a reference-only type into the normative
    ``_TYPE_BY_EXC`` vocabulary.
    """
    entry = _REF_TYPE_BY_EXC.get(type(exc))
    if entry is None:
        msg = f"no reference binding for exception {type(exc).__name__!r}"
        raise ValueError(msg)
    wire_type, status, title = entry
    detail = str(exc) if str(exc) else None
    return ProblemJson(
        type=wire_type,
        title=title,
        status=status,
        detail=detail,
        instance=instance,
    )


def raise_for_envelope(body: dict[str, Any]) -> None:
    """Reconstruct and raise the exception described by a problem+json body."""
    wire_type = body.get("type")
    if not isinstance(wire_type, str):
        msg = f"envelope missing 'type' string: {body!r}"
        raise ValueError(msg)
    exc_cls = _EXC_BY_TYPE.get(wire_type) or _REF_EXC_BY_TYPE.get(wire_type)
    if exc_cls is None:
        msg = f"unknown wire error type {wire_type!r}"
        raise WireError(msg)
    detail = body.get("detail") or body.get("title") or wire_type
    if issubclass(exc_cls, StorageError):
        raise exc_cls(detail)
    raise exc_cls(detail)
