"""Read-full-experiment route (chapter 7 §14.3): ``GET /v0/experiments/{id}``.

Split from the lifecycle routes (:mod:`eden_wire.routers.experiment_lifecycle`)
so ``make_app``'s include order matches the pre-F-3 file: this route was
registered after groups, whereas terminate / policy-errors / state were
registered before events.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, Request

from .._dependencies import RouterDeps, check_experiment


def build_router(deps: RouterDeps) -> APIRouter:
    """Return the experiment-read ``APIRouter`` bound to ``deps``."""
    router = APIRouter(prefix="/v0/experiments/{experiment_id}")
    router.get("")(_read_experiment(deps))
    return router


def _read_experiment(deps: RouterDeps):
    async def read_experiment(
        request: Request,
        experiment_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        """Chapter 7 §14.3: read the full experiment runtime object.

        Either-auth (any registered worker MAY read, parallel to the
        §2.9 ``GET /state`` companion read). Returns ``state`` +
        ``created_at`` + ``base_commit_sha`` + ``imported_from`` per
        ``spec/v0/schemas/experiment.schema.json``; the ``imported_from``
        field is the recovery-probe anchor for the lost-import-response
        case in chapter 10 §10. ``base_commit_sha`` (the seed commit,
        chapter 2 §2.5) lets the orchestrator create the baseline variant
        (§9.4). The orchestrator's per-iteration
        ``ExperimentStateView.experiment_created_at`` is the other
        consumer; restricting this surface to admin-only would 403 the
        orchestrator's worker bearer and break the dispatch loop (caught
        by the wave-5 smoke regression). See §14 intro for the
        bootstrap-class boundary rationale.
        """
        check_experiment(deps, experiment_id, x_eden_experiment_id)
        # Auth middleware (when admin_token is set) has already
        # authenticated the principal; either-auth means we accept any
        # registered principal class. No additional gate here.
        # exclude_none=False keeps `imported_from: null` on the wire (its
        # schema's oneOf permits explicit null — the recovery probe relies
        # on it). base_commit_sha's schema is `type: string` (no null), so
        # it MUST be omitted rather than serialized as null when absent.
        body = deps.store.read_experiment().model_dump(
            mode="json", exclude_none=False
        )
        if body.get("base_commit_sha") is None:
            body.pop("base_commit_sha", None)
        return body

    return read_experiment
