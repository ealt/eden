"""FastAPI server that exposes a ``Store`` over the EDEN wire protocol.

:func:`make_app` takes a single ``Store`` and returns a fresh ``FastAPI``
instance that routes every ``/v0/experiments/{E}/...`` endpoint specified
in ``spec/v0/07-wire-protocol.md`` to the corresponding ``Store`` method.

The route handlers live in per-resource ``APIRouter`` modules under
:mod:`eden_wire.routers` (the F-3 regroup, issue #115); ``make_app`` is a
thin assembler that constructs the shared
:class:`eden_wire._dependencies.RouterDeps` once, installs the §13 auth
middleware + the app-level problem+json exception handlers, and includes
each router. The handlers themselves contain no business logic: every
endpoint is a thin adapter that validates the request, calls the store,
and serializes the result.

Error handling:

- Any ``StorageError`` raised by the store maps to the matching
  ``eden://error/<name>`` problem+json body via
  :func:`eden_wire.errors.envelope_for_error`.
- ``BadRequest`` covers schema-validation failures; FastAPI's
  ``RequestValidationError`` is caught and rewritten.
- ``ExperimentIdMismatch`` guards the header-vs-path invariant (§1.3).
"""

from __future__ import annotations

from pathlib import Path

from eden_checkpoint import CheckpointError
from eden_storage import Store
from eden_storage.errors import StorageError
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from ._dependencies import RouterDeps
from .auth import install_auth_middleware
from .errors import (
    BadRequest,
    ExperimentIdMismatch,
    Forbidden,
    ProblemJson,
    Unauthorized,
    WireReferenceError,
    envelope_for_error,
    envelope_for_reference_error,
)
from .routers import (
    checkpoints,
    dispatch_mode,
    events,
    experiment_lifecycle,
    experiment_read,
    groups,
    ideas,
    reference,
    tasks,
    variants,
    workers,
)

PROBLEM_JSON = "application/problem+json"


def make_app(
    store: Store,
    *,
    subscribe_timeout: float = 30.0,
    subscribe_poll_interval: float = 0.1,
    admin_token: str | None = None,
    artifacts_dir: Path | str | None = None,
    checkpoint_experiment_config: str | None = None,
    checkpoint_repo_path: Path | str | None = None,
    checkpoint_import_credentials_dir: Path | str | None = None,
) -> FastAPI:
    """Build a FastAPI app that exposes ``store`` over the wire binding.

    The app is stateless beyond the injected ``store``; multiple apps for
    different experiments can coexist in one process, each with their own
    ``Store`` instance (the per-resource routers bind to a single
    per-app :class:`RouterDeps` and hold no module-level state).

    ``subscribe_timeout`` is the long-poll window per
    ``07-wire-protocol.md`` §8.2 (default 30s). Tests typically pass a
    short value. ``subscribe_poll_interval`` is how often the server
    re-checks the event log for new entries; finer values reduce latency
    at the cost of CPU.

    ``admin_token``, when non-``None``, installs the §13 normative
    authentication middleware: every ``/v0/`` request MUST carry a valid
    ``Authorization: Bearer <principal>:<secret>`` header where the
    principal is either ``admin`` (matched against ``admin_token``
    constant-time) or a registered ``worker_id`` (verified against the
    Store's ``verify_worker_credential``). ``None`` (test / in-process
    default) disables auth — convenient for unit tests but NOT
    spec-conformant for a deployed server.

    ``artifacts_dir``, when non-``None``, enables the 12a-1f
    reference-only artifact-serving route at
    ``/_reference/experiments/{experiment_id}/artifacts/{path:path}``.
    The route is ALWAYS mounted regardless; when ``artifacts_dir`` is
    ``None`` every request to it returns 503
    ``eden://reference-error/artifact-serving-disabled``. See
    ``spec/v0/reference-bindings/worker-host-subprocess.md`` §9 for the
    substrate-access posture this route supports.
    """
    app = FastAPI(
        title=f"EDEN task store — {store.experiment_id}",
        version="0",
    )

    deps = RouterDeps(
        store=store,
        admin_token=admin_token,
        subscribe_timeout=subscribe_timeout,
        subscribe_poll_interval=subscribe_poll_interval,
        artifact_root=Path(artifacts_dir) if artifacts_dir is not None else None,
        checkpoint_repo_root=(
            Path(checkpoint_repo_path) if checkpoint_repo_path is not None else None
        ),
        checkpoint_config_text=checkpoint_experiment_config or "",
        credentials_dir_root=(
            Path(checkpoint_import_credentials_dir)
            if checkpoint_import_credentials_dir is not None
            else None
        ),
    )

    if admin_token is not None:
        install_auth_middleware(app, admin_token=admin_token, store=store)

    _install_exception_handlers(app)

    # Include order mirrors the pre-F-3 file's registration order. Path-
    # segment scoping makes the order non-load-bearing (every route has a
    # unique fully-qualified path; ``{experiment_id}`` matches a single
    # segment), but matching the file order keeps the no-behavior-change
    # discipline visible.
    app.include_router(tasks.build_router(deps))
    app.include_router(ideas.build_router(deps))
    app.include_router(variants.build_router(deps))
    app.include_router(dispatch_mode.build_router(deps))
    app.include_router(experiment_lifecycle.build_router(deps))
    app.include_router(events.build_router(deps))
    app.include_router(workers.build_router(deps))
    app.include_router(groups.build_router(deps))
    app.include_router(experiment_read.build_router(deps))
    app.include_router(checkpoints.build_router(deps))
    app.include_router(reference.build_router(deps))

    return app


def _problem(
    status: int, type_: str, title: str, detail: str, instance: str
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        media_type=PROBLEM_JSON,
        content={
            "type": type_,
            "title": title,
            "status": status,
            "detail": detail,
            "instance": instance,
        },
    )


# The exception types whose problem+json envelope is built uniformly by
# ``envelope_for_error`` (chapter-7 error vocabulary). Registered against
# the shared :func:`_error_envelope_handler`. Note: ``BadRequest`` /
# ``ExperimentIdMismatch`` / ``Unauthorized`` / ``Forbidden`` are
# ``WireError`` subclasses but are registered individually (not the base
# ``WireError``) so the registered set matches the pre-F-3 surface
# exactly — an unrecognized ``WireError`` still falls through to 500.
_ENVELOPE_ERROR_TYPES: tuple[type[Exception], ...] = (
    StorageError,
    BadRequest,
    ExperimentIdMismatch,
    Unauthorized,
    Forbidden,
)


def _envelope_json(envelope: ProblemJson) -> JSONResponse:
    return JSONResponse(
        status_code=envelope.status,
        media_type=PROBLEM_JSON,
        content=envelope.to_dict(),
    )


async def _error_envelope_handler(request: Request, exc: Exception) -> JSONResponse:
    """Uniform problem+json handler for the chapter-7 error vocabulary."""
    return _envelope_json(envelope_for_error(exc, instance=str(request.url)))


async def _reference_error_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    # 12a-1f: the artifact route raises reference-only WireReferenceError
    # subclasses (InvalidPath / ArtifactTooLarge / ArtifactServingDisabled);
    # without this handler they'd fall through to FastAPI's default 500.
    # Delegates to envelope_for_reference_error which knows the
    # eden://reference-error/... URI mappings.
    return _envelope_json(
        envelope_for_reference_error(exc, instance=str(request.url))
    )


async def _request_validation_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    return _problem(
        400,
        "eden://error/bad-request",
        "Bad Request",
        "; ".join(str(e) for e in exc.errors()),
        str(request.url),
    )


async def _pydantic_validation_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    assert isinstance(exc, ValidationError)
    return _problem(
        400,
        "eden://error/bad-request",
        "Bad Request",
        exc.errors()[0].get("msg", "validation error")
        if exc.errors()
        else "validation error",
        str(request.url),
    )


async def _checkpoint_error_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    # Most CheckpointError subclasses (CheckpointInvalid,
    # ExperimentIdConflict, SpecVersionMismatch,
    # UnsupportedCheckpointVersion) have direct entries in _TYPE_BY_EXC.
    # CheckpointExperimentIdMismatch is converted in the import handler,
    # so by the time we get here only the registered subclasses arrive.
    try:
        envelope = envelope_for_error(exc, instance=str(request.url))
    except ValueError:
        # Defense in depth: an un-mapped CheckpointError surfaces as a
        # generic 400 rather than a 500.
        return _problem(
            400,
            "eden://error/checkpoint-invalid",
            "Checkpoint Invalid",
            str(exc) or type(exc).__name__,
            str(request.url),
        )
    return _envelope_json(envelope)


def _install_exception_handlers(app: FastAPI) -> None:
    """Wire the app-level problem+json exception handlers.

    Extracted from ``make_app`` (F-3 / audit L-D). Every handler is
    stateless beyond the request URL — the same ``StorageError`` raised
    in any router produces the same envelope, preserving the uniform
    error surface.
    """
    for exc_type in _ENVELOPE_ERROR_TYPES:
        app.add_exception_handler(exc_type, _error_envelope_handler)
    app.add_exception_handler(WireReferenceError, _reference_error_handler)
    app.add_exception_handler(RequestValidationError, _request_validation_handler)
    app.add_exception_handler(ValidationError, _pydantic_validation_handler)
    app.add_exception_handler(CheckpointError, _checkpoint_error_handler)
