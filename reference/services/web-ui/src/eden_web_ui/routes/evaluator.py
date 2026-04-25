"""Evaluator-module routes.

Implements the spec-to-code map pinned in §C of the Phase 9d plan.
The flow is: list pending evaluate tasks → claim with TTL +
server-pinned ``trial_id`` from ``task.payload.trial_id`` → render
draft form (read-only trial context, optional inline rationale +
trial-side artifact, per-metric inputs generated from the
experiment's ``metrics_schema``) → submit, which runs

1. validate the form (status in {success, error, eval_error};
   metric values type-check against ``metrics_schema``;
   ``status="success"`` requires at least one metric)
2. ``store.submit`` with retry-before-orphan plus a
   committed-state read-back. ``IllegalTransition`` falls
   through to read-back rather than short-circuiting (so the
   "we won, the orchestrator already terminalized, our retry
   only saw the new state" case is correctly classified as
   success). ``WrongToken`` and ``ConflictingResubmission``
   short-circuit (definitive). ``InvalidPrecondition`` re-renders
   the draft form with a wire-error banner so a fixable metric
   drift does not orphan the operator.

``trial_id`` is read from ``task.payload.trial_id`` at claim time
and stored in ``_CLAIMS``; it never round-trips through the
request surface.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from eden_contracts import EvaluateTask, Proposal, Trial
from eden_storage import (
    ConflictingResubmission,
    DispatchError,
    EvaluateSubmission,
    IllegalTransition,
    InvalidPrecondition,
    WrongToken,
)
from eden_storage.submissions import submissions_equivalent
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..forms import parse_evaluate_form
from ._helpers import (
    csrf_ok,
    get_session,
    is_htmx_request,
    read_proposal_rationale,
    read_trial_artifact,
)

router = APIRouter(prefix="/evaluator")


# In-memory mapping of (csrf_token, task_id) -> (claim token, trial_id).
# Same shape as the chunk-9c implementer's _CLAIMS but a separate
# module-level dict so two simultaneous claims (one implement, one
# evaluate) on tasks that happen to share an id don't collide.
_CLAIMS: dict[tuple[str, str], tuple[str, str]] = {}


def _claim_key(session_csrf: str, task_id: str) -> tuple[str, str]:
    return (session_csrf, task_id)


def _list_recent_trials(store: Any, *, limit: int = 20) -> list[Any]:
    items = store.list_trials()
    return items[-limit:]


@router.get("/", response_class=HTMLResponse, response_model=None)
async def list_pending(request: Request) -> HTMLResponse | RedirectResponse:
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    store = request.app.state.store
    try:
        pending = store.list_tasks(kind="evaluate", state="pending")
        recent = _list_recent_trials(store)
    except DispatchError as exc:
        return _render_error(request, _wire_error_banner(exc))
    except Exception as exc:  # noqa: BLE001 — transport-shaped via StoreClient
        return _render_error(
            request, f"task-store transport failure: {exc.__class__.__name__}"
        )
    config = request.app.state.experiment_config
    return request.app.state.templates.TemplateResponse(
        request,
        "evaluator_list.html",
        {
            "session": session,
            "pending": pending,
            "objective": config.objective,
            "recent_trials": recent,
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
        banner = _wire_error_banner(exc)
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
    if not isinstance(task, EvaluateTask):
        return RedirectResponse(
            url="/evaluator/?banner=task+is+not+an+evaluate+task",
            status_code=303,
        )
    trial_id = task.payload.trial_id
    now: Callable[[], Any] = request.app.state.now
    expires_at = now() + timedelta(seconds=request.app.state.claim_ttl_seconds)
    try:
        result = store.claim(task_id, session.worker_id, expires_at=expires_at)
    except (IllegalTransition, InvalidPrecondition) as exc:
        banner = _wire_error_banner(exc)
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
    _CLAIMS[_claim_key(session.csrf, task_id)] = (result.token, trial_id)
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
    _, trial_id = entry
    store = request.app.state.store
    try:
        trial = store.read_trial(trial_id)
        proposal: Proposal = store.read_proposal(trial.proposal_id)
    except DispatchError as exc:
        return _render_error(request, _wire_error_banner(exc))
    except Exception as exc:  # noqa: BLE001 — transport-shaped via StoreClient
        return _render_error(
            request,
            f"task-store transport failure: {exc.__class__.__name__}",
        )
    return _render_draft(
        request,
        session=session,
        task_id=task_id,
        trial=trial,
        proposal=proposal,
        form_state=_empty_form_state(request),
        errors=None,
        status_code=200,
    )


@router.post("/{task_id}/submit", response_model=None)
async def submit(  # noqa: PLR0911 — many distinct outcome arms by design
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
    token, trial_id = entry

    store = request.app.state.store
    try:
        trial = store.read_trial(trial_id)
        proposal: Proposal = store.read_proposal(trial.proposal_id)
    except DispatchError as exc:
        return _render_error(request, _wire_error_banner(exc))
    except Exception as exc:  # noqa: BLE001 — transport-shaped via StoreClient
        return _render_error(
            request,
            f"task-store transport failure: {exc.__class__.__name__}",
        )

    config = request.app.state.experiment_config
    metrics_schema = config.metrics_schema

    status_raw = str(form.get("status") or "")
    artifacts_uri_raw = str(form.get("artifacts_uri") or "")
    # Collect every submitted `metric.*` field so the parser can
    # reject unknown metric keys (a hand-crafted POST is the only
    # way they appear; the template only emits inputs for declared
    # metrics). Default declared schema keys to "" so the parser
    # also sees blanks for the omit-on-empty branch.
    metric_inputs: dict[str, str] = {name: "" for name in metrics_schema.root}
    for raw_key in form:
        if not isinstance(raw_key, str) or not raw_key.startswith("metric."):
            continue
        name = raw_key[len("metric."):]
        metric_inputs[name] = str(form.get(raw_key) or "")

    draft, errors = parse_evaluate_form(
        metrics_schema=metrics_schema,
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
            trial=trial,
            proposal=proposal,
            form_state=form_state,
            errors=errors,
            status_code=400,
        )

    submission = EvaluateSubmission(
        status=draft.status,
        trial_id=trial_id,
        metrics=dict(draft.metrics) if draft.metrics else None,
        artifacts_uri=draft.artifacts_uri,
    )

    outcome, banner = _retry_submit_with_readback(
        store=store, task_id=task_id, token=token, submission=submission
    )

    if outcome == "ok":
        _CLAIMS.pop(_claim_key(session.csrf, task_id), None)
        return _render_submitted(
            request,
            task_id=task_id,
            trial_id=trial_id,
            status=draft.status,
            metrics=submission.metrics,
            artifacts_uri=draft.artifacts_uri,
        )
    if outcome == "invalid-precondition":
        # Fixable: re-render the form with a wire-error banner so
        # the operator can correct the metrics and resubmit.
        if errors is None:
            from ..forms import FormErrors

            errors = FormErrors()
        errors.add_overall(banner or "eden://error/invalid-precondition")
        return _render_draft(
            request,
            session=session,
            task_id=task_id,
            trial=trial,
            proposal=proposal,
            form_state=form_state,
            errors=errors,
            status_code=400,
        )
    return _render_orphaned(
        request,
        task_id=task_id,
        trial_id=trial_id,
        status=draft.status,
        banner=banner or "submit failed",
        recovery_kind=outcome,
    )


_RETRY_DELAYS_S = (0.05, 0.2, 0.5)


def _retry_submit_with_readback(
    *,
    store: Any,
    task_id: str,
    token: str,
    submission: EvaluateSubmission,
) -> tuple[str, str | None]:
    """Submit with retry; reconcile transport / IllegalTransition via read-back.

    Returns one of:

    - ``("ok", None)`` — submit committed (clean call, retry, or
      read-back found an equivalent prior submission).
    - ``("auto", banner)`` — orphan page; auto-recovers via
      reclaim. Used for ``WrongToken`` short-circuit, transport
      retry exhaustion with claim still ours, and the read-back
      branch where the task has been reclaimed.
    - ``("conflict", banner)`` — orphan page; a different
      submission won the race
      (``ConflictingResubmission`` short-circuit, or read-back
      saw a non-equivalent committed payload).
    - ``("transport", banner)`` — orphan page; an
      implementation-illegal store state was observed during
      read-back (``read_submission`` returned ``None`` for a
      ``submitted`` / ``completed`` / ``failed`` task) or the
      read-back probe itself failed.
    - ``("invalid-precondition", banner)`` — form re-render; the
      server rejected the metrics shape. Fixable.
    """
    last_exc: BaseException | None = None
    needs_readback = False
    for delay in _RETRY_DELAYS_S:
        try:
            store.submit(task_id, token, submission)
            return "ok", None
        except WrongToken as exc:
            return "auto", _wire_error_banner(exc)
        except ConflictingResubmission as exc:
            return "conflict", _wire_error_banner(exc)
        except InvalidPrecondition as exc:
            return "invalid-precondition", _wire_error_banner(exc)
        except IllegalTransition as exc:
            # Could be: state==pending (we lost) | completed/failed
            # (we won, orchestrator terminalized) | submitted by
            # someone else (conflict). Read-back resolves it.
            last_exc = exc
            needs_readback = True
            break
        except Exception as exc:  # noqa: BLE001 — transport-shaped
            last_exc = exc
            time.sleep(delay)

    if not needs_readback and last_exc is None:
        # All retries returned cleanly is impossible (we'd return
        # "ok" inside the loop). Defensive.
        return "transport", "submit returned without exception or commit"

    return _readback(
        store=store, task_id=task_id, token=token, submission=submission, last_exc=last_exc
    )


def _readback(
    *,
    store: Any,
    task_id: str,
    token: str,
    submission: EvaluateSubmission,
    last_exc: BaseException | None,
) -> tuple[str, str | None]:
    last_name = last_exc.__class__.__name__ if last_exc else "unknown"
    try:
        task = store.read_task(task_id)
    except Exception as exc:  # noqa: BLE001
        return (
            "transport",
            f"transport failure after retries; read-back failed: {exc.__class__.__name__}",
        )
    state = task.state
    if state == "claimed":
        if task.claim is not None and task.claim.token == token:
            return ("auto", f"transport failure after retries: {last_name}")
        return "auto", "eden://error/wrong-token"
    if state in {"submitted", "completed", "failed"}:
        try:
            prior = store.read_submission(task_id)
        except Exception as exc:  # noqa: BLE001
            return (
                "transport",
                (
                    "transport failure after retries; "
                    f"read-submission failed: {exc.__class__.__name__}"
                ),
            )
        if prior is None:
            return (
                "transport",
                "store invariant violation: submission missing for terminal/submitted task",
            )
        if submissions_equivalent(prior, submission):
            return "ok", None
        return "conflict", "eden://error/conflicting-resubmission"
    # state == "pending"
    return ("auto", f"transport failure after retries; task reclaimed: {last_name}")


def _empty_form_state(request: Request) -> dict[str, Any]:
    config = request.app.state.experiment_config
    return {
        "status": "success",
        "artifacts_uri": "",
        "metric_values": {name: "" for name in config.metrics_schema.root},
    }


def _render_draft(
    request: Request,
    *,
    session: Any,
    task_id: str,
    trial: Trial,
    proposal: Proposal,
    form_state: dict[str, Any],
    errors: Any,
    status_code: int,
) -> HTMLResponse:
    artifacts_dir = request.app.state.artifacts_dir
    proposal_rationale = read_proposal_rationale(proposal, artifacts_dir)
    trial_artifact_inline = read_trial_artifact(trial.artifacts_uri, artifacts_dir)
    config = request.app.state.experiment_config
    metric_schema_items = list(config.metrics_schema.root.items())
    return request.app.state.templates.TemplateResponse(
        request,
        "evaluator_claim.html",
        {
            "session": session,
            "task_id": task_id,
            "trial": trial,
            "proposal": proposal,
            "proposal_rationale": proposal_rationale,
            "trial_artifact_inline": trial_artifact_inline,
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
    trial_id: str,
    status: str,
    metrics: dict[str, Any] | None,
    artifacts_uri: str | None,
) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request,
        "evaluator_submitted.html",
        {
            "task_id": task_id,
            "trial_id": trial_id,
            "status": status,
            "metrics": metrics or {},
            "artifacts_uri": artifacts_uri,
        },
    )


def _render_orphaned(
    request: Request,
    *,
    task_id: str,
    trial_id: str,
    status: str,
    banner: str,
    recovery_kind: str,
) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request,
        "evaluator_orphaned.html",
        {
            "task_id": task_id,
            "trial_id": trial_id,
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


_ERROR_NAMES: dict[type, str] = {
    WrongToken: "eden://error/wrong-token",
    IllegalTransition: "eden://error/illegal-transition",
    ConflictingResubmission: "eden://error/conflicting-resubmission",
    InvalidPrecondition: "eden://error/invalid-precondition",
}


def _wire_error_banner(exc: BaseException) -> str:
    name = _ERROR_NAMES.get(type(exc))
    if name is None:
        return f"unexpected error: {exc.__class__.__name__}"
    return name
