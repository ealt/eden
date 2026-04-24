"""FastAPI server that exposes a ``Store`` over the EDEN wire protocol.

:func:`make_app` takes a single ``Store`` and returns a fresh
``FastAPI`` instance that routes every ``/v0/experiments/{E}/...``
endpoint specified in ``spec/v0/07-wire-protocol.md`` to the
corresponding ``Store`` method.

Error handling:

- Any ``StorageError`` raised by the store maps to the matching
  ``eden://error/<name>`` problem+json body via
  :func:`eden_wire.errors.envelope_for_error`.
- ``BadRequest`` covers schema-validation failures; FastAPI's
  ``RequestValidationError`` is caught and rewritten.
- ``ExperimentIdMismatch`` guards the header-vs-path invariant (§1.3).

The server does **not** contain business logic: every endpoint is a
thin adapter that validates the request, calls the store, and
serializes the result.
"""

from __future__ import annotations

import asyncio
import hmac
import time
from typing import Any

from eden_contracts import Proposal, TaskAdapter, Trial
from eden_storage import Store
from eden_storage.errors import StorageError
from eden_storage.submissions import (
    EvaluateSubmission,
    ImplementSubmission,
    PlanSubmission,
    Submission,
)
from fastapi import Body, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from .errors import (
    BadRequest,
    ExperimentIdMismatch,
    Unauthorized,
    envelope_for_error,
    envelope_for_reference_error,
)
from .models import (
    ClaimRequest,
    EventsResponse,
    IntegrateRequest,
    ReclaimRequest,
    RejectRequest,
    SubmitRequest,
    ValidateMetricsRequest,
    ValidateTerminalResponse,
)

PROBLEM_JSON = "application/problem+json"


def make_app(
    store: Store,
    *,
    subscribe_timeout: float = 30.0,
    subscribe_poll_interval: float = 0.1,
    shared_token: str | None = None,
) -> FastAPI:
    """Build a FastAPI app that exposes ``store`` over the wire binding.

    The app is stateless beyond the injected ``store``; multiple apps
    for different experiments can coexist in one process, each with
    their own ``Store`` instance.

    ``subscribe_timeout`` is the long-poll window per
    ``07-wire-protocol.md`` §6.2 (default 30s). Tests typically pass
    a short value. ``subscribe_poll_interval`` is how often the
    server re-checks the event log for new entries; finer values
    reduce latency at the cost of CPU.

    ``shared_token``, when non-``None``, installs the reference-only
    bearer-token middleware from ``07-wire-protocol.md`` §12. Requests
    missing or carrying a wrong ``Authorization: Bearer <token>``
    header are rejected with ``eden://reference-error/unauthorized``
    (HTTP 401). ``None`` (default) disables auth.
    """
    app = FastAPI(
        title=f"EDEN task store — {store.experiment_id}",
        version="0",
    )

    if shared_token is not None:
        _install_shared_token_middleware(app, shared_token)

    def _problem(status: int, type_: str, title: str, detail: str, instance: str) -> JSONResponse:
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

    def _check_experiment(path_exp: str, header_exp: str | None, url: str) -> None:
        if header_exp is None:
            raise ExperimentIdMismatch(
                f"missing X-Eden-Experiment-Id header (expected {path_exp!r})"
            )
        if header_exp != path_exp:
            raise ExperimentIdMismatch(
                f"X-Eden-Experiment-Id header {header_exp!r} does not match "
                f"URL segment {path_exp!r}"
            )
        if path_exp != store.experiment_id:
            raise ExperimentIdMismatch(
                f"URL segment {path_exp!r} does not match server's experiment "
                f"{store.experiment_id!r}"
            )

    @app.exception_handler(StorageError)
    async def _storage_error_handler(request: Request, exc: StorageError) -> JSONResponse:
        envelope = envelope_for_error(exc, instance=str(request.url))
        return JSONResponse(
            status_code=envelope.status,
            media_type=PROBLEM_JSON,
            content=envelope.to_dict(),
        )

    @app.exception_handler(BadRequest)
    async def _bad_request_handler(request: Request, exc: BadRequest) -> JSONResponse:
        envelope = envelope_for_error(exc, instance=str(request.url))
        return JSONResponse(
            status_code=envelope.status,
            media_type=PROBLEM_JSON,
            content=envelope.to_dict(),
        )

    @app.exception_handler(ExperimentIdMismatch)
    async def _exp_mismatch_handler(
        request: Request, exc: ExperimentIdMismatch
    ) -> JSONResponse:
        envelope = envelope_for_error(exc, instance=str(request.url))
        return JSONResponse(
            status_code=envelope.status,
            media_type=PROBLEM_JSON,
            content=envelope.to_dict(),
        )

    @app.exception_handler(RequestValidationError)
    async def _request_validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _problem(
            400,
            "eden://error/bad-request",
            "Bad Request",
            "; ".join(str(e) for e in exc.errors()),
            str(request.url),
        )

    @app.exception_handler(ValidationError)
    async def _pydantic_validation_handler(
        request: Request, exc: ValidationError
    ) -> JSONResponse:
        return _problem(
            400,
            "eden://error/bad-request",
            "Bad Request",
            exc.errors()[0].get("msg", "validation error") if exc.errors() else "validation error",
            str(request.url),
        )

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    base = "/v0/experiments/{experiment_id}"

    @app.post(f"{base}/tasks")
    async def _create_task(
        experiment_id: str,
        body: dict[str, Any] = Body(...),
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/tasks",
        )
        try:
            task = TaskAdapter.validate_python(body)
        except ValidationError as exc:
            raise BadRequest(str(exc)) from exc
        created = store.create_task(task)
        return created.model_dump(mode="json", exclude_none=True)

    @app.get(f"{base}/tasks")
    async def _list_tasks(
        experiment_id: str,
        kind: str | None = Query(None),
        state: str | None = Query(None),
        x_eden_experiment_id: str | None = Header(None),
    ) -> list[dict[str, Any]]:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/tasks",
        )
        tasks = store.list_tasks(kind=kind, state=state)
        return [t.model_dump(mode="json", exclude_none=True) for t in tasks]

    @app.get(f"{base}/tasks/{{task_id}}")
    async def _read_task(
        experiment_id: str,
        task_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_experiment(
            experiment_id, x_eden_experiment_id, f"/v0/experiments/{experiment_id}/tasks/{task_id}"
        )
        task = store.read_task(task_id)
        return task.model_dump(mode="json", exclude_none=True)

    @app.get(f"{base}/tasks/{{task_id}}/submission")
    async def _read_submission(
        experiment_id: str,
        task_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> Response:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/tasks/{task_id}/submission",
        )
        submission = store.read_submission(task_id)
        if submission is None:
            return Response(status_code=204)
        return JSONResponse(content=_submission_to_wire(submission))

    @app.post(f"{base}/tasks/{{task_id}}/claim")
    async def _claim(
        experiment_id: str,
        task_id: str,
        body: ClaimRequest,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/tasks/{task_id}/claim",
        )
        claim = store.claim(task_id, body.worker_id, expires_at=body.expires_at)
        resp: dict[str, Any] = {
            "token": claim.token,
            "worker_id": claim.worker_id,
            "claimed_at": claim.claimed_at,
        }
        if claim.expires_at is not None:
            resp["expires_at"] = claim.expires_at
        return resp

    @app.post(f"{base}/tasks/{{task_id}}/submit")
    async def _submit(
        experiment_id: str,
        task_id: str,
        body: SubmitRequest,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/tasks/{task_id}/submit",
        )
        task = store.read_task(task_id)
        submission = _submission_from_wire(task.kind, body.payload)
        store.submit(task_id, body.token, submission)
        return {}

    @app.post(f"{base}/tasks/{{task_id}}/accept")
    async def _accept(
        experiment_id: str,
        task_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> Response:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/tasks/{task_id}/accept",
        )
        store.accept(task_id)
        return Response(status_code=204)

    @app.post(f"{base}/tasks/{{task_id}}/reject")
    async def _reject(
        experiment_id: str,
        task_id: str,
        body: RejectRequest,
        x_eden_experiment_id: str | None = Header(None),
    ) -> Response:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/tasks/{task_id}/reject",
        )
        store.reject(task_id, body.reason)  # type: ignore[arg-type]
        return Response(status_code=204)

    @app.post(f"{base}/tasks/{{task_id}}/reclaim")
    async def _reclaim(
        experiment_id: str,
        task_id: str,
        body: ReclaimRequest,
        x_eden_experiment_id: str | None = Header(None),
    ) -> Response:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/tasks/{task_id}/reclaim",
        )
        store.reclaim(task_id, body.cause)  # type: ignore[arg-type]
        return Response(status_code=204)

    # ------------------------------------------------------------------
    # Proposals
    # ------------------------------------------------------------------

    @app.post(f"{base}/proposals")
    async def _create_proposal(
        experiment_id: str,
        body: dict[str, Any] = Body(...),
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_experiment(
            experiment_id, x_eden_experiment_id, f"/v0/experiments/{experiment_id}/proposals"
        )
        try:
            proposal = Proposal.model_validate(body)
        except ValidationError as exc:
            raise BadRequest(str(exc)) from exc
        store.create_proposal(proposal)
        # §3: response body matches proposal.schema.json; return the
        # stored proposal so the caller sees what landed.
        return store.read_proposal(proposal.proposal_id).model_dump(
            mode="json", exclude_none=True
        )

    @app.get(f"{base}/proposals")
    async def _list_proposals(
        experiment_id: str,
        state: str | None = Query(None),
        x_eden_experiment_id: str | None = Header(None),
    ) -> list[dict[str, Any]]:
        _check_experiment(
            experiment_id, x_eden_experiment_id, f"/v0/experiments/{experiment_id}/proposals"
        )
        proposals = store.list_proposals(state=state)
        return [p.model_dump(mode="json", exclude_none=True) for p in proposals]

    @app.get(f"{base}/proposals/{{proposal_id}}")
    async def _read_proposal(
        experiment_id: str,
        proposal_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/proposals/{proposal_id}",
        )
        return store.read_proposal(proposal_id).model_dump(mode="json", exclude_none=True)

    @app.post(f"{base}/proposals/{{proposal_id}}/mark-ready")
    async def _mark_proposal_ready(
        experiment_id: str,
        proposal_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> Response:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/proposals/{proposal_id}/mark-ready",
        )
        store.mark_proposal_ready(proposal_id)
        return Response(status_code=204)

    # ------------------------------------------------------------------
    # Trials
    # ------------------------------------------------------------------

    @app.post(f"{base}/trials")
    async def _create_trial(
        experiment_id: str,
        body: dict[str, Any] = Body(...),
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_experiment(
            experiment_id, x_eden_experiment_id, f"/v0/experiments/{experiment_id}/trials"
        )
        try:
            trial = Trial.model_validate(body)
        except ValidationError as exc:
            raise BadRequest(str(exc)) from exc
        store.create_trial(trial)
        # §4: response body matches trial.schema.json.
        return store.read_trial(trial.trial_id).model_dump(
            mode="json", exclude_none=True
        )

    @app.get(f"{base}/trials")
    async def _list_trials(
        experiment_id: str,
        status: str | None = Query(None),
        x_eden_experiment_id: str | None = Header(None),
    ) -> list[dict[str, Any]]:
        _check_experiment(
            experiment_id, x_eden_experiment_id, f"/v0/experiments/{experiment_id}/trials"
        )
        return [
            t.model_dump(mode="json", exclude_none=True)
            for t in store.list_trials(status=status)
        ]

    @app.get(f"{base}/trials/{{trial_id}}")
    async def _read_trial(
        experiment_id: str,
        trial_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/trials/{trial_id}",
        )
        return store.read_trial(trial_id).model_dump(mode="json", exclude_none=True)

    @app.post(f"{base}/trials/{{trial_id}}/declare-eval-error")
    async def _declare_trial_eval_error(
        experiment_id: str,
        trial_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> Response:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/trials/{trial_id}/declare-eval-error",
        )
        store.declare_trial_eval_error(trial_id)
        return Response(status_code=204)

    @app.post(f"{base}/trials/{{trial_id}}/integrate")
    async def _integrate_trial(
        experiment_id: str,
        trial_id: str,
        body: IntegrateRequest,
        x_eden_experiment_id: str | None = Header(None),
    ) -> Response:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/trials/{trial_id}/integrate",
        )
        # §5: 200 + empty body on success and same-value idempotent
        # retries; 409 invalid-precondition on different-SHA divergence
        # (raised by Store.integrate_trial).
        store.integrate_trial(trial_id, body.trial_commit_sha)
        return Response(status_code=200)

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    @app.get(f"{base}/events")
    async def _read_range(
        experiment_id: str,
        cursor: int = Query(0, ge=0),
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_experiment(
            experiment_id, x_eden_experiment_id, f"/v0/experiments/{experiment_id}/events"
        )
        events = store.read_range(cursor=cursor if cursor > 0 else None)
        resp = EventsResponse(events=events, cursor=cursor + len(events))
        return resp.model_dump(mode="json", exclude_none=True)

    @app.get(f"{base}/events/subscribe")
    async def _subscribe(
        experiment_id: str,
        cursor: int = Query(0, ge=0),
        timeout: float | None = Query(None, ge=0),
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        # §6.2 long-poll: hold the connection open until at least one
        # event is available after `cursor` or ``timeout`` (default
        # ``subscribe_timeout``) elapses. The underlying ``Store`` is
        # a synchronous in-process object, so we poll ``read_range``
        # in a loop with a short interval. An asyncio.sleep yields to
        # the event loop, so other requests (e.g. the write that
        # unblocks us) progress concurrently.
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/events/subscribe",
        )
        effective_timeout = timeout if timeout is not None else subscribe_timeout
        deadline = time.monotonic() + effective_timeout
        events = store.read_range(cursor=cursor if cursor > 0 else None)
        while not events and time.monotonic() < deadline:
            await asyncio.sleep(subscribe_poll_interval)
            events = store.read_range(cursor=cursor if cursor > 0 else None)
        resp = EventsResponse(events=events, cursor=cursor + len(events))
        return resp.model_dump(mode="json", exclude_none=True)

    # ------------------------------------------------------------------
    # Reference-only helpers (non-normative)
    # ------------------------------------------------------------------

    ref_base = "/_reference/experiments/{experiment_id}"

    @app.get(f"{ref_base}/tasks/{{task_id}}/validate-terminal")
    async def _validate_terminal(
        experiment_id: str,
        task_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/_reference/experiments/{experiment_id}/tasks/{task_id}/validate-terminal",
        )
        decision, reason = store.validate_terminal(task_id)
        return ValidateTerminalResponse(
            decision=decision, reason=reason
        ).model_dump(mode="json", exclude_none=True)

    @app.post(f"{ref_base}/validate/metrics")
    async def _validate_metrics(
        experiment_id: str,
        body: ValidateMetricsRequest,
        x_eden_experiment_id: str | None = Header(None),
    ) -> Response:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/_reference/experiments/{experiment_id}/validate/metrics",
        )
        store.validate_metrics(body.metrics)
        return Response(status_code=204)

    return app


def _submission_to_wire(submission: Submission) -> dict[str, Any]:
    if isinstance(submission, PlanSubmission):
        return {
            "kind": "plan",
            "status": submission.status,
            "proposal_ids": list(submission.proposal_ids),
        }
    if isinstance(submission, ImplementSubmission):
        body: dict[str, Any] = {
            "kind": "implement",
            "status": submission.status,
            "trial_id": submission.trial_id,
        }
        if submission.commit_sha is not None:
            body["commit_sha"] = submission.commit_sha
        return body
    if isinstance(submission, EvaluateSubmission):
        body = {
            "kind": "evaluate",
            "status": submission.status,
            "trial_id": submission.trial_id,
        }
        if submission.metrics is not None:
            body["metrics"] = submission.metrics
        if submission.artifacts_uri is not None:
            body["artifacts_uri"] = submission.artifacts_uri
        return body
    raise ValueError(f"unknown submission type: {type(submission).__name__}")


def _submission_from_wire(kind: str, payload: dict[str, Any]) -> Submission:
    if kind == "plan":
        return PlanSubmission(
            status=payload["status"],
            proposal_ids=tuple(payload.get("proposal_ids", ())),
        )
    if kind == "implement":
        return ImplementSubmission(
            status=payload["status"],
            trial_id=payload["trial_id"],
            commit_sha=payload.get("commit_sha"),
        )
    if kind == "evaluate":
        return EvaluateSubmission(
            status=payload["status"],
            trial_id=payload["trial_id"],
            metrics=payload.get("metrics"),
            artifacts_uri=payload.get("artifacts_uri"),
        )
    raise HTTPException(status_code=400, detail=f"unknown task kind {kind!r}")


def _install_shared_token_middleware(app: FastAPI, expected: str) -> None:
    """Install the reference-only shared-token auth middleware.

    Checks ``Authorization: Bearer <token>`` on every request against
    ``expected`` with a constant-time compare. Mismatches emit a
    problem+json body under the reference-only
    ``eden://reference-error/unauthorized`` type (HTTP 401).
    """
    expected_bytes = expected.encode("utf-8")

    @app.middleware("http")
    async def _shared_token_middleware(request: Request, call_next: Any) -> Any:
        header = request.headers.get("authorization")
        presented: str | None = None
        if header is not None:
            parts = header.split(" ", 1)
            if len(parts) == 2 and parts[0].lower() == "bearer":
                presented = parts[1]
        if presented is None or not hmac.compare_digest(
            presented.encode("utf-8"), expected_bytes
        ):
            exc = Unauthorized("missing or invalid Authorization header")
            envelope = envelope_for_reference_error(exc, instance=str(request.url))
            return JSONResponse(
                status_code=envelope.status,
                media_type=PROBLEM_JSON,
                content=envelope.to_dict(),
            )
        return await call_next(request)
