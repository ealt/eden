"""Evaluator-module routes.

Implements the spec-to-code map pinned in §C of the Phase 9d plan.
The flow is: list pending evaluation tasks → claim with TTL +
server-pinned ``variant_id`` from ``task.payload.variant_id`` → render
draft form (read-only variant context, optional inline content +
variant-side artifact, per-metric inputs generated from the
experiment's ``evaluation_schema``) → submit, which runs

1. validate the form (status in {success, error, evaluation_error};
   metric values type-check against ``evaluation_schema``;
   ``status="success"`` requires at least one metric)
2. ``store.submit`` with retry-before-orphan plus a
   committed-state read-back. ``IllegalTransition`` falls
   through to read-back rather than short-circuiting (so the
   "we won, the orchestrator already terminalized, our retry
   only saw the new state" case is correctly classified as
   success). ``NotClaimed`` and ``ConflictingResubmission``
   short-circuit (definitive). ``InvalidPrecondition`` re-renders
   the draft form with a wire-error banner so a fixable metric
   drift does not orphan the operator.

``variant_id`` is read from ``task.payload.variant_id`` at claim time
and stored in ``_CLAIMS``; it never round-trips through the
request surface.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

from eden_contracts import EvaluationTask, Idea, Variant
from eden_storage import (
    DispatchError,
    EvaluationSubmission,
    IllegalTransition,
    InvalidPrecondition,
)
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..forms import parse_evaluate_form
from ._helpers import (
    csrf_ok,
    get_session,
    is_htmx_request,
    read_idea_content,
    read_variant_artifact,
)
from ._submit_readback import submit_with_readback, wire_error_banner

router = APIRouter(prefix="/evaluator")


# In-memory mapping of (csrf_token, task_id) -> (claim token, variant_id).
# Same shape as the chunk-9c executor's _CLAIMS but a separate
# module-level dict so two simultaneous claims (one implement, one
# evaluate) on tasks that happen to share an id don't collide.
_CLAIMS: dict[tuple[str, str], tuple[str, str]] = {}


def _claim_key(session_csrf: str, task_id: str) -> tuple[str, str]:
    return (session_csrf, task_id)


def _list_recent_variants(store: Any, *, limit: int = 20) -> list[Any]:
    items = store.list_variants()
    return items[-limit:]


def _build_evaluator_pending_rows(
    store: Any, pending: list[Any], artifacts_dir: Any
) -> tuple[list[dict[str, Any]], int]:
    """Per pending evaluation task, build a preview row with variant + artifact.

    Plan §D.5 — one ``read_variant`` per row (and one ``read_idea``
    for parent-idea context). ``StorageNotFound`` on either degrades
    that row gracefully; transport-shaped failures increment the
    page-level counter.

    Per codex r4 W2: the row-level ``read_failed`` flag fires ONLY
    when the *variant* itself could not be read. A failed parent-idea
    read sets ``idea_read_failed=True`` on the row but leaves the
    variant context (branch / commit / executed_by / artifact) intact;
    the template surfaces a localized "(idea: read error)" note inline
    while still incrementing the page-level read-failed counter so the
    "N read(s) failed" banner fires.
    """
    from eden_storage.errors import NotFound as StorageNotFound

    rows: list[dict[str, Any]] = []
    read_failed = 0
    for task in pending:
        variant: Variant | None = None
        idea: Idea | None = None
        variant_artifact: str | None = None
        read_failed_row = False
        idea_read_failed = False
        try:
            variant = store.read_variant(task.payload.variant_id)
        except StorageNotFound:
            variant = None
        except Exception:  # noqa: BLE001 — transport-shaped
            variant = None
            read_failed_row = True
            read_failed += 1
        if variant is not None:
            try:
                idea = store.read_idea(variant.idea_id)
            except StorageNotFound:
                idea = None
            except Exception:  # noqa: BLE001 — transport-shaped
                idea = None
                idea_read_failed = True
                read_failed += 1
            try:
                variant_artifact = read_variant_artifact(
                    variant.artifacts_uri, artifacts_dir
                )
            except Exception:  # noqa: BLE001 — defensive
                variant_artifact = None
        rows.append(
            {
                "task": task,
                "variant": variant,
                "idea": idea,
                "variant_artifact": variant_artifact,
                "target": task.target,
                "lineage_link": f"/admin/tasks/{task.task_id}/",
                "read_failed": read_failed_row,
                "idea_read_failed": idea_read_failed,
            }
        )
    return rows, read_failed


@router.get("/", response_class=HTMLResponse, response_model=None)
async def list_pending(request: Request) -> HTMLResponse | RedirectResponse:
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    store = request.app.state.store
    artifacts_dir = request.app.state.artifacts_dir
    try:
        pending = store.list_tasks(kind="evaluation", state="pending")
        recent = _list_recent_variants(store)
    except DispatchError as exc:
        return _render_error(request, wire_error_banner(exc))
    except Exception as exc:  # noqa: BLE001 — transport-shaped via StoreClient
        return _render_error(
            request, f"task-store transport failure: {exc.__class__.__name__}"
        )
    config = request.app.state.experiment_config
    pending_rows, read_failed_count = _build_evaluator_pending_rows(
        store, pending, artifacts_dir
    )
    return request.app.state.templates.TemplateResponse(
        request,
        "evaluator_list.html",
        {
            "session": session,
            "pending": pending,
            "pending_rows": pending_rows,
            "read_failed_count": read_failed_count,
            "objective": config.objective,
            "recent_variants": recent,
            "banner": request.query_params.get("banner"),
        },
    )


@router.post("/{task_id}/claim", response_model=None)
async def claim(
    task_id: str,
    request: Request,
    csrf_token: str = Form(""),
) -> RedirectResponse | HTMLResponse:
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    if not csrf_ok(session, csrf_token):
        return _csrf_failure_response(request)
    store = request.app.state.store
    try:
        task = store.read_task(task_id)
    except DispatchError as exc:
        banner = wire_error_banner(exc)
        return RedirectResponse(
            url=f"/evaluator/?banner={banner}", status_code=303
        )
    except Exception as exc:  # noqa: BLE001 — transport-shaped via StoreClient
        return RedirectResponse(
            url=(
                "/evaluator/?banner=task-store+transport+failure:+"
                f"{exc.__class__.__name__}"
            ),
            status_code=303,
        )
    if not isinstance(task, EvaluationTask):
        return RedirectResponse(
            url="/evaluator/?banner=task+is+not+an+evaluate+task",
            status_code=303,
        )
    variant_id = task.payload.variant_id
    now: Callable[[], Any] = request.app.state.now
    expires_at = now() + timedelta(seconds=request.app.state.claim_ttl_seconds)
    try:
        result = store.claim(task_id, session.worker_id, expires_at=expires_at)
    except (IllegalTransition, InvalidPrecondition) as exc:
        banner = wire_error_banner(exc)
        return RedirectResponse(
            url=f"/evaluator/?banner={banner}", status_code=303
        )
    except Exception as exc:  # noqa: BLE001 — transport-shaped via StoreClient
        return RedirectResponse(
            url=(
                "/evaluator/?banner=task-store+transport+failure:+"
                f"{exc.__class__.__name__}"
            ),
            status_code=303,
        )
    _CLAIMS[_claim_key(session.csrf, task_id)] = (result.worker_id, variant_id)
    return RedirectResponse(url=f"/evaluator/{task_id}/draft", status_code=303)


@router.get("/{task_id}/draft", response_class=HTMLResponse, response_model=None)
async def draft_form(
    task_id: str,
    request: Request,
) -> HTMLResponse | RedirectResponse:
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    entry = _CLAIMS.get(_claim_key(session.csrf, task_id))
    if entry is None:
        return RedirectResponse(
            url="/evaluator/?banner=claim+missing+from+session",
            status_code=303,
        )
    _, variant_id = entry
    store = request.app.state.store
    try:
        variant = store.read_variant(variant_id)
        idea: Idea = store.read_idea(variant.idea_id)
    except DispatchError as exc:
        return _render_error(request, wire_error_banner(exc))
    except Exception as exc:  # noqa: BLE001 — transport-shaped via StoreClient
        return _render_error(
            request,
            f"task-store transport failure: {exc.__class__.__name__}",
        )
    return _render_draft(
        request,
        session=session,
        task_id=task_id,
        variant=variant,
        idea=idea,
        form_state=_empty_form_state(request),
        errors=None,
        status_code=200,
    )


def _collect_metric_inputs(form: Any, evaluation_schema: Any) -> dict[str, str]:
    """Collect every submitted ``metric.*`` form field.

    Defaults declared schema keys to ``""`` so the parser sees blanks
    for the omit-on-empty branch. Reads back unknown ``metric.*`` keys
    too so the parser can reject them (a hand-crafted POST is the only
    way they appear; the template only emits inputs for declared
    metrics).
    """
    metric_inputs: dict[str, str] = {name: "" for name in evaluation_schema.root}
    for raw_key in form:
        if not isinstance(raw_key, str) or not raw_key.startswith("metric."):
            continue
        name = raw_key[len("metric."):]
        metric_inputs[name] = str(form.get(raw_key) or "")
    return metric_inputs


def _finalize_evaluator_submit(
    *,
    request: Request,
    session: Any,
    task_id: str,
    variant: Variant,
    idea: Idea,
    variant_id: str,
    draft: Any,
    submission: EvaluationSubmission,
    form_state: dict[str, Any],
    errors: Any,
    outcome: str,
    banner: str | None,
) -> HTMLResponse | RedirectResponse:
    """Render the appropriate response for a submit_with_readback outcome.

    ``ok`` → submitted page. ``invalid-precondition`` → redraft with
    wire-error banner (fixable; spec lets the operator correct metrics
    and resubmit). Otherwise → orphan page.
    """
    if outcome == "ok":
        _CLAIMS.pop(_claim_key(session.csrf, task_id), None)
        return _render_submitted(
            request,
            task_id=task_id,
            variant_id=variant_id,
            status=draft.status,
            evaluation=submission.evaluation,
            artifacts_uri=draft.artifacts_uri,
        )
    if outcome == "invalid-precondition":
        if errors is None:
            from ..forms import FormErrors

            errors = FormErrors()
        errors.add_overall(banner or "eden://error/invalid-precondition")
        return _render_draft(
            request,
            session=session,
            task_id=task_id,
            variant=variant,
            idea=idea,
            form_state=form_state,
            errors=errors,
            status_code=400,
        )
    return _render_orphaned(
        request,
        task_id=task_id,
        variant_id=variant_id,
        status=draft.status,
        banner=banner or "submit failed",
        recovery_kind=outcome,
    )


@router.post("/{task_id}/submit", response_model=None)
async def submit(
    task_id: str,
    request: Request,
) -> HTMLResponse | RedirectResponse:
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    form = await request.form()
    if not csrf_ok(session, form.get("csrf_token")):  # type: ignore[arg-type]
        return _csrf_failure_response(request)

    entry = _CLAIMS.get(_claim_key(session.csrf, task_id))
    if entry is None:
        return RedirectResponse(
            url="/evaluator/?banner=claim+missing+from+session",
            status_code=303,
        )
    token, variant_id = entry

    store = request.app.state.store
    try:
        variant = store.read_variant(variant_id)
        idea: Idea = store.read_idea(variant.idea_id)
    except DispatchError as exc:
        return _render_error(request, wire_error_banner(exc))
    except Exception as exc:  # noqa: BLE001 — transport-shaped via StoreClient
        return _render_error(
            request,
            f"task-store transport failure: {exc.__class__.__name__}",
        )

    config = request.app.state.experiment_config
    evaluation_schema = config.evaluation_schema

    status_raw = str(form.get("status") or "")
    artifacts_uri_raw = str(form.get("artifacts_uri") or "")
    metric_inputs = _collect_metric_inputs(form, evaluation_schema)

    draft, errors = parse_evaluate_form(
        evaluation_schema=evaluation_schema,
        status_raw=status_raw,
        metric_inputs=metric_inputs,
        artifacts_uri_raw=artifacts_uri_raw,
    )
    form_state: dict[str, Any] = {
        "status": status_raw or "success",
        "artifacts_uri": artifacts_uri_raw,
        "metric_values": dict(metric_inputs),
    }
    if draft is None:
        return _render_draft(
            request,
            session=session,
            task_id=task_id,
            variant=variant,
            idea=idea,
            form_state=form_state,
            errors=errors,
            status_code=400,
        )

    submission = EvaluationSubmission(
        status=draft.status,
        variant_id=variant_id,
        evaluation=dict(draft.evaluation) if draft.evaluation else None,
        artifacts_uri=draft.artifacts_uri,
    )

    outcome, banner = submit_with_readback(
        store=store,
        task_id=task_id,
        token=token,
        submission=submission,
        extra_catches=((InvalidPrecondition, "invalid-precondition"),),
    )
    return _finalize_evaluator_submit(
        request=request,
        session=session,
        task_id=task_id,
        variant=variant,
        idea=idea,
        variant_id=variant_id,
        draft=draft,
        submission=submission,
        form_state=form_state,
        errors=errors,
        outcome=outcome,
        banner=banner,
    )


def _empty_form_state(request: Request) -> dict[str, Any]:
    config = request.app.state.experiment_config
    return {
        "status": "success",
        "artifacts_uri": "",
        "metric_values": {name: "" for name in config.evaluation_schema.root},
    }


def _render_draft(
    request: Request,
    *,
    session: Any,
    task_id: str,
    variant: Variant,
    idea: Idea,
    form_state: dict[str, Any],
    errors: Any,
    status_code: int,
) -> HTMLResponse:
    artifacts_dir = request.app.state.artifacts_dir
    idea_content = read_idea_content(idea, artifacts_dir)
    variant_artifact_inline = read_variant_artifact(variant.artifacts_uri, artifacts_dir)
    config = request.app.state.experiment_config
    metric_schema_items = list(config.evaluation_schema.root.items())
    return request.app.state.templates.TemplateResponse(
        request,
        "evaluator_claim.html",
        {
            "session": session,
            "task_id": task_id,
            "variant": variant,
            "idea": idea,
            "idea_content": idea_content,
            "variant_artifact_inline": variant_artifact_inline,
            "metric_schema_items": metric_schema_items,
            "form_state": form_state,
            "errors": errors,
        },
        status_code=status_code,
    )


def _render_submitted(
    request: Request,
    *,
    task_id: str,
    variant_id: str,
    status: str,
    evaluation: dict[str, Any] | None,
    artifacts_uri: str | None,
) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request,
        "evaluator_submitted.html",
        {
            "task_id": task_id,
            "variant_id": variant_id,
            "status": status,
            "evaluation": evaluation or {},
            "artifacts_uri": artifacts_uri,
        },
    )


def _render_orphaned(
    request: Request,
    *,
    task_id: str,
    variant_id: str,
    status: str,
    banner: str,
    recovery_kind: str,
) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request,
        "evaluator_orphaned.html",
        {
            "task_id": task_id,
            "variant_id": variant_id,
            "status": status,
            "banner": banner,
            "recovery_kind": recovery_kind,
        },
        status_code=502,
    )


def _render_error(request: Request, message: str) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request,
        "_error.html",
        {"title": "Wire error", "message": message},
        status_code=502,
    )


def _csrf_failure_response(request: Request | None = None) -> HTMLResponse:
    headers: dict[str, str] = {}
    if request is not None and is_htmx_request(request):
        headers["hx-reswap"] = "none"
    return HTMLResponse(
        content="CSRF token missing or invalid",
        status_code=403,
        headers=headers,
    )


