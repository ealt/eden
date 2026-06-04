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

from eden_checkpoint import (
    CheckpointInvalid,
    ExperimentIdConflict,
    SpecVersionMismatch,
    UnsupportedCheckpointVersion,
)
from eden_storage.errors import (
    AlreadyExists,
    ConflictingResubmission,
    CycleDetected,
    IllegalTransition,
    InvalidPrecondition,
    NoOpVariant,
    NotClaimed,
    NotFound,
    ReservedIdentifier,
    StorageError,
    WorkerNotEligible,
    WorkerNotRegistered,
    WrongClaimant,
)

__all__ = [
    "ArtifactServingDisabled",
    "ArtifactTooLarge",
    "BadRequest",
    "ExperimentIdMismatch",
    "Forbidden",
    "InvalidPath",
    "PayloadTooLarge",
    "ProblemJson",
    "Unauthorized",
    "WireError",
    "WireReferenceError",
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


class Unauthorized(WireError):
    """Authentication failed.

    Per [`spec/v0/07-wire-protocol.md`](../../../../spec/v0/07-wire-protocol.md)
    §13, every ``/v0/`` request MUST carry a valid ``Authorization:
    Bearer <principal>:<secret>`` header; servers reject requests
    without one (or with a malformed one) using ``eden://error/unauthorized``
    (HTTP 401). 12a-1 made this error normative — pre-12a-1 it lived
    under the informative ``eden://reference-error/...`` namespace.
    """


class Forbidden(WireError):
    """Authentication succeeded but the principal is not authorized for this endpoint.

    Used for principal-class mismatches per
    [`spec/v0/07-wire-protocol.md`](../../../../spec/v0/07-wire-protocol.md)
    §13.3 (e.g. a worker bearer hitting an admin-gated route, or
    vice versa), and for the §16.2 row-scoped `fetch_artifact` ACL miss
    (a worker that is neither the artifact's depositor nor admin-class).
    Returns HTTP 403.
    """


class PayloadTooLarge(WireError):
    """A `deposit_artifact` upload exceeded the configured size cap.

    Raised by the §16.1 deposit handler when the streamed multipart read
    crosses ``--max-artifact-bytes``. Normative (closed §9 vocabulary),
    distinct from the reference-only ``ArtifactTooLarge`` (the 1 MiB
    inline-render cap on the retired ``_reference`` serve route). Maps to
    HTTP 413 ``eden://error/payload-too-large``.
    """


class WireReferenceError(Exception):
    """Base class for reference-only errors outside the normative vocabulary.

    Errors under this hierarchy live under ``eden://reference-error/…``
    and are not part of the ``07-wire-protocol.md`` §9 closed vocabulary.
    A conforming server is free to use a different transport-level
    extension scheme.
    """


class InvalidPath(WireReferenceError):
    """The request path contains a malformed component.

    Raised by the 12a-1f artifact route's pre-FS component-walk guard
    when a path part is ``..``, an empty segment (from a leading,
    trailing, or doubled slash), or contains a NUL byte. The route
    rejects these BEFORE any filesystem call to avoid leaking the
    layout of the artifacts directory via timing or response-code
    differences. Maps to HTTP 400
    ``eden://reference-error/invalid-path``.
    """


class ArtifactTooLarge(WireReferenceError):
    """The requested artifact exceeds the 1 MiB cap.

    Raised by the 12a-1f artifact route when ``os.fstat`` on the
    opened file fd reports a size larger than ``MAX_ARTIFACT_BYTES``
    (1 MiB). The cap mirrors the existing
    ``_read_inline_artifact`` helper and exists because the route
    uses a fixed-bytes ``Response(content=…)`` model — Phase 13d's
    ``Backend`` abstraction will handle streaming + range requests
    properly. Maps to HTTP 413
    ``eden://reference-error/artifact-too-large``.
    """


class ArtifactServingDisabled(WireReferenceError):
    """The task-store-server was started without ``--artifacts-dir``.

    Raised by the 12a-1f artifact route when no artifacts directory
    is configured. The route is **always mounted** regardless of
    configuration; this error signals to operators that the
    deployment opted out of artifact serving (returning 404 would
    be ambiguous with "file not found"). Maps to HTTP 503
    ``eden://reference-error/artifact-serving-disabled``.
    """


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
    NotClaimed: ("eden://error/not-claimed", 409, "Not Claimed"),
    WrongClaimant: ("eden://error/wrong-claimant", 403, "Wrong Claimant"),
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
    NoOpVariant: (
        "eden://error/no-op-variant",
        409,
        "No-Op Variant",
    ),
    WorkerNotRegistered: (
        "eden://error/worker-not-registered",
        403,
        "Worker Not Registered",
    ),
    WorkerNotEligible: (
        "eden://error/worker-not-eligible",
        403,
        "Worker Not Eligible",
    ),
    ReservedIdentifier: (
        "eden://error/reserved-identifier",
        409,
        "Reserved Identifier",
    ),
    CycleDetected: ("eden://error/cycle-detected", 409, "Cycle Detected"),
    BadRequest: ("eden://error/bad-request", 400, "Bad Request"),
    ExperimentIdMismatch: (
        "eden://error/experiment-id-mismatch",
        400,
        "Experiment ID Mismatch",
    ),
    Unauthorized: ("eden://error/unauthorized", 401, "Unauthorized"),
    Forbidden: ("eden://error/forbidden", 403, "Forbidden"),
    PayloadTooLarge: (
        "eden://error/payload-too-large",
        413,
        "Payload Too Large",
    ),
    # Portable-checkpoint errors per spec/v0/07-wire-protocol.md §9 and
    # spec/v0/10-checkpoints.md. The eden-checkpoint
    # ExperimentIdMismatch is NOT registered here — the import-endpoint
    # handler in server.py catches it and re-raises the wire-layer
    # ExperimentIdMismatch above so a single class maps the wire type.
    CheckpointInvalid: (
        "eden://error/checkpoint-invalid",
        400,
        "Checkpoint Invalid",
    ),
    ExperimentIdConflict: (
        "eden://error/experiment-id-conflict",
        409,
        "Experiment ID Conflict",
    ),
    SpecVersionMismatch: (
        "eden://error/spec-version-mismatch",
        409,
        "Spec Version Mismatch",
    ),
    UnsupportedCheckpointVersion: (
        "eden://error/unsupported-checkpoint-version",
        409,
        "Unsupported Checkpoint Version",
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
    InvalidPath: (
        "eden://reference-error/invalid-path",
        400,
        "Invalid Path",
    ),
    ArtifactTooLarge: (
        "eden://reference-error/artifact-too-large",
        413,
        "Artifact Too Large",
    ),
    ArtifactServingDisabled: (
        "eden://reference-error/artifact-serving-disabled",
        503,
        "Artifact Serving Disabled",
    ),
}
"""Reference-only error vocabulary.

Pre-12a-1 the reference impl shipped an informative shared-token
auth scheme that emitted ``eden://reference-error/unauthorized``;
that scheme has been replaced by the normative §13 per-worker /
admin auth, with errors emitted under the closed ``eden://error/…``
vocabulary. 12a-1f's reference-only artifact route at
``/_reference/experiments/{experiment_id}/artifacts/{path:path}``
is the first reference-error consumer post-12a-1; the three entries
above cover its non-normative failure modes.
"""

_REF_EXC_BY_TYPE: dict[str, type[Exception]] = {
    wire_type: exc for exc, (wire_type, _, _) in _REF_TYPE_BY_EXC.items()
}


def envelope_for_reference_error(
    exc: Exception, *, instance: str | None = None
) -> ProblemJson:
    """Build a :class:`ProblemJson` envelope for a reference-only error.

    Kept separate from :func:`envelope_for_error` so no refactor can
    accidentally leak a reference-only type into the normative
    ``_TYPE_BY_EXC`` vocabulary. The reference table is currently
    empty (see :data:`_REF_TYPE_BY_EXC`).
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
