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
import time
from typing import Any

from eden_contracts import Idea, TaskAdapter, Variant
from eden_storage import Store
from eden_storage.errors import StorageError
from eden_storage.submissions import (
    EvaluationSubmission,
    IdeaSubmission,
    Submission,
    VariantSubmission,
)
from fastapi import Body, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from .auth import (
    install_auth_middleware,
    require_admin,
    require_worker,
)
from .errors import (
    BadRequest,
    ExperimentIdMismatch,
    Forbidden,
    Unauthorized,
    envelope_for_error,
)
from .models import (
    AddGroupMemberRequest,
    ClaimRequest,
    DispatchModeResponse,
    DispatchModeUpdateRequest,
    EventsResponse,
    IntegrateRequest,
    ReassignRequest,
    ReclaimRequest,
    RegisterGroupRequest,
    RegisterWorkerRequest,
    RejectRequest,
    SubmitRequest,
    ValidateEvaluationRequest,
    ValidateTerminalResponse,
)

PROBLEM_JSON = "application/problem+json"


def make_app(
    store: Store,
    *,
    subscribe_timeout: float = 30.0,
    subscribe_poll_interval: float = 0.1,
    admin_token: str | None = None,
) -> FastAPI:
    """Build a FastAPI app that exposes ``store`` over the wire binding.

    The app is stateless beyond the injected ``store``; multiple apps
    for different experiments can coexist in one process, each with
    their own ``Store`` instance.

    ``subscribe_timeout`` is the long-poll window per
    ``07-wire-protocol.md`` §8.2 (default 30s). Tests typically pass
    a short value. ``subscribe_poll_interval`` is how often the
    server re-checks the event log for new entries; finer values
    reduce latency at the cost of CPU.

    ``admin_token``, when non-``None``, installs the §13 normative
    authentication middleware: every ``/v0/`` request MUST carry a
    valid ``Authorization: Bearer <principal>:<secret>`` header
    where the principal is either ``admin`` (matched against
    ``admin_token`` constant-time) or a registered ``worker_id``
    (verified against the Store's ``verify_worker_credential``).
    ``None`` (test / in-process default) disables auth — convenient
    for unit tests but NOT spec-conformant for a deployed server.
    """
    app = FastAPI(
        title=f"EDEN task store — {store.experiment_id}",
        version="0",
    )

    if admin_token is not None:
        install_auth_middleware(app, admin_token=admin_token, store=store)

    def _enforce_worker(request: Request) -> None:
        """Worker-gated route guard (§13.3).

        When auth is disabled (``admin_token is None``), the middleware
        hasn't installed a principal and no enforcement runs — that's
        the in-process / TestClient posture. When auth is enabled, any
        admin bearer hitting a worker-gated route MUST 403 per the
        chapter-7 §13.3 dispatcher contract.
        """
        if admin_token is None:
            return
        require_worker(request)

    def _enforce_in_any_group(
        request: Request, group_ids: tuple[str, ...]
    ) -> str:
        """Worker-gated route guard plus group-membership check (§3.7).

        Requires the request to carry a worker bearer (admin bearers
        are rejected — these endpoints exist for operator workflows
        that the deployment surfaces through registered workers in
        ``admins`` / ``orchestrators``; the literal ``admin``
        principal is a bootstrap-only identity for registry mgmt per
        12a-1 §D.5). Then checks the worker's transitive membership
        in any of ``group_ids`` via ``Store.resolve_worker_in_group``;
        membership in ANY listed group passes (OR semantics).

        Returns the authenticated worker_id on success so the caller
        can stamp attribution fields (``reassigned_by`` / ``updated_by``).
        Returns the literal ``"anonymous"`` when auth is disabled (test
        posture) — equivalent to the existing ``X-Eden-Worker-Id``
        fallback in ``_worker_id_from_request``.

        Raises :class:`Forbidden` (403 ``eden://error/forbidden``) on
        membership miss.
        """
        if admin_token is None:
            return request.headers.get("X-Eden-Worker-Id", "anonymous")
        principal = require_worker(request)
        assert principal.worker_id is not None
        for gid in group_ids:
            if store.resolve_worker_in_group(principal.worker_id, gid):
                return principal.worker_id
        groups_str = " or ".join(repr(g) for g in group_ids)
        raise Forbidden(
            f"endpoint requires membership in {groups_str}; worker "
            f"{principal.worker_id!r} is not a transitive member"
        )

    def _stamp_created_by(
        request: Request, body: dict[str, Any], field: str = "created_by"
    ) -> dict[str, Any]:
        """Stamp ``created_by`` on a create-* request body from the auth principal.

        Per chapter 02 §3.1 / §5.1, ``created_by`` records the actor
        identifier of the caller that produced the artifact. To prevent
        a client from spoofing the attribution, the binding overrides
        the field with the authenticated principal's identity:

        - Worker bearer → ``created_by = principal.worker_id``.
        - Admin bearer → ``created_by = "admin"`` (the §13.1 admin
          principal name; carried through chapter 02 §3.1 / §5.1).

        If the body supplied a different ``created_by`` value, the
        binding rejects with `BadRequest`. When auth is disabled (test
        / in-process posture, ``admin_token is None``), the body is
        passed through unchanged so existing test fixtures keep
        working.
        """
        if admin_token is None:
            return body
        principal = getattr(request.state, "principal", None)
        if principal is None or not hasattr(principal, "is_worker"):
            return body
        if principal.is_worker():
            assert principal.worker_id is not None
            stamp = principal.worker_id
        else:
            stamp = "admin"
        supplied = body.get(field)
        if supplied is not None and supplied != stamp:
            raise BadRequest(
                f"{field}={supplied!r} disagrees with authenticated "
                f"principal {stamp!r}; the binding overrides this "
                f"field from the bearer's identity per chapter 02 §3.1"
            )
        return {**body, field: stamp}

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

    @app.exception_handler(Unauthorized)
    async def _unauthorized_handler(
        request: Request, exc: Unauthorized
    ) -> JSONResponse:
        envelope = envelope_for_error(exc, instance=str(request.url))
        return JSONResponse(
            status_code=envelope.status,
            media_type=PROBLEM_JSON,
            content=envelope.to_dict(),
        )

    @app.exception_handler(Forbidden)
    async def _forbidden_handler(
        request: Request, exc: Forbidden
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
        request: Request,
        experiment_id: str,
        body: dict[str, Any] = Body(...),
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/tasks",
        )
        # §2.1 per-kind authority. Peek at `body["kind"]` BEFORE the
        # full validate so the authority check fires on schema-valid
        # AND schema-invalid bodies alike. If the kind is missing /
        # unrecognized the downstream TaskAdapter.validate_python
        # produces the canonical bad-request envelope; we fall through
        # to that path without claiming authority either way.
        kind = body.get("kind") if isinstance(body, dict) else None
        if kind == "execution":
            _enforce_in_any_group(request, ("orchestrators",))
        elif kind in ("ideation", "evaluation"):
            _enforce_in_any_group(request, ("admins", "orchestrators"))
        else:
            # Unrecognized kind — let the schema validator decide. We
            # still require a worker bearer so an admin bearer never
            # reaches the route handler.
            _enforce_worker(request)
        body = _stamp_created_by(request, body)
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
        request: Request,
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
        _enforce_worker(request)
        # §2.3 + §13: claimant worker_id comes from the authenticated
        # bearer, not the request body. _worker_id_from_request reads
        # request.state.principal when auth is enabled and falls back
        # to a sentinel only when auth was not installed (test-only).
        worker_id = _worker_id_from_request(request)
        claim = store.claim(task_id, worker_id, expires_at=body.expires_at)
        resp: dict[str, Any] = {
            "worker_id": claim.worker_id,
            "claimed_at": claim.claimed_at,
        }
        if claim.expires_at is not None:
            resp["expires_at"] = claim.expires_at
        return resp

    @app.post(f"{base}/tasks/{{task_id}}/submit")
    async def _submit(
        request: Request,
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
        _enforce_worker(request)
        # §2.4 + §13: forward the authenticated worker_id to
        # Store.submit; the Store performs the §4.1 atomic claim-match
        # (WrongClaimant / NotClaimed). No pre-flight `read_task →
        # compare` here — that would introduce a TOCTOU window
        # against reclaim.
        worker_id = _worker_id_from_request(request)
        task = store.read_task(task_id)
        submission = _submission_from_wire(task.kind, body.payload)
        store.submit(task_id, worker_id, submission)
        return {}

    @app.post(f"{base}/tasks/{{task_id}}/accept")
    async def _accept(
        request: Request,
        experiment_id: str,
        task_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> Response:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/tasks/{task_id}/accept",
        )
        # §2.5: accept is the orchestrator role's responsibility.
        _enforce_in_any_group(request, ("orchestrators",))
        store.accept(task_id)
        return Response(status_code=204)

    @app.post(f"{base}/tasks/{{task_id}}/reject")
    async def _reject(
        request: Request,
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
        # §2.5: reject is the orchestrator role's responsibility.
        _enforce_in_any_group(request, ("orchestrators",))
        store.reject(task_id, body.reason)  # type: ignore[arg-type]
        return Response(status_code=204)

    @app.post(f"{base}/tasks/{{task_id}}/reclaim")
    async def _reclaim(
        request: Request,
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
        _enforce_worker(request)
        store.reclaim(task_id, body.cause)  # type: ignore[arg-type]
        return Response(status_code=204)

    @app.post(f"{base}/tasks/{{task_id}}/reassign")
    async def _reassign_task(
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
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/tasks/{task_id}/reassign",
        )
        reassigned_by = _enforce_in_any_group(request, ("admins",))
        updated = store.reassign_task(
            task_id,
            body.new_target,
            reason=body.reason,
            reassigned_by=reassigned_by,
        )
        return updated.model_dump(mode="json", exclude_none=True)

    # ------------------------------------------------------------------
    # Ideas
    # ------------------------------------------------------------------

    @app.post(f"{base}/ideas")
    async def _create_idea(
        request: Request,
        experiment_id: str,
        body: dict[str, Any] = Body(...),
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_experiment(
            experiment_id, x_eden_experiment_id, f"/v0/experiments/{experiment_id}/ideas"
        )
        _enforce_worker(request)
        body = _stamp_created_by(request, body)
        try:
            idea = Idea.model_validate(body)
        except ValidationError as exc:
            raise BadRequest(str(exc)) from exc
        store.create_idea(idea)
        # §3: response body matches idea.schema.json; return the
        # stored idea so the caller sees what landed.
        return store.read_idea(idea.idea_id).model_dump(
            mode="json", exclude_none=True
        )

    @app.get(f"{base}/ideas")
    async def _list_ideas(
        experiment_id: str,
        state: str | None = Query(None),
        x_eden_experiment_id: str | None = Header(None),
    ) -> list[dict[str, Any]]:
        _check_experiment(
            experiment_id, x_eden_experiment_id, f"/v0/experiments/{experiment_id}/ideas"
        )
        ideas = store.list_ideas(state=state)
        return [p.model_dump(mode="json", exclude_none=True) for p in ideas]

    @app.get(f"{base}/ideas/{{idea_id}}")
    async def _read_idea(
        experiment_id: str,
        idea_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/ideas/{idea_id}",
        )
        return store.read_idea(idea_id).model_dump(mode="json", exclude_none=True)

    @app.post(f"{base}/ideas/{{idea_id}}/mark-ready")
    async def _mark_idea_ready(
        request: Request,
        experiment_id: str,
        idea_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> Response:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/ideas/{idea_id}/mark-ready",
        )
        _enforce_worker(request)
        store.mark_idea_ready(idea_id)
        return Response(status_code=204)

    # ------------------------------------------------------------------
    # Variants
    # ------------------------------------------------------------------

    @app.post(f"{base}/variants")
    async def _create_variant(
        request: Request,
        experiment_id: str,
        body: dict[str, Any] = Body(...),
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_experiment(
            experiment_id, x_eden_experiment_id, f"/v0/experiments/{experiment_id}/variants"
        )
        _enforce_worker(request)
        try:
            variant = Variant.model_validate(body)
        except ValidationError as exc:
            raise BadRequest(str(exc)) from exc
        store.create_variant(variant)
        # §4: response body matches variant.schema.json.
        return store.read_variant(variant.variant_id).model_dump(
            mode="json", exclude_none=True
        )

    @app.get(f"{base}/variants")
    async def _list_variants(
        experiment_id: str,
        status: str | None = Query(None),
        x_eden_experiment_id: str | None = Header(None),
    ) -> list[dict[str, Any]]:
        _check_experiment(
            experiment_id, x_eden_experiment_id, f"/v0/experiments/{experiment_id}/variants"
        )
        return [
            t.model_dump(mode="json", exclude_none=True)
            for t in store.list_variants(status=status)
        ]

    @app.get(f"{base}/variants/{{variant_id}}")
    async def _read_variant(
        experiment_id: str,
        variant_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/variants/{variant_id}",
        )
        return store.read_variant(variant_id).model_dump(mode="json", exclude_none=True)

    @app.post(f"{base}/variants/{{variant_id}}/declare-evaluation-error")
    async def _declare_variant_eval_error(
        request: Request,
        experiment_id: str,
        variant_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> Response:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/variants/{variant_id}/declare-evaluation-error",
        )
        _enforce_worker(request)
        store.declare_variant_evaluation_error(variant_id)
        return Response(status_code=204)

    @app.post(f"{base}/variants/{{variant_id}}/integrate")
    async def _integrate_variant(
        request: Request,
        experiment_id: str,
        variant_id: str,
        body: IntegrateRequest,
        x_eden_experiment_id: str | None = Header(None),
    ) -> Response:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/variants/{variant_id}/integrate",
        )
        # §4 / §5: integration is the orchestrator role's job; the
        # 12a-2 authority table pins the caller to `orchestrators`.
        _enforce_in_any_group(request, ("orchestrators",))
        # §5: 200 + empty body on success and same-value idempotent
        # retries; 409 invalid-precondition on different-SHA divergence
        # (raised by Store.integrate_variant).
        store.integrate_variant(variant_id, body.variant_commit_sha)
        return Response(status_code=200)

    # ------------------------------------------------------------------
    # Dispatch mode (12a-2 §2.8)
    # ------------------------------------------------------------------

    @app.get(f"{base}/dispatch_mode")
    async def _read_dispatch_mode(
        request: Request,
        experiment_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        """§2.8 companion read endpoint (MAY-level per spec).

        Wave-3 exposes the read because the StoreClient's read-back
        ladder for PATCH transport-indeterminate failures needs it.
        Either-auth (admin OR worker) — same posture as
        ``GET /events`` and the other read endpoints.
        """
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/dispatch_mode",
        )
        if admin_token is not None:
            _ = request.state.principal  # ensure auth was run
        mode = store.read_dispatch_mode()
        return mode.model_dump(mode="json", exclude_none=True)

    @app.patch(f"{base}/dispatch_mode")
    async def _update_dispatch_mode(
        request: Request,
        experiment_id: str,
        body: DispatchModeUpdateRequest,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        """§2.8 admin-group-gated partial-merge update.

        Stamps `updated_by` from the authenticated principal; the
        request body MUST NOT carry the field (the model's
        ``extra="allow"`` lets unknown dispatch_mode keys round-trip
        per §2.5, but the server itself sources `updated_by` from
        auth).
        """
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/dispatch_mode",
        )
        updated_by = _enforce_in_any_group(request, ("admins",))
        # Dump excludes None so the wave-2 `update_dispatch_mode`
        # partial-merge semantics see only the keys the caller actually
        # supplied. Unknown keys passed via `extra="allow"` are kept.
        updates = body.model_dump(mode="json", exclude_none=True)
        # Value-grammar validation lives at the wire layer so a bad
        # value (including on an unknown extra="allow" key) becomes a
        # 400 BadRequest per chapter 04 §7.1 / chapter 07 §2.8, not a
        # 409 invalid-precondition (the store-side check exists as
        # defense-in-depth but is reachable only via direct Store
        # callers). The closed value-set is `auto` / `manual`.
        for key, value in updates.items():
            if value not in ("auto", "manual"):
                raise BadRequest(
                    f"dispatch_mode.{key} value {value!r} is not 'auto' or 'manual'"
                )
        result = store.update_dispatch_mode(updates, updated_by=updated_by)
        return DispatchModeResponse.model_validate(
            result.model_dump(mode="json", exclude_none=True)
        ).model_dump(mode="json", exclude_none=True)

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
    # Worker registry (chapter 7 §6)
    # ------------------------------------------------------------------

    @app.post(f"{base}/workers")
    async def _register_worker(
        request: Request,
        experiment_id: str,
        body: RegisterWorkerRequest,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/workers",
        )
        # Admin-gated. require_admin raises Forbidden (with the
        # "endpoint requires authentication" path) when auth is off,
        # so test harnesses can still drive the route by passing the
        # admin bearer.
        principal = require_admin(request) if admin_token is not None else None
        worker, registration_token = store.register_worker(
            body.worker_id,
            labels=body.labels,
            registered_by=principal.kind if principal is not None else None,
        )
        resp = worker.model_dump(mode="json", exclude_none=True)
        if registration_token is not None:
            resp["registration_token"] = registration_token
        return resp

    @app.get(f"{base}/workers")
    async def _list_workers(
        request: Request,
        experiment_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/workers",
        )
        # Either-gated (admin OR worker). Auth was already verified by
        # the middleware; we don't classify further.
        if admin_token is not None:
            _ = request.state.principal  # auth was run; principal is set
        workers = store.list_workers()
        return {
            "workers": [w.model_dump(mode="json", exclude_none=True) for w in workers]
        }

    @app.get(f"{base}/workers/{{worker_id}}")
    async def _read_worker(
        experiment_id: str,
        worker_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/workers/{worker_id}",
        )
        worker = store.read_worker(worker_id)
        return worker.model_dump(mode="json", exclude_none=True)

    @app.post(f"{base}/workers/{{worker_id}}/reissue-credential")
    async def _reissue_credential(
        request: Request,
        experiment_id: str,
        worker_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/workers/{worker_id}/reissue-credential",
        )
        if admin_token is not None:
            require_admin(request)
        token = store.reissue_credential(worker_id)
        worker = store.read_worker(worker_id)
        resp = worker.model_dump(mode="json", exclude_none=True)
        resp["registration_token"] = token
        return resp

    @app.get(f"{base}/whoami")
    async def _whoami(
        request: Request,
        experiment_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, str]:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/whoami",
        )
        # Worker-gated per §6.4: the endpoint exists to confirm the
        # caller's worker_id; an admin bearer cannot speak as a
        # worker, so this MUST 403 for admins.
        if admin_token is not None:
            principal = require_worker(request)
            assert principal.worker_id is not None
            return {"worker_id": principal.worker_id}
        # Auth disabled: return a sentinel so tests still get a 200.
        return {"worker_id": "anonymous"}

    # ------------------------------------------------------------------
    # Group registry (chapter 7 §7)
    # ------------------------------------------------------------------

    @app.post(f"{base}/groups")
    async def _register_group(
        request: Request,
        experiment_id: str,
        body: RegisterGroupRequest,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/groups",
        )
        principal = require_admin(request) if admin_token is not None else None
        group = store.register_group(
            body.group_id,
            members=body.members,
            created_by=principal.kind if principal is not None else None,
        )
        return group.model_dump(mode="json", exclude_none=True)

    @app.get(f"{base}/groups")
    async def _list_groups(
        experiment_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/groups",
        )
        groups = store.list_groups()
        return {
            "groups": [g.model_dump(mode="json", exclude_none=True) for g in groups]
        }

    @app.get(f"{base}/groups/{{group_id}}")
    async def _read_group(
        experiment_id: str,
        group_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/groups/{group_id}",
        )
        group = store.read_group(group_id)
        return group.model_dump(mode="json", exclude_none=True)

    @app.post(f"{base}/groups/{{group_id}}/members")
    async def _add_to_group(
        request: Request,
        experiment_id: str,
        group_id: str,
        body: AddGroupMemberRequest,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/groups/{group_id}/members",
        )
        if admin_token is not None:
            require_admin(request)
        group = store.add_to_group(group_id, body.member_id)
        return group.model_dump(mode="json", exclude_none=True)

    @app.delete(f"{base}/groups/{{group_id}}/members/{{member_id}}")
    async def _remove_from_group(
        request: Request,
        experiment_id: str,
        group_id: str,
        member_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/groups/{group_id}/members/{member_id}",
        )
        if admin_token is not None:
            require_admin(request)
        group = store.remove_from_group(group_id, member_id)
        return group.model_dump(mode="json", exclude_none=True)

    @app.delete(f"{base}/groups/{{group_id}}")
    async def _delete_group(
        request: Request,
        experiment_id: str,
        group_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> Response:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/groups/{group_id}",
        )
        if admin_token is not None:
            require_admin(request)
        store.delete_group(group_id)
        return Response(status_code=204)

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

    @app.post(f"{ref_base}/validate/evaluation")
    async def _validate_evaluation(
        experiment_id: str,
        body: ValidateEvaluationRequest,
        x_eden_experiment_id: str | None = Header(None),
    ) -> Response:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/_reference/experiments/{experiment_id}/validate/evaluation",
        )
        store.validate_evaluation(body.evaluation)
        return Response(status_code=204)

    return app


def _submission_to_wire(submission: Submission) -> dict[str, Any]:
    if isinstance(submission, IdeaSubmission):
        return {
            "kind": "ideation",
            "status": submission.status,
            "idea_ids": list(submission.idea_ids),
        }
    if isinstance(submission, VariantSubmission):
        body: dict[str, Any] = {
            "kind": "execution",
            "status": submission.status,
            "variant_id": submission.variant_id,
        }
        if submission.commit_sha is not None:
            body["commit_sha"] = submission.commit_sha
        return body
    if isinstance(submission, EvaluationSubmission):
        body = {
            "kind": "evaluation",
            "status": submission.status,
            "variant_id": submission.variant_id,
        }
        if submission.evaluation is not None:
            body["evaluation"] = submission.evaluation
        if submission.artifacts_uri is not None:
            body["artifacts_uri"] = submission.artifacts_uri
        return body
    raise ValueError(f"unknown submission type: {type(submission).__name__}")


def _submission_from_wire(kind: str, payload: dict[str, Any]) -> Submission:
    if kind == "ideation":
        return IdeaSubmission(
            status=payload["status"],
            idea_ids=tuple(payload.get("idea_ids", ())),
        )
    if kind == "execution":
        return VariantSubmission(
            status=payload["status"],
            variant_id=payload["variant_id"],
            commit_sha=payload.get("commit_sha"),
        )
    if kind == "evaluation":
        return EvaluationSubmission(
            status=payload["status"],
            variant_id=payload["variant_id"],
            evaluation=payload.get("evaluation"),
            artifacts_uri=payload.get("artifacts_uri"),
        )
    raise HTTPException(status_code=400, detail=f"unknown task kind {kind!r}")


def _worker_id_from_request(request: Request) -> str:
    """Extract the authenticated ``worker_id`` from a request.

    When auth is enabled (``admin_token`` was passed to :func:`make_app`),
    the §13 middleware sets ``request.state.principal``; this helper
    returns the principal's ``worker_id`` and rejects admin bearers
    on worker-gated routes (§13.3) with :class:`Forbidden`.

    When auth is disabled (test / in-process default), there is no
    principal on ``request.state``; this helper returns the
    ``X-Eden-Worker-Id`` header value if present, otherwise the
    sentinel ``"anonymous"``. The sentinel exists so tests that don't
    care about identity can still drive claim / submit, while tests
    that DO care can opt in by setting the header explicitly.
    """
    principal = getattr(request.state, "principal", None)
    if principal is not None:
        if not principal.is_worker():
            raise Forbidden(
                "endpoint is worker-gated; admin bearers MUST NOT access it (§13.3)"
            )
        assert principal.worker_id is not None
        return principal.worker_id
    # Auth disabled — read the test-only override header, otherwise sentinel.
    return request.headers.get("X-Eden-Worker-Id", "anonymous")


# The pre-12a-1 reference shared-token middleware has been removed in
# favor of the normative §13 per-worker + admin auth implemented in
# :mod:`eden_wire.auth` (`install_auth_middleware`). Callers that
# previously passed ``shared_token=...`` to :func:`make_app` now pass
# ``admin_token=...``; the bearer format and error vocabulary have
# moved to the normative ``eden://error/unauthorized`` /
# ``eden://error/forbidden`` types.
