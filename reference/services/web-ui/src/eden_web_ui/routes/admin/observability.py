"""Read-only admin views: tasks / variants / events / ideas / experiment."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from eden_contracts import (
    EvaluationTask,
    Event,
    ExecutionTask,
    IdeationTask,
    Task,
)
from eden_storage.errors import NotFound as StorageNotFound
from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from .._helpers import (
    get_session,
    read_idea_content,
    read_variant_artifact,
    resolve_active_context,
)
from .._lineage import (
    lineage_for_evaluation_task,
    lineage_for_execution_task,
    lineage_for_idea,
    lineage_for_ideation_task,
    lineage_for_variant,
)
from ..admin_workers import WORKER_FILTER_INVALID, coerce_worker_filter
from ._common import (
    _CREATE_EXECUTION_OUTCOMES,
    _DEFAULT_EVENTS_LIMIT,
    _IDEA_STATE_VALUES,
    _INVALID_FILTER,
    _KIND_VALUES,
    _MAX_EVENTS_LIMIT,
    _RECLAIM_OUTCOMES,
    _STATE_VALUES,
    _TERMINATE_OUTCOMES,
    _TRIAL_DETAIL_EVENT_CAP,
    _VARIANT_STATUS_VALUES,
    _claim_age_seconds,
    _claim_expired,
    _coerce_filter,
    _now_dt,
    _outcome,
    _read_failure_response,
)

router = APIRouter(prefix="/admin")


@dataclass(frozen=True)
class _TaskFilters:
    """Parsed task-list filters + render-time select values.

    ``kind`` / ``state`` / ``worker`` are the values to pass to the
    store (``None`` ≡ unfiltered). ``invalid`` short-circuits the
    route to an empty rowset (plan §A.3). The ``*_select`` fields
    preserve the raw user input so the form keeps the operator's
    typed value visible after a redirect.
    """

    kind: str | None
    state: str | None
    worker: str | None
    kind_select: str
    state_select: str
    worker_select: str
    invalid: bool


def _parse_task_filters(request: Request) -> _TaskFilters:
    raw_kind = request.query_params.get("kind")
    raw_state = request.query_params.get("state")
    raw_worker = request.query_params.get("worker")
    kind = _coerce_filter(raw_kind, _KIND_VALUES)
    state = _coerce_filter(raw_state, _STATE_VALUES)
    worker = coerce_worker_filter(raw_worker)
    invalid = (
        kind == _INVALID_FILTER
        or state == _INVALID_FILTER
        or worker == WORKER_FILTER_INVALID
    )
    if invalid:
        kind_select = (
            (raw_kind or "*") if kind == _INVALID_FILTER else (kind or "*")
        )
        state_select = (
            (raw_state or "*") if state == _INVALID_FILTER else (state or "*")
        )
        worker_select = (
            (raw_worker or "")
            if worker == WORKER_FILTER_INVALID
            else (worker or "")
        )
    else:
        kind_select = kind or "*"
        state_select = state or "*"
        worker_select = worker or ""
    return _TaskFilters(
        kind=None if invalid else kind,
        state=None if invalid else state,
        worker=None if invalid else worker,
        kind_select=kind_select,
        state_select=state_select,
        worker_select=worker_select,
        invalid=invalid,
    )


def _filter_tasks_by_worker(tasks: list[Task], worker: str) -> list[Task]:
    return [
        t
        for t in tasks
        if (t.claim and t.claim.worker_id == worker)
        or t.submitted_by == worker
        or t.created_by == worker
    ]


@router.get("/tasks/", response_class=HTMLResponse, response_model=None)
async def tasks_index(request: Request) -> HTMLResponse | RedirectResponse:
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    active = resolve_active_context(request)
    if isinstance(active, Response):
        return active
    store = active.store
    now = _now_dt(request)

    filters = _parse_task_filters(request)
    if filters.invalid:
        tasks: list[Task] = []
    else:
        try:
            tasks = store.list_tasks(kind=filters.kind, state=filters.state)
        except Exception:  # noqa: BLE001 — transport/store-domain
            return _read_failure_response(request, "could not load tasks")
        if filters.worker is not None:
            # Client-side post-filter; plan §D.8 accepts this at the
            # reference-stack scale.
            tasks = _filter_tasks_by_worker(tasks, filters.worker)

    rows: list[dict[str, Any]] = [
        {
            "task_id": t.task_id,
            "kind": t.kind,
            "state": t.state,
            "worker_id": t.claim.worker_id if t.claim else None,
            "claim_age": _claim_age_seconds(t, now),
            "expires_at": t.claim.expires_at if t.claim else None,
            "updated_at": t.updated_at,
            "claim_expired": _claim_expired(t, now),
        }
        for t in tasks
    ]

    return request.app.state.templates.TemplateResponse(
        request,
        "admin_tasks.html",
        {
            "session": session,
            "rows": rows,
            "selected_kind": filters.kind_select,
            "selected_state": filters.state_select,
            "selected_worker": filters.worker_select,
            "kinds": _KIND_VALUES,
            "states": _STATE_VALUES,
        },
    )


@router.get("/tasks/{task_id}/", response_class=HTMLResponse, response_model=None)
async def task_detail(
    task_id: str, request: Request
) -> HTMLResponse | RedirectResponse:
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    active = resolve_active_context(request)
    if isinstance(active, Response):
        return active
    store = active.store
    now = _now_dt(request)

    try:
        task = store.read_task(task_id)
        events_full = store.replay()
    except StorageNotFound:
        raise
    except Exception:  # noqa: BLE001 — transport/store-domain
        return _read_failure_response(request, "could not load task")
    related = [ev for ev in events_full if ev.data.get("task_id") == task_id]

    reclaim_outcome = _outcome(request, "reclaimed", "error", _RECLAIM_OUTCOMES)
    can_reclaim = task.state in {"claimed", "submitted"}
    is_force = task.state == "submitted"

    lineage: Any
    if isinstance(task, IdeationTask):
        lineage = lineage_for_ideation_task(store, task)
    elif isinstance(task, ExecutionTask):
        lineage = lineage_for_execution_task(store, task)
    elif isinstance(task, EvaluationTask):
        lineage = lineage_for_evaluation_task(store, task)
    else:  # pragma: no cover — TaskAdapter union is exhaustive
        lineage = None

    return request.app.state.templates.TemplateResponse(
        request,
        "admin_task_detail.html",
        {
            "session": session,
            "csrf_token": session.csrf,
            "task": task,
            "payload_json": task.payload.model_dump(mode="json"),
            "claim_age": _claim_age_seconds(task, now),
            "claim_expired": _claim_expired(task, now),
            "related_events": list(reversed(related))[:50],
            "outcome": reclaim_outcome,
            "can_reclaim": can_reclaim,
            "is_force": is_force,
            "lineage": lineage,
        },
    )


@router.get("/variants/", response_class=HTMLResponse, response_model=None)
async def variants_index(request: Request) -> HTMLResponse | RedirectResponse:
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    active = resolve_active_context(request)
    if isinstance(active, Response):
        return active
    store = active.store

    status = _coerce_filter(request.query_params.get("status"), _VARIANT_STATUS_VALUES)
    worker = coerce_worker_filter(request.query_params.get("worker"))
    if status == _INVALID_FILTER or worker == WORKER_FILTER_INVALID:
        return request.app.state.templates.TemplateResponse(
            request,
            "admin_variants.html",
            {
                "session": session,
                "rows": [],
                "selected_status": (
                    request.query_params.get("status", "*")
                    if status == _INVALID_FILTER
                    else (status or "*")
                ),
                "selected_worker": (
                    request.query_params.get("worker", "")
                    if worker == WORKER_FILTER_INVALID
                    else (worker or "")
                ),
                "variant_statuses": _VARIANT_STATUS_VALUES,
            },
        )

    try:
        variants = store.list_variants(status=status)
        exec_tasks = cast(
            "list[ExecutionTask]", store.list_tasks(kind="execution")
        )
    except Exception:  # noqa: BLE001 — transport/store-domain
        return _read_failure_response(request, "could not load variants")
    exec_terminal_by_idea: dict[str, bool] = {
        t.payload.idea_id: (t.state in {"completed", "failed"})
        for t in exec_tasks
    }
    if worker is not None:
        variants = [
            v
            for v in variants
            if v.executed_by == worker or v.evaluated_by == worker
        ]

    rows: list[dict[str, Any]] = []
    for tr in variants:
        orphaned = (
            tr.status == "starting"
            and tr.idea_id is not None
            and exec_terminal_by_idea.get(tr.idea_id, False)
        )
        rows.append(
            {
                "variant_id": tr.variant_id,
                "idea_id": tr.idea_id,
                "status": tr.status,
                "branch": tr.branch,
                "commit_sha": tr.commit_sha,
                "variant_commit_sha": tr.variant_commit_sha,
                "started_at": tr.started_at,
                "completed_at": tr.completed_at,
                "orphaned": orphaned,
            }
        )

    return request.app.state.templates.TemplateResponse(
        request,
        "admin_variants.html",
        {
            "session": session,
            "rows": rows,
            "selected_status": status or "*",
            "selected_worker": worker or "",
            "variant_statuses": _VARIANT_STATUS_VALUES,
        },
    )


@router.get(
    "/variants/{variant_id}/", response_class=HTMLResponse, response_model=None
)
async def variant_detail(
    variant_id: str, request: Request
) -> HTMLResponse | RedirectResponse:
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    active = resolve_active_context(request)
    if isinstance(active, Response):
        return active
    store = active.store
    artifacts_dir = request.app.state.artifacts_dir

    try:
        variant = store.read_variant(variant_id)
        # A kind == "baseline" variant has no producing idea (02-data-model.md
        # §9.4); render it with no idea panel rather than looking up None.
        idea = (
            store.read_idea(variant.idea_id)
            if variant.idea_id is not None
            else None
        )
        events_full = store.replay()
        exec_tasks = store.list_tasks(kind="execution")
        eval_tasks = store.list_tasks(kind="evaluation")
        related, total_related = _events_for_variant(
            events_full,
            variant_id=variant_id,
            idea_id=variant.idea_id,
            exec_tasks=exec_tasks,
            eval_tasks=eval_tasks,
        )
    except StorageNotFound:
        raise
    except Exception:  # noqa: BLE001 — transport/store-domain
        return _read_failure_response(request, "could not load variant")

    inline_artifact = read_variant_artifact(variant.artifacts_uri, artifacts_dir)

    lineage = lineage_for_variant(
        store,
        variant,
        exec_tasks=exec_tasks,
        eval_tasks=eval_tasks,
    )

    return request.app.state.templates.TemplateResponse(
        request,
        "admin_variant_detail.html",
        {
            "session": session,
            "variant": variant,
            "idea": idea,
            "related_events": list(reversed(related))[:_TRIAL_DETAIL_EVENT_CAP],
            "related_total": total_related,
            "variant_artifact_inline": inline_artifact,
            "lineage": lineage,
        },
    )


def _events_for_variant(
    events_full: list[Event],
    *,
    variant_id: str,
    idea_id: str | None,
    exec_tasks: list[Any],
    eval_tasks: list[Any],
) -> tuple[list[Event], int]:
    """Return (events_correlated_to_this_variant_in_replay_order, total_match_count)."""
    exec_task_ids = {
        t.task_id for t in exec_tasks if t.payload.idea_id == idea_id
    }
    eval_task_ids = {
        t.task_id for t in eval_tasks if t.payload.variant_id == variant_id
    }
    related: list[Event] = []
    for ev in events_full:
        if ev.data.get("variant_id") == variant_id:
            related.append(ev)
            continue
        tid = ev.data.get("task_id")
        if tid is not None and (tid in exec_task_ids or tid in eval_task_ids):
            related.append(ev)
    return related, len(related)


@router.get("/events/", response_class=HTMLResponse, response_model=None)
async def events_index(request: Request) -> HTMLResponse | RedirectResponse:
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    active = resolve_active_context(request)
    if isinstance(active, Response):
        return active
    store = active.store

    raw_limit = request.query_params.get("limit")
    limit = _DEFAULT_EVENTS_LIMIT
    if raw_limit is not None:
        try:
            limit = max(1, min(_MAX_EVENTS_LIMIT, int(raw_limit)))
        except ValueError:
            limit = _DEFAULT_EVENTS_LIMIT

    type_filter = request.query_params.get("type") or None

    try:
        events_full = store.replay()
    except Exception:  # noqa: BLE001 — transport/store-domain
        return _read_failure_response(request, "could not load events")
    total = len(events_full)

    indexed: list[tuple[int, Event]] = []
    for idx, ev in enumerate(events_full, start=1):
        if type_filter is not None and ev.type != type_filter:
            continue
        indexed.append((idx, ev))

    filtered_total = len(indexed)
    sliced = indexed[-limit:]
    rows = list(reversed(sliced))

    return request.app.state.templates.TemplateResponse(
        request,
        "admin_events.html",
        {
            "session": session,
            "rows": rows,
            "limit": limit,
            "total": total,
            "filtered_total": filtered_total,
            "type_filter": type_filter,
        },
    )


@router.get("/ideas/", response_class=HTMLResponse, response_model=None)
async def ideas_index(request: Request) -> HTMLResponse | RedirectResponse:
    """List every idea, optionally filtered by state."""
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    active = resolve_active_context(request)
    if isinstance(active, Response):
        return active
    store = active.store
    state = _coerce_filter(request.query_params.get("state"), _IDEA_STATE_VALUES)
    if state == _INVALID_FILTER:
        return request.app.state.templates.TemplateResponse(
            request,
            "admin_ideas.html",
            {
                "session": session,
                "rows": [],
                "idea_states": _IDEA_STATE_VALUES,
                "selected_state": request.query_params.get("state", "*"),
            },
        )
    try:
        ideas_list = store.list_ideas(state=state) if state else store.list_ideas()
        variants = store.list_variants()
    except Exception:  # noqa: BLE001 — transport/store-domain
        return _read_failure_response(request, "could not load ideas")

    variant_count_by_idea: dict[str, int] = {}
    for v in variants:
        # A kind == "baseline" variant has no producing idea (02-data-model.md
        # §9.4), so it contributes to no per-idea count.
        if v.idea_id is None:
            continue
        variant_count_by_idea[v.idea_id] = (
            variant_count_by_idea.get(v.idea_id, 0) + 1
        )

    rows: list[dict[str, Any]] = []
    for idea in ideas_list:
        rows.append(
            {
                "idea_id": idea.idea_id,
                "slug": idea.slug,
                "priority": idea.priority,
                "state": idea.state,
                "created_by": idea.created_by,
                "parent_commits_preview": [
                    sha[:8] for sha in idea.parent_commits
                ],
                "intended_executor": getattr(idea, "intended_executor", None),
                "variant_count": variant_count_by_idea.get(idea.idea_id, 0),
                "created_at": idea.created_at,
            }
        )

    return request.app.state.templates.TemplateResponse(
        request,
        "admin_ideas.html",
        {
            "session": session,
            "rows": rows,
            "idea_states": _IDEA_STATE_VALUES,
            "selected_state": state or "*",
        },
    )


@router.get(
    "/ideas/{idea_id}/", response_class=HTMLResponse, response_model=None
)
async def idea_detail(
    idea_id: str, request: Request
) -> HTMLResponse | RedirectResponse:
    """Per-idea detail page; surfaces intended_executor + create-task form."""
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    active = resolve_active_context(request)
    if isinstance(active, Response):
        return active
    store = active.store
    artifacts_dir = request.app.state.artifacts_dir
    try:
        idea = store.read_idea(idea_id)
        live_execution_tasks = [
            t
            for t in cast(
                "list[ExecutionTask]", store.list_tasks(kind="execution")
            )
            if t.payload.idea_id == idea_id
        ]
        workers_list = store.list_workers()
        groups_list = store.list_groups()
    except StorageNotFound:
        raise
    except Exception:  # noqa: BLE001 — transport/store-domain
        return _read_failure_response(request, "could not load idea")
    can_create_execution = idea.state == "ready" and not any(
        t.state in ("pending", "claimed", "submitted") for t in live_execution_tasks
    )
    inline_content = read_idea_content(idea, artifacts_dir)
    lineage = lineage_for_idea(store, idea)
    return request.app.state.templates.TemplateResponse(
        request,
        "admin_idea_detail.html",
        {
            "session": session,
            "idea": idea,
            "inline_content": inline_content,
            "lineage": lineage,
            "live_execution_tasks": live_execution_tasks,
            "workers": [w.worker_id for w in workers_list],
            "groups": [g.group_id for g in groups_list],
            "can_create_execution": can_create_execution,
            "outcome": _outcome(
                request, "created", "error", _CREATE_EXECUTION_OUTCOMES
            ),
        },
    )


@router.get(
    "/experiment/", response_class=HTMLResponse, response_model=None
)
async def experiment_detail(
    request: Request,
) -> HTMLResponse | RedirectResponse:
    """Experiment-lifecycle dashboard (12a-3)."""
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    active = resolve_active_context(request)
    if isinstance(active, Response):
        return active
    store = active.store
    admin_store = active.admin_store
    try:
        experiment = store.read_experiment()
        events_full = store.replay()
    except Exception:  # noqa: BLE001 — transport/store-domain
        return _read_failure_response(request, "could not load experiment state")
    terminated_event = next(
        (e for e in events_full if e.type == "experiment.terminated"),
        None,
    )
    policy_errors = [
        e for e in events_full if e.type == "experiment.policy_error"
    ]
    return request.app.state.templates.TemplateResponse(
        request,
        "admin_experiment.html",
        {
            "session": session,
            "experiment": experiment,
            "terminated_event": terminated_event,
            "policy_errors": policy_errors[-10:],
            "admin_disabled": admin_store is None,
            "outcome": _outcome(
                request, "terminated", "error", _TERMINATE_OUTCOMES
            ),
        },
    )
