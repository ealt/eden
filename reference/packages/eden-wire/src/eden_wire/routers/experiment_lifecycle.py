"""Experiment-lifecycle routes (chapter 7 §2.9): terminate / policy-errors
/ state.

Registered between dispatch_mode and events in ``make_app`` to match the
pre-F-3 file's registration order; ``GET {base}`` (read full experiment)
lives separately in :mod:`eden_wire.routers.experiment_read`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import Response

from .._dependencies import RouterDeps, check_experiment, enforce_in_any_group
from ..models import ExperimentStateResponse, PolicyErrorRequest, TerminateRequest


def build_router(deps: RouterDeps) -> APIRouter:
    """Return the experiment-lifecycle ``APIRouter`` bound to ``deps``."""
    router = APIRouter(prefix="/v0/experiments/{experiment_id}")
    router.post("/terminate")(_terminate_experiment(deps))
    router.post("/policy-errors")(_emit_policy_error(deps))
    router.get("/state")(_read_experiment_state(deps))
    return router


def _terminate_experiment(deps: RouterDeps):
    async def terminate_experiment(
        request: Request,
        experiment_id: str,
        body: TerminateRequest,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        """§2.9 group-gated lifecycle transition (``admins`` OR ``orchestrators``).

        Gated on either group so both termination paths work over the
        wire: the operator-driven path (`admins` bearer) and the
        orchestrator's policy-driven termination ([`03-roles.md`] §6.2
        decision-type 0, an `orchestrators` bearer). Mirrors the
        ``accept`` / ``reject`` / ``emit_policy_error`` gating.

        Stamps ``terminated_by`` from the authenticated principal; the
        request body MUST NOT carry it (the model's ``extra="forbid"``
        rejects unknown keys). Idempotent on the terminated state
        (`04-task-protocol.md` §8.1) — a second call returns 200 with the
        existing experiment and emits no second event; the winning
        caller's ``reason`` is the one recorded.
        """
        check_experiment(deps, experiment_id, x_eden_experiment_id)
        terminated_by = enforce_in_any_group(
            deps, request, ("admins", "orchestrators")
        )
        experiment = deps.store.terminate_experiment(
            reason=body.reason, terminated_by=terminated_by
        )
        return experiment.model_dump(mode="json", exclude_none=True)

    return terminate_experiment


def _emit_policy_error(deps: RouterDeps):
    async def emit_policy_error(
        request: Request,
        experiment_id: str,
        body: PolicyErrorRequest,
        x_eden_experiment_id: str | None = Header(None),
    ) -> Response:
        """12a-3 wave-7 follow-up: emit ``experiment.policy_error``.

        Per [`03-roles.md`](../../../../spec/v0/03-roles.md) §6.2
        decision-type 0 fault-tolerance, when a termination policy raises
        the orchestrator MUST emit a registered
        ``experiment.policy_error`` event so operators see the failure in
        the event log. The orchestrator service runs against
        ``StoreClient`` (wire-bound), so the event needs a wire endpoint
        to land in the per-experiment log.

        Authority: ``orchestrators`` — the orchestrator instance is the
        only caller that produces these events. The endpoint is NOT
        exposed to ``admins`` to keep the event surface from becoming a
        manual log-spam vector.

        The event is exempt from the
        [`05-event-protocol.md`](../../../../spec/v0/05-event-protocol.md)
        §2 transactional invariant: no protocol-owned state mutation
        pairs with it. The route delegates to
        ``Store.emit_policy_error`` for the actual single-event append;
        204 on success.
        """
        check_experiment(deps, experiment_id, x_eden_experiment_id)
        enforce_in_any_group(deps, request, ("orchestrators",))
        deps.store.emit_policy_error(
            policy_kind=body.policy_kind,
            error_type=body.error_type,
            error_message=body.error_message,
        )
        return Response(status_code=204)

    return emit_policy_error


def _read_experiment_state(deps: RouterDeps):
    async def read_experiment_state(
        request: Request,
        experiment_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        """§2.9 companion read endpoint.

        Either-auth — any registered worker MAY read the state. Mirrors
        the `GET /dispatch_mode` posture (both reads support the
        corresponding StoreClient's read-back ladders).
        """
        check_experiment(deps, experiment_id, x_eden_experiment_id)
        if deps.admin_token is not None:
            _ = request.state.principal  # ensure auth was run
        state = deps.store.read_experiment_state()
        return ExperimentStateResponse(state=state).model_dump(
            mode="json", exclude_none=True
        )

    return read_experiment_state
