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

import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from typing import Any

from eden_contracts import EvaluationTask, ExperimentConfig, Idea, Variant
from eden_storage import (
    DispatchError,
    EvaluationSubmission,
    InvalidPrecondition,
    StorageError,
)
from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.datastructures import UploadFile

from ..artifacts import (
    UploadedFile,
    entity_artifact_dir,
    submission_naming,
    write_artifact_bundle,
)
from ..forms import parse_evaluate_form
from ._helpers import (
    EligibilityResolver,
    arrange_pending_rows,
    build_artifact_links,
    build_list_links,
    csrf_ok,
    form_experiment_guard,
    get_session,
    is_htmx_request,
    parse_list_view,
    read_idea_content,
    read_idea_manifest,
    read_variant_artifact,
    read_variant_artifact_manifest,
    resolve_active_context,
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
    store: Any,
    pending: list[Any],
    artifacts_dir: Any,
    resolver: EligibilityResolver,
) -> tuple[list[dict[str, Any]], int]:
    """Per pending evaluation task, build a row with variant + idea + eligibility.

    Issue #137 — one ``read_variant`` per row (and one ``read_idea`` for
    parent-idea context: slug / priority / created_by come from the
    idea). ``StorageNotFound`` on either degrades that row gracefully;
    transport-shaped failures increment the page-level counter.
    Eligibility is resolved per row from ``task.target`` (independent of
    the variant/idea reads) via the shared :class:`EligibilityResolver`.

    The row-level ``read_failed`` flag fires ONLY when the *variant*
    itself could not be read. A failed parent-idea read sets
    ``idea_read_failed=True`` (the row still sorts to the bottom, since
    slug/priority are unavailable) and still bumps the page-level read
    counter.
    """
    from eden_storage.errors import NotFound as StorageNotFound

    rows: list[dict[str, Any]] = []
    read_failed = 0
    for task in pending:
        variant: Variant | None = None
        idea: Idea | None = None
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
        eligible, eligibility_unknown = resolver.resolve(task.target)
        artifact_links = (
            build_artifact_links(variant.artifacts_uri, artifacts_dir)
            if variant is not None
            else {"kind": "none"}
        )
        rows.append(
            {
                "task": task,
                "variant": variant,
                "variant_id": task.payload.variant_id,
                "idea": idea,
                "idea_id": variant.idea_id if variant is not None else None,
                "slug": idea.slug if idea is not None else None,
                "priority": idea.priority if idea is not None else None,
                "created_by": idea.created_by if idea is not None else None,
                "target": task.target,
                "eligible": eligible,
                "eligibility_unknown": eligibility_unknown,
                "artifact_links": artifact_links,
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
    active = resolve_active_context(request, need_config=True)
    if isinstance(active, Response):
        return active
    store = active.store
    config = active.config
    assert config is not None  # need_config=True populates it
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
    view = parse_list_view(request.query_params)
    resolver = EligibilityResolver(store, session.worker_id)
    pending_rows, read_failed_count = _build_evaluator_pending_rows(
        store, pending, artifacts_dir, resolver
    )
    arranged_rows, pending_groups = arrange_pending_rows(pending_rows, view)
    return request.app.state.templates.TemplateResponse(
        request,
        "evaluator_list.html",
        {
            "session": session,
            "pending": pending,
            "pending_rows": arranged_rows,
            "pending_groups": pending_groups,
            "view": view,
            "links": build_list_links(view),
            "registered": resolver.registered,
            "registration_unknown": resolver.registration_unknown,
            "eligibility_unresolved_count": resolver.unresolved_count,
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
    active = resolve_active_context(request)
    if isinstance(active, Response):
        return active
    store = active.store
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
    except StorageError as exc:
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
    active = resolve_active_context(request, need_config=True)
    if isinstance(active, Response):
        return active
    store = active.store
    config = active.config
    assert config is not None  # need_config=True populates it
    try:
        variant = store.read_variant(variant_id)
        # A kind == "baseline" variant has no producing idea (02-data-model.md
        # §9.4); evaluate it with no idea panel rather than reading None.
        idea: Idea | None = (
            store.read_idea(variant.idea_id)
            if variant.idea_id is not None
            else None
        )
    except DispatchError as exc:
        return _render_error(request, wire_error_banner(exc))
    except Exception as exc:  # noqa: BLE001 — transport-shaped via StoreClient
        return _render_error(
            request,
            f"task-store transport failure: {exc.__class__.__name__}",
        )
    return _render_draft(
        request,
        config=config,
        session=session,
        task_id=task_id,
        variant=variant,
        idea=idea,
        form_state=_empty_form_state(config),
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
    config: ExperimentConfig,
    session: Any,
    task_id: str,
    variant: Variant,
    idea: Idea | None,
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
            config=config,
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

    active = resolve_active_context(request, need_config=True)
    if isinstance(active, Response):
        return active
    store = active.store
    config = active.config
    assert config is not None  # need_config=True populates it
    mismatch = form_experiment_guard(form, active.experiment_id)
    if mismatch is not None:
        return mismatch
    try:
        variant = store.read_variant(variant_id)
        # A kind == "baseline" variant has no producing idea (02-data-model.md
        # §9.4); evaluate it with no idea panel rather than reading None.
        idea: Idea | None = (
            store.read_idea(variant.idea_id)
            if variant.idea_id is not None
            else None
        )
    except DispatchError as exc:
        return _render_error(request, wire_error_banner(exc))
    except Exception as exc:  # noqa: BLE001 — transport-shaped via StoreClient
        return _render_error(
            request,
            f"task-store transport failure: {exc.__class__.__name__}",
        )

    draft, errors, form_state = _parse_evaluator_submit_form(
        form,
        request=request,
        config=config,
        variant_id=variant_id,
        uploaded=await _collect_uploads(form, field_name="artifact_files"),
    )
    if draft is None or errors:
        return _render_draft(
            request,
            config=config,
            session=session,
            task_id=task_id,
            variant=variant,
            idea=idea,
            form_state=form_state,
            errors=errors,
            status_code=400,
        )

    return _build_and_submit_evaluation(
        request=request,
        store=store,
        config=config,
        session=session,
        task_id=task_id,
        token=token,
        variant=variant,
        idea=idea,
        variant_id=variant_id,
        draft=draft,
        form_state=form_state,
        errors=errors,
    )


def _parse_evaluator_submit_form(
    form: Any,
    *,
    request: Request,
    config: ExperimentConfig,
    variant_id: str,
    uploaded: list[UploadedFile],
) -> tuple[Any, Any, dict[str, Any]]:
    """Read the evaluator submit form and produce ``(draft, errors, form_state)``.

    Pulled out of :func:`submit` so the route handler stays under
    the 100-line slop-prevention cap (issue #120 + the bundling
    branch added ~30 lines). The draft's ``artifacts_uri`` is
    rewritten to point at a freshly-bundled artifact when the
    operator supplied text/uploads instead of an explicit URI.
    """
    evaluation_schema = config.evaluation_schema

    status_raw = str(form.get("status") or "")
    artifacts_uri_raw = str(form.get("artifacts_uri") or "")
    artifact_text_raw = str(form.get("artifact_text") or "")
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
        "artifact_text": artifact_text_raw,
        "metric_values": dict(metric_inputs),
    }
    if draft is None:
        return None, errors, form_state

    written_uri, bundle_error = _maybe_bundle_evaluator_artifact(
        request=request,
        variant_id=variant_id,
        artifacts_uri_raw=artifacts_uri_raw,
        artifact_text_raw=artifact_text_raw,
        uploaded=uploaded,
    )
    if bundle_error is not None:
        from ..forms import FormErrors

        new_errors = errors or FormErrors()
        new_errors.add(0, "artifact", bundle_error)
        return None, new_errors, form_state
    if written_uri is not None:
        draft = replace(draft, artifacts_uri=written_uri)
        form_state["artifacts_uri"] = written_uri
    return draft, errors, form_state


def _maybe_bundle_evaluator_artifact(
    *,
    request: Request,
    variant_id: str,
    artifacts_uri_raw: str,
    artifact_text_raw: str,
    uploaded: list[UploadedFile],
) -> tuple[str | None, str | None]:
    """Decide whether to write a new bundled artifact.

    Returns ``(uri, error)``:

    - ``(None, None)`` if the operator supplied an explicit
      ``artifacts_uri`` OR provided neither text nor uploads — no
      new artifact is written.
    - ``(uri, None)`` on a successful write — the caller pins
      ``draft.artifacts_uri`` to ``uri``.
    - ``(None, msg)`` on a bundling collision / filename rejection
      — the caller turns ``msg`` into a per-field form error.
    """
    if artifacts_uri_raw.strip():
        return None, None
    if not (artifact_text_raw.strip() or uploaded):
        return None, None
    # Issue #168: evaluator artifacts land under
    # variants/<variant_id>/evaluator/, keyed by the stable variant_id; the
    # per-submission eval-<uuid> stem keeps resubmissions distinct (§D.2).
    target_dir = entity_artifact_dir(
        request.app.state.artifacts_dir,
        producer="evaluator",
        entity_id=variant_id,
    )
    naming = submission_naming(
        f"eval-{uuid.uuid4().hex}", headline="evaluation.md"
    )
    try:
        uri = write_artifact_bundle(
            target_dir,
            naming,
            text_content=artifact_text_raw,
            uploads=uploaded,
        )
    except ValueError as exc:
        return None, str(exc)
    return uri, None


async def _collect_uploads(
    form: Any, *, field_name: str
) -> list[UploadedFile]:
    """Pull all ``UploadFile`` entries for ``field_name`` from ``form``.

    Browser file inputs with ``multiple`` post one part per selected
    file, all under the same field name; ``form.getlist(field_name)``
    returns them in order. Drops empty-filename entries (which is
    how Starlette represents "user selected nothing") so the
    bundler doesn't treat the absence-of-uploads as a single empty
    upload. Body bytes are read here so callers see plain bytes
    objects.
    """
    out: list[UploadedFile] = []
    for item in form.getlist(field_name):
        if not isinstance(item, UploadFile):
            continue
        if not item.filename:
            continue
        data = await item.read()
        out.append(
            UploadedFile(
                filename=item.filename,
                data=data,
                content_type=item.content_type,
            )
        )
    return out


def _build_and_submit_evaluation(
    *,
    request: Request,
    store: Any,
    config: ExperimentConfig,
    session: Any,
    task_id: str,
    token: str,
    variant: Variant,
    idea: Idea | None,
    variant_id: str,
    draft: Any,
    form_state: dict[str, Any],
    errors: Any,
) -> HTMLResponse | RedirectResponse:
    """Construct the ``EvaluationSubmission``, submit with read-back,
    and route the outcome through ``_finalize_evaluator_submit``."""
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
        config=config,
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


def _empty_form_state(config: ExperimentConfig) -> dict[str, Any]:
    return {
        "status": "success",
        "artifacts_uri": "",
        "artifact_text": "",
        "metric_values": {name: "" for name in config.evaluation_schema.root},
    }


def _render_draft(
    request: Request,
    *,
    config: ExperimentConfig,
    session: Any,
    task_id: str,
    variant: Variant,
    idea: Idea | None,
    form_state: dict[str, Any],
    errors: Any,
    status_code: int,
) -> HTMLResponse:
    artifacts_dir = request.app.state.artifacts_dir
    idea_content = read_idea_content(idea, artifacts_dir)
    idea_manifest = read_idea_manifest(idea, artifacts_dir)
    variant_artifact_inline = read_variant_artifact(variant.artifacts_uri, artifacts_dir)
    variant_artifact_manifest = read_variant_artifact_manifest(
        variant.artifacts_uri, artifacts_dir
    )
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
            "idea_manifest": idea_manifest,
            "variant_artifact_inline": variant_artifact_inline,
            "variant_artifact_manifest": variant_artifact_manifest,
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


