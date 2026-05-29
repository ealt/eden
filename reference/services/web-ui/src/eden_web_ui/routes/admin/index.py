"""Admin dashboard landing page (the ``/admin/`` route)."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from .._helpers import get_session, resolve_active_context
from ._common import (
    _KIND_VALUES,
    _STATE_VALUES,
    _VARIANT_STATUS_VALUES,
    _claim_expired,
    _now_dt,
    _read_failure_response,
)

router = APIRouter(prefix="/admin")


@router.get("/", response_class=HTMLResponse, response_model=None)
async def index(request: Request) -> HTMLResponse | RedirectResponse:
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    active = resolve_active_context(request)
    if isinstance(active, Response):
        return active
    store = active.store
    repo = request.app.state.repo
    now = _now_dt(request)

    try:
        tasks = store.list_tasks()
        variants = store.list_variants()
        events_full = store.replay()
        workers_list = store.list_workers()
        groups_list = store.list_groups()
        experiment_state = store.read_experiment_state()
    except Exception:  # noqa: BLE001 — transport/store-domain
        return _read_failure_response(request, "could not load dashboard data")

    cross_tab: dict[str, dict[str, int]] = {
        kind: {state: 0 for state in _STATE_VALUES} for kind in _KIND_VALUES
    }
    expired_count = 0
    for t in tasks:
        if t.kind in cross_tab and t.state in cross_tab[t.kind]:
            cross_tab[t.kind][t.state] += 1
        if _claim_expired(t, now):
            expired_count += 1

    variant_counts = {status: 0 for status in _VARIANT_STATUS_VALUES}
    for tr in variants:
        if tr.status in variant_counts:
            variant_counts[tr.status] += 1

    # Three distinct states for ``work_ref_count`` so the dashboard
    # template can disambiguate them (plan §G + finding 4 from the
    # impl-review): ``None`` means "no --repo-path configured";
    # ``-1`` means "git read failed"; otherwise the count.
    work_ref_count: int | None = None
    work_ref_error: bool = False
    if repo is not None:
        try:
            work_ref_count = len(repo.list_refs("refs/heads/work/*"))
        except Exception:  # noqa: BLE001 — git failure on dashboard read
            work_ref_count = None
            work_ref_error = True

    recent = list(reversed(events_full))[:10]

    admin_store = active.admin_store
    return request.app.state.templates.TemplateResponse(
        request,
        "admin_index.html",
        {
            "session": session,
            "cross_tab": cross_tab,
            "kinds": _KIND_VALUES,
            "states": _STATE_VALUES,
            "variant_counts": variant_counts,
            "variant_statuses": _VARIANT_STATUS_VALUES,
            "event_total": len(events_full),
            "work_ref_count": work_ref_count,
            "work_ref_error": work_ref_error,
            "repo_enabled": repo is not None,
            "expired_count": expired_count,
            "recent_events": recent,
            "worker_count": len(workers_list),
            "group_count": len(groups_list),
            "admin_disabled": admin_store is None,
            "experiment_state": experiment_state,
        },
    )
