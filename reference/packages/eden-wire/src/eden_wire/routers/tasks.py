"""Task-lifecycle routes (chapter 7 §2).

The 10 task endpoints: create / list / read / read-submission, plus the
claim / submit / accept / reject / reclaim / reassign transitions.
``build_router`` is a thin assembler; each route's handler lives in a
module-level closure factory (``_<route>(deps)``) so every handler is an
independently-measured function for the complexity gate.
"""

from __future__ import annotations

from typing import Any

from eden_contracts import TaskAdapter
from eden_storage.submissions import (
    Submission,
    submission_from_payload,
    submission_to_payload,
)
from fastapi import APIRouter, Body, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from .._dependencies import (
    RouterDeps,
    check_experiment,
    enforce_in_any_group,
    enforce_worker,
    stamp_created_by,
    worker_id_from_request,
)
from ..errors import BadRequest
from ..models import ClaimRequest, ReassignRequest, ReclaimRequest, RejectRequest, SubmitRequest


def build_router(deps: RouterDeps) -> APIRouter:
    """Return the tasks ``APIRouter`` bound to ``deps``."""
    router = APIRouter(prefix="/v0/experiments/{experiment_id}/tasks")
    router.post("")(_create_task(deps))
    router.get("")(_list_tasks(deps))
    router.get("/{task_id}")(_read_task(deps))
    router.get("/{task_id}/submission")(_read_submission(deps))
    router.post("/{task_id}/claim")(_claim(deps))
    router.post("/{task_id}/submit")(_submit(deps))
    router.post("/{task_id}/accept")(_accept(deps))
    router.post("/{task_id}/reject")(_reject(deps))
    router.post("/{task_id}/reclaim")(_reclaim(deps))
    router.post("/{task_id}/reassign")(_reassign_task(deps))
    return router


def _create_task(deps: RouterDeps):
    async def create_task(
        request: Request,
        experiment_id: str,
        body: dict[str, Any] = Body(...),
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        check_experiment(deps, experiment_id, x_eden_experiment_id)
        # §2.1 per-kind authority. Peek at `body["kind"]` BEFORE the
        # full validate so the authority check fires on schema-valid AND
        # schema-invalid bodies alike. If the kind is missing /
        # unrecognized the downstream TaskAdapter.validate_python
        # produces the canonical bad-request envelope; we fall through to
        # that path without claiming authority either way.
        #
        # 12a-3 broadened `kind=execution` from orchestrators-only to
        # admins OR orchestrators: the new ``idea.intended_executor``
        # field gives operators a non-fungible routing seed, so the
        # pre-12a-3 deferral that the operator path needed first no
        # longer applies (`03-roles.md` §6.5, `07-wire-protocol.md`
        # §2.1).
        kind = body.get("kind") if isinstance(body, dict) else None
        if kind in ("ideation", "execution", "evaluation"):
            enforce_in_any_group(deps, request, ("admins", "orchestrators"))
        else:
            # Unrecognized kind — let the schema validator decide. We
            # still require a worker bearer so an admin bearer never
            # reaches the route handler.
            enforce_worker(deps, request)
        body = stamp_created_by(deps, request, body)
        try:
            task = TaskAdapter.validate_python(body)
        except ValidationError as exc:
            raise BadRequest(str(exc)) from exc
        created = deps.store.create_task(task)
        return created.model_dump(mode="json", exclude_none=True)

    return create_task


def _list_tasks(deps: RouterDeps):
    async def list_tasks(
        experiment_id: str,
        kind: str | None = Query(None),
        state: str | None = Query(None),
        x_eden_experiment_id: str | None = Header(None),
    ) -> list[dict[str, Any]]:
        check_experiment(deps, experiment_id, x_eden_experiment_id)
        tasks = deps.store.list_tasks(kind=kind, state=state)
        return [t.model_dump(mode="json", exclude_none=True) for t in tasks]

    return list_tasks


def _read_task(deps: RouterDeps):
    async def read_task(
        experiment_id: str,
        task_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        check_experiment(deps, experiment_id, x_eden_experiment_id)
        task = deps.store.read_task(task_id)
        return task.model_dump(mode="json", exclude_none=True)

    return read_task


def _read_submission(deps: RouterDeps):
    async def read_submission(
        experiment_id: str,
        task_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> Response:
        check_experiment(deps, experiment_id, x_eden_experiment_id)
        submission = deps.store.read_submission(task_id)
        if submission is None:
            return Response(status_code=204)
        return JSONResponse(content=_submission_to_wire(submission))

    return read_submission


def _claim(deps: RouterDeps):
    async def claim(
        request: Request,
        experiment_id: str,
        task_id: str,
        body: ClaimRequest,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        check_experiment(deps, experiment_id, x_eden_experiment_id)
        enforce_worker(deps, request)
        # §2.3 + §13: claimant worker_id comes from the authenticated
        # bearer, not the request body. worker_id_from_request reads
        # request.state.principal when auth is enabled and falls back to
        # a sentinel only when auth was not installed (test-only).
        worker_id = worker_id_from_request(request)
        claim = deps.store.claim(task_id, worker_id, expires_at=body.expires_at)
        resp: dict[str, Any] = {
            "worker_id": claim.worker_id,
            "claimed_at": claim.claimed_at,
        }
        if claim.expires_at is not None:
            resp["expires_at"] = claim.expires_at
        return resp

    return claim


def _submit(deps: RouterDeps):
    async def submit(
        request: Request,
        experiment_id: str,
        task_id: str,
        body: SubmitRequest,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        check_experiment(deps, experiment_id, x_eden_experiment_id)
        enforce_worker(deps, request)
        # §2.4 + §13: forward the authenticated worker_id to
        # Store.submit; the Store performs the §4.1 atomic claim-match
        # (WrongClaimant / NotClaimed). No pre-flight `read_task →
        # compare` here — that would introduce a TOCTOU window against
        # reclaim.
        worker_id = worker_id_from_request(request)
        task = deps.store.read_task(task_id)
        submission = _submission_from_wire(task.kind, body.payload)
        deps.store.submit(task_id, worker_id, submission)
        return {}

    return submit


def _accept(deps: RouterDeps):
    async def accept(
        request: Request,
        experiment_id: str,
        task_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> Response:
        check_experiment(deps, experiment_id, x_eden_experiment_id)
        # §2.5: accept is the orchestrator role's responsibility.
        enforce_in_any_group(deps, request, ("orchestrators",))
        deps.store.accept(task_id)
        return Response(status_code=204)

    return accept


def _reject(deps: RouterDeps):
    async def reject(
        request: Request,
        experiment_id: str,
        task_id: str,
        body: RejectRequest,
        x_eden_experiment_id: str | None = Header(None),
    ) -> Response:
        check_experiment(deps, experiment_id, x_eden_experiment_id)
        # §2.5: reject is the orchestrator role's responsibility.
        enforce_in_any_group(deps, request, ("orchestrators",))
        deps.store.reject(task_id, body.reason)  # type: ignore[arg-type]
        return Response(status_code=204)

    return reject


def _reclaim(deps: RouterDeps):
    async def reclaim(
        request: Request,
        experiment_id: str,
        task_id: str,
        body: ReclaimRequest,
        x_eden_experiment_id: str | None = Header(None),
    ) -> Response:
        check_experiment(deps, experiment_id, x_eden_experiment_id)
        enforce_worker(deps, request)
        deps.store.reclaim(task_id, body.cause)  # type: ignore[arg-type]
        return Response(status_code=204)

    return reclaim


def _reassign_task(deps: RouterDeps):
    async def reassign_task(
        request: Request,
        experiment_id: str,
        task_id: str,
        body: ReassignRequest,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        """§2.7: admin-group-gated reassignment of `task.target`.

        Stamps `reassigned_by` from the authenticated principal; the
        request body MUST NOT carry the field (the `ReassignRequest`
        model forbids it via `extra="forbid"`).
        """
        check_experiment(deps, experiment_id, x_eden_experiment_id)
        reassigned_by = enforce_in_any_group(deps, request, ("admins",))
        updated = deps.store.reassign_task(
            task_id,
            body.new_target,
            reason=body.reason,
            reassigned_by=reassigned_by,
        )
        return updated.model_dump(mode="json", exclude_none=True)

    return reassign_task


def _submission_to_wire(submission: Submission) -> dict[str, Any]:
    kind, payload = submission_to_payload(submission)
    return {"kind": kind, **payload}


def _submission_from_wire(kind: str, payload: dict[str, Any]) -> Submission:
    try:
        return submission_from_payload(kind, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
