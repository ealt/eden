"""Variant routes (chapter 7 §4-§5).

Create / list / read, plus the declare-evaluation-error and integrate
transitions.
"""

from __future__ import annotations

from typing import Any

from eden_contracts import Variant
from fastapi import APIRouter, Body, Header, Query, Request
from fastapi.responses import Response
from pydantic import ValidationError

from .._dependencies import RouterDeps, check_experiment, enforce_in_any_group, enforce_worker
from ..errors import BadRequest
from ..models import IntegrateRequest


def build_router(deps: RouterDeps) -> APIRouter:
    """Return the variants ``APIRouter`` bound to ``deps``."""
    router = APIRouter(prefix="/v0/experiments/{experiment_id}/variants")
    router.post("")(_create_variant(deps))
    router.get("")(_list_variants(deps))
    router.get("/{variant_id}")(_read_variant(deps))
    router.post("/{variant_id}/declare-evaluation-error")(
        _declare_variant_eval_error(deps)
    )
    router.post("/{variant_id}/integrate")(_integrate_variant(deps))
    return router


def _create_variant(deps: RouterDeps):
    async def create_variant(
        request: Request,
        experiment_id: str,
        body: dict[str, Any] = Body(...),
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        check_experiment(deps, experiment_id, x_eden_experiment_id)
        enforce_worker(deps, request)
        try:
            variant = Variant.model_validate(body)
        except ValidationError as exc:
            raise BadRequest(str(exc)) from exc
        deps.store.create_variant(variant)
        # §4: response body matches variant.schema.json.
        return deps.store.read_variant(variant.variant_id).model_dump(
            mode="json", exclude_none=True
        )

    return create_variant


def _list_variants(deps: RouterDeps):
    async def list_variants(
        experiment_id: str,
        status: str | None = Query(None),
        x_eden_experiment_id: str | None = Header(None),
    ) -> list[dict[str, Any]]:
        check_experiment(deps, experiment_id, x_eden_experiment_id)
        return [
            t.model_dump(mode="json", exclude_none=True)
            for t in deps.store.list_variants(status=status)
        ]

    return list_variants


def _read_variant(deps: RouterDeps):
    async def read_variant(
        experiment_id: str,
        variant_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        check_experiment(deps, experiment_id, x_eden_experiment_id)
        return deps.store.read_variant(variant_id).model_dump(
            mode="json", exclude_none=True
        )

    return read_variant


def _declare_variant_eval_error(deps: RouterDeps):
    async def declare_variant_eval_error(
        request: Request,
        experiment_id: str,
        variant_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> Response:
        check_experiment(deps, experiment_id, x_eden_experiment_id)
        enforce_worker(deps, request)
        deps.store.declare_variant_evaluation_error(variant_id)
        return Response(status_code=204)

    return declare_variant_eval_error


def _integrate_variant(deps: RouterDeps):
    async def integrate_variant(
        request: Request,
        experiment_id: str,
        variant_id: str,
        body: IntegrateRequest,
        x_eden_experiment_id: str | None = Header(None),
    ) -> Response:
        check_experiment(deps, experiment_id, x_eden_experiment_id)
        # §4 / §5: integration is the orchestrator role's job; the 12a-2
        # authority table pins the caller to `orchestrators`.
        enforce_in_any_group(deps, request, ("orchestrators",))
        # §5: 200 + empty body on success and same-value idempotent
        # retries; 409 invalid-precondition on different-SHA divergence
        # (raised by Store.integrate_variant).
        deps.store.integrate_variant(variant_id, body.variant_commit_sha)
        return Response(status_code=200)

    return integrate_variant
