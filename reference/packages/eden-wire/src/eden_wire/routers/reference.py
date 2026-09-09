"""Reference-only (non-normative) routes.

- ``GET /_reference/experiments/{id}/tasks/{task_id}/validate-terminal``
- ``POST /_reference/experiments/{id}/validate/evaluation``
- ``GET /_reference/experiments/{id}/artifacts/{path:path}`` (12a-1f)
- ``POST`` / ``GET /_reference/experiments/{id}/cost`` (issue #343)

The auth middleware skips ``/_reference/`` paths (see
``eden_wire.auth.install_auth_middleware``), so the artifact and cost
handlers do their OWN bearer-auth check via ``authenticate(...)``. The
two validate-* helpers predate that posture and stay unauthenticated:
they are pure validators over caller-supplied input and read no stored
state. The cost routes are gated because one of them *writes*.

The descriptor-walk artifact primitives live in
:mod:`eden_wire._artifact_fd`.
"""

from __future__ import annotations

from typing import Any, cast

from eden_storage import CostEntry, CostLedger
from fastapi import APIRouter, Header, Request
from fastapi.responses import Response

from .._artifact_fd import artifact_response_headers, open_and_read_artifact
from .._dependencies import RouterDeps, check_experiment
from ..auth import authenticate
from ..errors import ArtifactServingDisabled, ExperimentIdMismatch
from ..models import (
    CostEntriesResponse,
    ValidateEvaluationRequest,
    ValidateTerminalResponse,
)


def build_router(deps: RouterDeps) -> APIRouter:
    """Return the reference-helpers ``APIRouter`` bound to ``deps``."""
    router = APIRouter(prefix="/_reference/experiments/{experiment_id}")
    router.get("/tasks/{task_id}/validate-terminal")(_validate_terminal(deps))
    router.post("/validate/evaluation")(_validate_evaluation(deps))
    router.get("/artifacts/{path:path}")(_serve_artifact(deps))
    router.post("/cost", status_code=204)(_record_cost(deps))
    router.get("/cost")(_list_cost_entries(deps))
    return router


def _authenticate_reference(deps: RouterDeps, request: Request) -> None:
    """Bearer-auth a ``/_reference/`` route the middleware skipped.

    ``admin_token is None`` is the test / in-process posture in which the
    whole wire runs unauthenticated — same gate the §16 artifact routes
    and :func:`_serve_artifact` apply.
    """
    if deps.admin_token is not None:
        authenticate(
            request.headers.get("authorization"),
            admin_token=deps.admin_token,
            store=deps.store,
        )


def _record_cost(deps: RouterDeps):
    async def record_cost(
        experiment_id: str,
        entry: CostEntry,
        request: Request,
        x_eden_experiment_id: str | None = Header(None),
    ) -> Response:
        # Auth-first, before any store access — the write is
        # worker-or-admin, matching who spends money in a deployment.
        _authenticate_reference(deps, request)
        check_experiment(deps, experiment_id, x_eden_experiment_id)
        cast(CostLedger, deps.store).record_cost(entry)
        return Response(status_code=204)

    return record_cost


def _list_cost_entries(deps: RouterDeps):
    async def list_cost_entries(
        experiment_id: str,
        request: Request,
        role: str | None = None,
        variant_id: str | None = None,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        _authenticate_reference(deps, request)
        check_experiment(deps, experiment_id, x_eden_experiment_id)
        entries = cast(CostLedger, deps.store).list_cost_entries(
            role=role, variant_id=variant_id
        )
        # exclude_none on each entry keeps absent figures absent rather
        # than emitting nulls the ledger never recorded.
        return CostEntriesResponse(entries=entries).model_dump(
            mode="json", exclude_none=True
        )

    return list_cost_entries


def _validate_terminal(deps: RouterDeps):
    async def validate_terminal(
        experiment_id: str,
        task_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        check_experiment(deps, experiment_id, x_eden_experiment_id)
        decision, reason = deps.store.validate_terminal(task_id)
        return ValidateTerminalResponse(
            decision=decision, reason=reason
        ).model_dump(mode="json", exclude_none=True)

    return validate_terminal


def _validate_evaluation(deps: RouterDeps):
    async def validate_evaluation(
        experiment_id: str,
        body: ValidateEvaluationRequest,
        x_eden_experiment_id: str | None = Header(None),
    ) -> Response:
        check_experiment(deps, experiment_id, x_eden_experiment_id)
        deps.store.validate_evaluation(body.evaluation)
        return Response(status_code=204)

    return validate_evaluation


def _serve_artifact(deps: RouterDeps):
    async def serve_artifact(
        experiment_id: str,
        path: str,
        request: Request,
    ) -> Response:
        # 1. Auth-first. NEVER touch the filesystem before auth so timing
        #    / response-code differences on unauth requests can't leak
        #    existence-of-files. When admin_token is None (test /
        #    in-process posture), auth is disabled — same posture the rest
        #    of the wire takes.
        if deps.admin_token is not None:
            authenticate(
                request.headers.get("authorization"),
                admin_token=deps.admin_token,
                store=deps.store,
            )
        # 2. Experiment-id mismatch guard (chapter-7 §1.3 parity).
        if experiment_id != deps.store.experiment_id:
            raise ExperimentIdMismatch(
                f"URL segment {experiment_id!r} does not match server's "
                f"experiment {deps.store.experiment_id!r}"
            )
        # 3. Disabled-deployment guard.
        if deps.artifact_root is None:
            raise ArtifactServingDisabled(
                "task-store-server started without --artifacts-dir"
            )
        # 4-7. Open + read with all path-traversal / symlink / size-cap
        # guards. See `open_and_read_artifact`.
        data = open_and_read_artifact(deps.artifact_root, path)
        # 8. Return with safe-delivery headers. See
        #    `artifact_response_headers` for the Content-Disposition +
        #    nosniff posture.
        return Response(
            content=data,
            media_type="application/octet-stream",
            headers=artifact_response_headers(path),
        )

    return serve_artifact
