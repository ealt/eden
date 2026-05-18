"""Admin-module routes — observability views + operator actions.

Implements the chunk 9e plan: read-only views over tasks / variants /
events, an operator ``reclaim`` action on tasks, and a ``work/*`` ref
GC page when ``--repo-path`` is configured.

Auth-first POST discipline: every handler — GET and POST — runs
``get_session(request)`` first; an absent session redirects to
``/signin``. CSRF runs after the auth check on mutating routes.
This matches the ideator / executor / evaluator pattern.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from eden_contracts import (
    EvaluationTask,
    Event,
    ExecutionTask,
    IdeationTask,
    Task,
    TaskTarget,
    Variant,
)
from eden_storage.errors import IllegalTransition, InvalidPrecondition
from eden_storage.errors import NotFound as StorageNotFound
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ._helpers import csrf_ok, get_session, read_idea_content, read_variant_artifact
from ._lineage import (
    lineage_for_evaluation_task,
    lineage_for_execution_task,
    lineage_for_idea,
    lineage_for_ideation_task,
    lineage_for_variant,
)
from .admin_workers import WORKER_FILTER_INVALID, coerce_worker_filter

router = APIRouter(prefix="/admin")


_DEFAULT_EVENTS_LIMIT = 200
_MAX_EVENTS_LIMIT = 1000
_TRIAL_DETAIL_EVENT_CAP = 50

_KIND_VALUES = ("ideation", "execution", "evaluation")
_STATE_VALUES = ("pending", "claimed", "submitted", "completed", "failed")
_VARIANT_STATUS_VALUES = ("starting", "success", "error", "evaluation_error")

# Closed allowlist: ?error=… and ?reclaimed=… banner copy. Keys
# match the querystring values; values are (level, message).
_RECLAIM_OUTCOMES: dict[str, tuple[str, str]] = {
    "ok": ("ok", "task reclaimed"),
    "illegal-transition": (
        "error",
        "this task cannot be reclaimed (terminal or not claimed)",
    ),
    "transport": (
        "error",
        "transport failure; refresh and try again if the task did not move to pending",
    ),
}

_DISPATCH_MODE_OUTCOMES: dict[str, tuple[str, str]] = {
    "ok": ("ok", "dispatch_mode updated"),
    "no-change": ("ok", "no changes — dispatch_mode already at requested values"),
    "invalid-value": (
        "error",
        "every key must be 'auto' or 'manual'",
    ),
    "transport": (
        "error",
        "transport failure; refresh and verify whether your change landed",
    ),
}

_REASSIGN_OUTCOMES: dict[str, tuple[str, str]] = {
    "ok": ("ok", "task reassigned"),
    "no-change": ("ok", "no change — task target already at requested value"),
    "invalid-target": (
        "error",
        "target id must match the §6.1 grammar; pick a worker or group from the lists",
    ),
    "missing-reason": ("error", "reason is required"),
    "illegal-state": (
        "error",
        "this task cannot be reassigned (submitted or terminal)",
    ),
    "unknown-target": (
        "error",
        "the named worker / group is not registered in this experiment",
    ),
    "transport": (
        "error",
        "transport failure; refresh and verify whether your change landed",
    ),
}

# Per spec §2.5 — closed set of dispatch_mode decision keys with
# display labels for the toggle UI. The ordering in this tuple drives
# the on-screen ordering of the toggles; we lead with the
# orchestrator's most-frequent decision (execution_dispatch) and
# finish with integration so the page reads top-to-bottom in
# pipeline order.
_DISPATCH_MODE_KEYS: tuple[tuple[str, str, str], ...] = (
    (
        "ideation_creation",
        "ideation-task creation",
        "Auto-orchestrator creates new ideation tasks per the configured policy.",
    ),
    (
        "execution_dispatch",
        "execution dispatch",
        "Auto-orchestrator creates one execution task per ready idea.",
    ),
    (
        "evaluation_dispatch",
        "evaluation dispatch",
        "Auto-orchestrator creates one evaluation task per starting variant with commit_sha.",
    ),
    (
        "integration",
        "integration",
        "Auto-orchestrator invokes the integrator on success variants.",
    ),
)

_DISPATCH_MODE_VALUES: tuple[str, ...] = ("auto", "manual")

_REASSIGN_TARGET_KINDS: tuple[str, ...] = ("none", "worker", "group")

# Reused from `eden_storage._base` — the §6.1 grammar for worker /
# group ids. Inline so admin.py doesn't reach into a leading-
# underscore module of eden_storage.
_REGISTRY_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


_REF_DELETE_OUTCOMES: dict[str, tuple[str, str]] = {
    "ok": ("ok", "ref deleted"),
    "invalid-ref-name": ("error", "ref name is not a work-branch ref"),
    "not-eligible": ("error", "ref is not eligible for deletion"),
    "not-found": ("error", "ref no longer exists"),
    "ref-changed": (
        "error",
        "ref changed since you loaded the page; refresh and re-confirm",
    ),
    "transport": ("error", "git operation failed; check server logs"),
}

# refs/heads/work/<segment>(/<segment>)*
# Each segment matches [A-Za-z0-9_.-]+; we forbid empty segments and
# anything that could escape the work/ namespace.
_WORK_REF_RE = re.compile(r"^refs/heads/work/[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)*$")


# A sentinel returned by ``_coerce_filter`` to mean "the operator
# typed a filter value that isn't in the allowed set". The route
# layer renders an empty rowset for this case (plan §A.3) — distinct
# from ``None`` which means "no filter applied".
_INVALID_FILTER = "__invalid__"


def _coerce_filter(raw: str | None, allowed: tuple[str, ...]) -> str | None:
    """Map ``raw`` to a value in ``allowed``, ``None`` (no filter), or ``_INVALID_FILTER``."""
    if raw is None or raw == "*" or raw == "":
        return None
    if raw in allowed:
        return raw
    return _INVALID_FILTER


def _claim_age_seconds(task: Task, now: datetime) -> float | None:
    if task.claim is None:
        return None
    claimed_at = _parse_dt(task.claim.claimed_at)
    if claimed_at is None:
        return None
    return (now - claimed_at).total_seconds()


def _claim_expired(task: Task, now: datetime) -> bool:
    if task.claim is None or task.claim.expires_at is None:
        return False
    expires_at = _parse_dt(task.claim.expires_at)
    if expires_at is None:
        return False
    return expires_at < now


def _parse_dt(raw: str | datetime | None) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _variant_terminal_handled(variant: Variant) -> bool:
    """Return True iff the variant has reached a terminal-and-handled status."""
    if variant.status in {"error", "evaluation_error"}:
        return True
    return variant.status == "success" and variant.variant_commit_sha is not None


def _now_dt(request: Request) -> datetime:
    fn: Callable[[], datetime] = request.app.state.now
    return fn()


# ---------------------------------------------------------------------
# Landing page
# ---------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse, response_model=None)
async def index(request: Request) -> HTMLResponse | RedirectResponse:
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    store = request.app.state.store
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

    admin_store = request.app.state.admin_store
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


# ---------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------


@router.get("/tasks/", response_class=HTMLResponse, response_model=None)
async def tasks_index(request: Request) -> HTMLResponse | RedirectResponse:
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    store = request.app.state.store
    now = _now_dt(request)

    kind = _coerce_filter(request.query_params.get("kind"), _KIND_VALUES)
    state = _coerce_filter(request.query_params.get("state"), _STATE_VALUES)
    worker = coerce_worker_filter(request.query_params.get("worker"))

    if (
        kind == _INVALID_FILTER
        or state == _INVALID_FILTER
        or worker == WORKER_FILTER_INVALID
    ):
        tasks: list[Task] = []
        kind_for_select = (
            request.query_params.get("kind", "*")
            if kind == _INVALID_FILTER
            else (kind or "*")
        )
        state_for_select = (
            request.query_params.get("state", "*")
            if state == _INVALID_FILTER
            else (state or "*")
        )
        worker_for_select = (
            request.query_params.get("worker", "")
            if worker == WORKER_FILTER_INVALID
            else (worker or "")
        )
    else:
        try:
            tasks = store.list_tasks(kind=kind, state=state)
        except Exception:  # noqa: BLE001 — transport/store-domain
            return _read_failure_response(request, "could not load tasks")
        if worker is not None:
            # Client-side post-filter; plan §D.8 accepts this for
            # reference-stack scale.
            tasks = [
                t
                for t in tasks
                if (t.claim and t.claim.worker_id == worker)
                or t.submitted_by == worker
                or t.created_by == worker
            ]
        kind_for_select = kind or "*"
        state_for_select = state or "*"
        worker_for_select = worker or ""

    rows: list[dict[str, Any]] = []
    for t in tasks:
        rows.append(
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
        )

    return request.app.state.templates.TemplateResponse(
        request,
        "admin_tasks.html",
        {
            "session": session,
            "rows": rows,
            "selected_kind": kind_for_select,
            "selected_state": state_for_select,
            "selected_worker": worker_for_select,
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
    store = request.app.state.store
    now = _now_dt(request)

    # ``StorageNotFound`` propagates to the app-wide 404 handler.
    # Other exceptions (transport-shaped) get the inline placeholder.
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

    # Lineage (plan §D.6) — dispatch on kind. Build best-effort; per-call
    # transport failures surface via the view model's transport_errors
    # counter, never crash the page.
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


@router.post("/tasks/{task_id}/reclaim", response_model=None)
async def task_reclaim(
    task_id: str,
    request: Request,
    csrf_token: str = Form(""),
) -> RedirectResponse | HTMLResponse:
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    if not csrf_ok(session, csrf_token):
        return _csrf_failure_response()
    store = request.app.state.store
    try:
        store.reclaim(task_id, "operator")
    except IllegalTransition:
        return RedirectResponse(
            url=f"/admin/tasks/{task_id}/?error=illegal-transition",
            status_code=303,
        )
    except Exception:  # noqa: BLE001 — transport-shaped from StoreClient
        return RedirectResponse(
            url=f"/admin/tasks/{task_id}/?error=transport",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/admin/tasks/{task_id}/?reclaimed=ok",
        status_code=303,
    )


@router.get(
    "/tasks/{task_id}/reassign", response_class=HTMLResponse, response_model=None
)
async def task_reassign_form(
    task_id: str, request: Request
) -> HTMLResponse | RedirectResponse:
    """Render the per-task reassign form (12a-2 §2.7).

    Loads the current task plus the registered workers + groups so the
    operator picks from the existing registry instead of typing a
    free-form id. Submitted / terminal tasks render a read-only banner
    explaining that the spec rejects reassign past the claimed phase
    (§6.1), so the operator doesn't waste a POST to discover it.
    """
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    store = request.app.state.store

    try:
        task = store.read_task(task_id)
        workers = store.list_workers()
        groups = store.list_groups()
    except StorageNotFound:
        raise
    except Exception:  # noqa: BLE001 — transport / store domain
        return _read_failure_response(request, "could not load task")

    outcome = _outcome(request, "reassigned", "error", _REASSIGN_OUTCOMES)
    can_reassign = task.state in {"pending", "claimed"}
    current_target = task.target

    return request.app.state.templates.TemplateResponse(
        request,
        "admin_task_reassign.html",
        {
            "session": session,
            "csrf_token": session.csrf,
            "task": task,
            "current_target": current_target,
            "can_reassign": can_reassign,
            "workers": [w.worker_id for w in workers],
            "groups": [g.group_id for g in groups],
            "target_kinds": _REASSIGN_TARGET_KINDS,
            "outcome": outcome,
        },
    )


@router.post("/tasks/{task_id}/reassign", response_model=None)
async def task_reassign(
    task_id: str,
    request: Request,
    target_kind: str = Form(""),
    target_id: str = Form(""),
    target_id_worker: str = Form(""),
    target_id_group: str = Form(""),
    reason: str = Form(""),
    csrf_token: str = Form(""),
) -> RedirectResponse | HTMLResponse:
    """Reassign a task to a new target via :meth:`Store.reassign_task`.

    Per spec §2.7 + §3.6:

    - ``target_kind="none"`` → ``new_target=None`` (the task opens to
      any registered worker that satisfies the eligibility ladder).
    - ``target_kind="worker"`` or ``"group"`` → ``new_target =
      TaskTarget(kind=..., id=target_id)``; ``target_id`` MUST match
      the §6.1 grammar AND name a registered worker / group.
    - ``reason`` is non-empty audit text (the Store rejects empty
      reasons; the route does its own check so the operator gets a
      cleaner banner than the underlying ``InvalidPrecondition``).

    Outcome → ``303`` redirect to the reassign form with a
    closed-allowlist banner. Plan §G error handling: every
    StorageError subclass maps to a distinct banner, transport-shaped
    exceptions map to ``transport`` so the operator can refresh and
    verify.
    """
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    if not csrf_ok(session, csrf_token):
        return _csrf_failure_response()
    store = request.app.state.store

    redirect_base = f"/admin/tasks/{task_id}/reassign"
    if not reason or reason.strip() == "":
        return RedirectResponse(
            url=f"{redirect_base}?error=missing-reason", status_code=303
        )
    if target_kind not in _REASSIGN_TARGET_KINDS:
        return RedirectResponse(
            url=f"{redirect_base}?error=invalid-target", status_code=303
        )

    new_target: TaskTarget | None
    if target_kind == "none":
        new_target = None
    else:
        # The form offers per-kind dropdowns AND a manual override
        # text field, with the manual field taking precedence when
        # both are supplied. Pick the active value in that order so
        # the operator can either click a dropdown row or paste a
        # registered id directly without flipping between fields.
        if target_kind == "worker":
            resolved_id = target_id.strip() or target_id_worker.strip()
        else:  # target_kind == "group"
            resolved_id = target_id.strip() or target_id_group.strip()
        if not _REGISTRY_ID_RE.fullmatch(resolved_id):
            return RedirectResponse(
                url=f"{redirect_base}?error=invalid-target", status_code=303
            )
        target_id = resolved_id
        # Existence check against the registry. The wave-2 Store
        # contract requires the target to identify a real worker /
        # group for the §3.5 eligibility ladder to admit any claimant.
        # We surface "unknown-target" here instead of letting the
        # claim flow silently produce WorkerNotEligible errors on
        # every subsequent attempt.
        try:
            registered = (
                {w.worker_id for w in store.list_workers()}
                if target_kind == "worker"
                else {g.group_id for g in store.list_groups()}
            )
        except Exception:  # noqa: BLE001 — registry read transport blip
            return RedirectResponse(
                url=f"{redirect_base}?error=transport", status_code=303
            )
        if target_id not in registered:
            return RedirectResponse(
                url=f"{redirect_base}?error=unknown-target", status_code=303
            )
        new_target = TaskTarget(kind=target_kind, id=target_id)  # type: ignore[arg-type]

    actor = request.app.state.worker_id
    try:
        updated = store.reassign_task(
            task_id,
            new_target,
            reason=reason.strip(),
            reassigned_by=actor,
        )
    except InvalidPrecondition:
        # Submitted / terminal tasks; OR same-target no-op on the
        # claim-state path (the wave-2 Store treats same-target on
        # pending as a no-op success — claimed always emits the
        # composite). The route can't distinguish these without
        # re-reading, so it reports "illegal-state" for both —
        # operationally the user's next refresh will show the actual
        # task state.
        return RedirectResponse(
            url=f"{redirect_base}?error=illegal-state", status_code=303
        )
    except StorageNotFound:
        raise
    except Exception:  # noqa: BLE001 — transport-shaped from StoreClient
        return RedirectResponse(
            url=f"{redirect_base}?error=transport", status_code=303
        )

    # Wave-2 same-target idempotency: pending + already-at-target
    # emits no event and returns the task unchanged. Surface this to
    # the operator as a distinct "no-change" banner so the absence of
    # an audit-log entry isn't surprising. The composite-commit path
    # (claimed→pending) ALSO ends with state=pending and matching
    # target, so we additionally check whether the latest event for
    # this task is `task.reassigned` — the Store skips emission only
    # on the pending+same-target path.
    if (
        _targets_equal(updated.target, new_target)
        and updated.state == "pending"
        and not _last_event_is_reassign(store, task_id)
    ):
        return RedirectResponse(
            url=f"{redirect_base}?reassigned=no-change", status_code=303
        )
    return RedirectResponse(
        url=f"{redirect_base}?reassigned=ok", status_code=303
    )


def _targets_equal(a: TaskTarget | None, b: TaskTarget | None) -> bool:
    """Structural equality on ``Task.target`` values, None-safe."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return a.kind == b.kind and a.id == b.id


def _last_event_is_reassign(store: Any, task_id: str) -> bool:
    """Return True iff the most recent event referencing ``task_id`` is task.reassigned.

    Helper for the post-write "did the Store actually emit?" branch on
    the same-target idempotent path. The wave-2 Store treats a
    same-target reassign on a `pending` task as a no-op (no event); we
    surface this distinction to the operator via the ``no-change``
    banner.
    """
    try:
        events = store.replay()
    except Exception:  # noqa: BLE001 — best-effort heuristic
        return False
    for ev in reversed(events):
        if ev.data.get("task_id") == task_id:
            return ev.type == "task.reassigned"
    return False


# ---------------------------------------------------------------------
# Dispatch mode
# ---------------------------------------------------------------------


@router.get("/dispatch-mode/", response_class=HTMLResponse, response_model=None)
async def dispatch_mode_form(
    request: Request,
) -> HTMLResponse | RedirectResponse:
    """Render the 4-toggle dispatch_mode page (12a-2 §2.8)."""
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    store = request.app.state.store
    try:
        mode = store.read_dispatch_mode()
    except Exception:  # noqa: BLE001 — transport / store domain
        return _read_failure_response(request, "could not load dispatch_mode")
    outcome = _outcome(request, "dispatched", "error", _DISPATCH_MODE_OUTCOMES)
    mode_dump = mode.model_dump(mode="json", exclude_none=True)
    return request.app.state.templates.TemplateResponse(
        request,
        "admin_dispatch_mode.html",
        {
            "session": session,
            "csrf_token": session.csrf,
            "keys": _DISPATCH_MODE_KEYS,
            "values": _DISPATCH_MODE_VALUES,
            "current": mode_dump,
            "outcome": outcome,
        },
    )


@router.post("/dispatch-mode/", response_model=None)
async def dispatch_mode_update(
    request: Request,
    csrf_token: str = Form(""),
    ideation_creation: str = Form(""),
    execution_dispatch: str = Form(""),
    evaluation_dispatch: str = Form(""),
    integration: str = Form(""),
) -> RedirectResponse | HTMLResponse:
    """Apply a 4-key dispatch_mode update.

    The form submits all four keys every time (even if only one
    toggle changed) because HTML radio groups can't omit a key
    cleanly. The route assembles the dict and lets the wave-2
    ``update_dispatch_mode`` partial-merge semantics no-op the
    unchanged keys; the event payload's ``changed`` diff will only
    record the keys that actually flipped.

    Invalid values (anything outside ``{"auto", "manual"}``) →
    ``?error=invalid-value`` with no Store write.
    """
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    if not csrf_ok(session, csrf_token):
        return _csrf_failure_response()
    store = request.app.state.store

    updates: dict[str, str] = {
        "ideation_creation": ideation_creation,
        "execution_dispatch": execution_dispatch,
        "evaluation_dispatch": evaluation_dispatch,
        "integration": integration,
    }
    for key, value in updates.items():
        if value not in _DISPATCH_MODE_VALUES:
            return RedirectResponse(
                url=f"/admin/dispatch-mode/?error=invalid-value&offending={key}",
                status_code=303,
            )

    actor = request.app.state.worker_id
    try:
        # Read the pre-update state to detect the no-op flip case (so
        # the operator gets a distinct "no-change" banner instead of
        # the generic success message when nothing flipped).
        before = store.read_dispatch_mode().model_dump(
            mode="json", exclude_none=True
        )
        store.update_dispatch_mode(updates, updated_by=actor)
    except InvalidPrecondition:
        return RedirectResponse(
            url="/admin/dispatch-mode/?error=invalid-value", status_code=303
        )
    except Exception:  # noqa: BLE001 — transport-shaped from StoreClient
        return RedirectResponse(
            url="/admin/dispatch-mode/?error=transport", status_code=303
        )

    changed = {k: v for k, v in updates.items() if before.get(k) != v}
    if not changed:
        return RedirectResponse(
            url="/admin/dispatch-mode/?dispatched=no-change", status_code=303
        )
    return RedirectResponse(
        url="/admin/dispatch-mode/?dispatched=ok", status_code=303
    )


# ---------------------------------------------------------------------
# Variants
# ---------------------------------------------------------------------


@router.get("/variants/", response_class=HTMLResponse, response_model=None)
async def variants_index(request: Request) -> HTMLResponse | RedirectResponse:
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    store = request.app.state.store

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
        exec_tasks = store.list_tasks(kind="execution")
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
    store = request.app.state.store
    artifacts_dir = request.app.state.artifacts_dir

    try:
        variant = store.read_variant(variant_id)
        idea = store.read_idea(variant.idea_id)
        events_full = store.replay()
        # Plan §D.10: one list_tasks() per kind per render. Pre-fetch
        # here and pass to both ``_events_for_variant`` and
        # ``lineage_for_variant`` so the helpers don't each repeat the
        # same scan.
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
    idea_id: str,
    exec_tasks: list[Any],
    eval_tasks: list[Any],
) -> tuple[list[Event], int]:
    """Return (events_correlated_to_this_variant_in_replay_order, total_match_count).

    Correlation per chunk-9e plan §A.5: variant-id direct match + task-id
    match for the execution task that produced this variant + task-id
    match for any evaluation task whose payload references this variant.

    Per phase-12a-1c plan §D.10, the caller pre-fetches the kind-scoped
    task lists once and passes them in here AND to ``lineage_for_variant``,
    avoiding duplicate ``store.list_tasks`` scans on the same render.
    """
    exec_task_ids = {
        t.task_id for t in exec_tasks if t.payload.idea_id == idea_id
    }
    eval_task_ids = {
        t.task_id for t in eval_tasks if t.payload.variant_id == variant_id
    }
    related: list[Event] = []
    for ev in events_full:  # replay order
        if ev.data.get("variant_id") == variant_id:
            related.append(ev)
            continue
        tid = ev.data.get("task_id")
        if tid is not None and (tid in exec_task_ids or tid in eval_task_ids):
            related.append(ev)
    return related, len(related)


# ---------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------


@router.get("/events/", response_class=HTMLResponse, response_model=None)
async def events_index(request: Request) -> HTMLResponse | RedirectResponse:
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    store = request.app.state.store

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


# ---------------------------------------------------------------------
# Work-ref GC
# ---------------------------------------------------------------------


@router.get("/work-refs/", response_class=HTMLResponse, response_model=None)
async def work_refs_index(request: Request) -> HTMLResponse | RedirectResponse:
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    repo = request.app.state.repo
    outcome = _outcome(request, "deleted", "error", _REF_DELETE_OUTCOMES)
    if repo is None:
        return request.app.state.templates.TemplateResponse(
            request,
            "admin_work_refs.html",
            {
                "session": session,
                "csrf_token": session.csrf,
                "repo_enabled": False,
                "groups": None,
                "outcome": outcome,
            },
        )
    store = request.app.state.store
    # Phase 10d follow-up B §D.7c read-before-display: fetch from
    # the remote so the operator's view of work/* matches Gitea.
    # No-op when origin is not configured (legacy local-only mode).
    if _repo_has_origin(repo):
        try:
            repo.fetch_all_heads()
        except Exception:  # noqa: BLE001 — git or transport
            return _read_failure_response(
                request, "could not fetch from gitea"
            )
    try:
        groups = _classify_work_refs(repo, store)
    except Exception:  # noqa: BLE001 — git or transport
        return _read_failure_response(request, "could not list work refs")
    return request.app.state.templates.TemplateResponse(
        request,
        "admin_work_refs.html",
        {
            "session": session,
            "csrf_token": session.csrf,
            "repo_enabled": True,
            "groups": groups,
            "outcome": outcome,
        },
    )


@router.post("/work-refs/delete", response_model=None)
async def work_refs_delete(
    request: Request,
    ref_name: str = Form(""),
    expected_old_sha: str = Form(""),  # noqa: ARG001 — accepted for symmetry, never trusted
    csrf_token: str = Form(""),
) -> RedirectResponse | HTMLResponse:
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    if not csrf_ok(session, csrf_token):
        return _csrf_failure_response()
    repo = request.app.state.repo
    if repo is None:
        return RedirectResponse(
            url="/admin/work-refs/?error=invalid-ref-name", status_code=303
        )
    if not _WORK_REF_RE.match(ref_name) or ".." in ref_name:
        return RedirectResponse(
            url="/admin/work-refs/?error=invalid-ref-name", status_code=303
        )
    store = request.app.state.store
    groups = _classify_work_refs(repo, store)
    target = None
    for entry in (*groups["eligible"], *groups["orphan"]):
        if entry["ref_name"] == ref_name:
            target = entry
            break
    if target is None:
        # Could be a not-eligible entry (still listed) or vanished.
        for entry in groups["not_eligible"]:
            if entry["ref_name"] == ref_name:
                return RedirectResponse(
                    url="/admin/work-refs/?error=not-eligible", status_code=303
                )
        return RedirectResponse(
            url="/admin/work-refs/?error=not-found", status_code=303
        )
    live_sha = target["current_sha"]
    from eden_git.repo import GitError

    # Phase 10d follow-up B §D.7c: when origin is configured, the
    # remote IS the source of truth — delete there first. Local
    # delete still happens after so the local clone matches.
    if _repo_has_origin(repo):
        try:
            repo.delete_remote_ref(ref_name, expected_sha=live_sha)
        except GitError as exc:
            stderr = (exc.stderr or "").lower()
            if "stale info" in stderr or "rejected" in stderr:
                return RedirectResponse(
                    url="/admin/work-refs/?error=ref-changed",
                    status_code=303,
                )
            if (
                "deleting unknown ref" in stderr
                or "remote ref does not exist" in stderr
            ):
                return RedirectResponse(
                    url="/admin/work-refs/?error=not-found",
                    status_code=303,
                )
            raise

    try:
        repo.delete_ref(ref_name, expected_old_sha=live_sha)
    except GitError as exc:
        stderr = (exc.stderr or "").lower()
        # ``git update-ref -d <ref> <oldvalue>`` exits 1 with two
        # operationally-different stderrs:
        # - "expected ... but is ..." → CAS mismatch (the SHA we
        #   read at GET-time is no longer the ref's current SHA).
        # - "unable to resolve reference ..." → the ref vanished
        #   between our list_refs read and the delete call.
        # When origin was configured, the remote-delete already
        # succeeded; a local-delete failure here only affects the
        # local clone (a `fetch_all_heads --prune` on the next
        # restart will align it). Surface as the same operator
        # banner as before.
        if "expected" in stderr and "but is" in stderr:
            return RedirectResponse(
                url="/admin/work-refs/?error=ref-changed", status_code=303
            )
        if "unable to resolve reference" in stderr:
            return RedirectResponse(
                url="/admin/work-refs/?error=not-found", status_code=303
            )
        raise
    return RedirectResponse(
        url="/admin/work-refs/?deleted=ok", status_code=303
    )


def _classify_work_refs(repo: Any, store: Any) -> dict[str, list[dict[str, Any]]]:
    """Group ``refs/heads/work/*`` refs by GC eligibility.

    Ownership is keyed off exact ``variant.branch`` equality (chunk 9e
    plan §A.7), not by parsing the ref name.
    """
    variants = store.list_variants()
    branch_index: dict[str, Variant] = {
        tr.branch: tr for tr in variants if tr.branch is not None
    }
    pairs = repo.list_refs("refs/heads/work/*")
    eligible: list[dict[str, Any]] = []
    not_eligible: list[dict[str, Any]] = []
    orphan: list[dict[str, Any]] = []
    for refname, current_sha in pairs:
        branch_name = refname.removeprefix("refs/heads/")
        variant = branch_index.get(branch_name)
        entry: dict[str, Any] = {
            "ref_name": refname,
            "current_sha": current_sha,
            "branch_name": branch_name,
            "variant": variant,
        }
        if variant is None:
            entry["reason"] = "no variant owns this ref"
            orphan.append(entry)
            continue
        if not _variant_terminal_handled(variant):
            entry["reason"] = (
                f"variant is {variant.status}"
                + (
                    " (integrator has not yet integrated)"
                    if variant.status == "success" and variant.variant_commit_sha is None
                    else ""
                )
            )
            not_eligible.append(entry)
            continue
        if variant.commit_sha != current_sha:
            entry["reason"] = (
                "ref SHA does not match variant.commit_sha (manual rewrite?)"
            )
            not_eligible.append(entry)
            continue
        entry["reason"] = (
            f"variant {variant.variant_id} is {variant.status}; safe to delete"
        )
        eligible.append(entry)
    return {
        "eligible": eligible,
        "not_eligible": not_eligible,
        "orphan": orphan,
    }


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _repo_has_origin(repo: Any) -> bool:
    """Return True if the GitRepo has an origin remote configured.

    Phase 10d follow-up B: gates the read-before-display fetch and
    the remote-delete on the work-refs admin page. Pre-cutover (no
    --gitea-url) deployments skip the new code paths entirely.
    """
    try:
        result = repo._run(["remote"], check=False)
    except Exception:  # noqa: BLE001
        return False
    return "origin" in result.stdout.split()


def _csrf_failure_response() -> HTMLResponse:
    return HTMLResponse(content="CSRF token missing or invalid", status_code=403)


def _read_failure_response(request: Request, message: str) -> HTMLResponse:
    """Inline placeholder for transport-shaped read failures (plan §G).

    We render the standard ``_error.html`` page with a 502 so the
    operator sees a clear "transport failure; refresh to retry"
    surface instead of an unhandled 500 from the underlying
    ``StoreClient`` / ``GitRepo`` exception.
    """
    return request.app.state.templates.TemplateResponse(
        request,
        "_error.html",
        {
            "title": "Transport failure",
            "message": (
                f"{message}; refresh to retry. If the failure persists, "
                "check the task-store-server logs."
            ),
        },
        status_code=502,
    )


def _outcome(
    request: Request,
    ok_param: str,
    err_param: str,
    table: dict[str, tuple[str, str]],
) -> dict[str, str] | None:
    """Resolve the action-result banner by looking up the closed allowlist.

    ``ok_param`` is the success querystring key (``reclaimed``,
    ``deleted``); ``err_param`` is always ``error``. The value is
    looked up in ``table``; an unknown value renders no banner.
    """
    raw_ok = request.query_params.get(ok_param)
    raw_err = request.query_params.get(err_param)
    raw = raw_err if raw_err else raw_ok
    if raw is None:
        return None
    pair = table.get(raw)
    if pair is None:
        return None
    level, message = pair
    return {"level": level, "message": message}


# ---------------------------------------------------------------------
# Ideas (12a-3 wave 5)
# ---------------------------------------------------------------------

_IDEA_STATE_VALUES = ("drafting", "ready", "dispatched", "completed")

_CREATE_EXECUTION_OUTCOMES: dict[str, tuple[str, str]] = {
    "ok": ("ok", "execution task created"),
    "invalid-precondition": (
        "error",
        "this idea is not 'ready' or already has a live execution task",
    ),
    "invalid-target": (
        "error",
        "target id must match the §6.1 registry-id grammar; pick worker / group / none",
    ),
    "not-found": ("error", "idea not found"),
    "illegal-transition": (
        "error",
        "the experiment is terminated; new execution tasks are forbidden",
    ),
    "transport": (
        "error",
        "transport failure; refresh and verify whether the task landed",
    ),
}


@router.get("/ideas/", response_class=HTMLResponse, response_model=None)
async def ideas_index(request: Request) -> HTMLResponse | RedirectResponse:
    """List every idea, optionally filtered by state.

    Read-only view; per-idea actions (admin-driven
    create-execution-task) hang off the idea-detail page. Each row
    carries the 12a-1c transparency columns (slug / priority /
    parent_commits[:8] / created_by / intended_executor /
    variant_count / created_at).
    """
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    store = request.app.state.store
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
    """Per-idea detail page; surfaces intended_executor + create-task form.

    Phase 12a-1c additions: ``inline_content`` (markdown body via
    ``read_idea_content`` trust-boundary helper) and ``lineage``
    (one hop back to ideation task + one hop forward to spawned
    variants per plan §D.8).
    """
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    store = request.app.state.store
    artifacts_dir = request.app.state.artifacts_dir
    try:
        idea = store.read_idea(idea_id)
        live_execution_tasks = [
            t
            for t in store.list_tasks(kind="execution")
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


@router.post(
    "/ideas/{idea_id}/create-execution-task", response_model=None
)
async def create_execution_task(
    idea_id: str,
    request: Request,
    csrf_token: str = Form(""),
    target_kind: str = Form("none"),
    target_id: str = Form(""),
) -> RedirectResponse | HTMLResponse:
    """Admin-driven execution-task creation (12a-3 §6.5 authority lift).

    The pre-12a-3 path was orchestrators-only; 12a-3 broadens the wire
    authority to admins OR orchestrators now that
    ``idea.intended_executor`` gives operators a non-fungible routing
    seed. The UI POSTs here; the route calls
    ``Store.create_execution_task`` with an optional ``target``
    override (defaults to the idea's ``intended_executor`` when
    ``target_kind == "none"``).
    """
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    if not csrf_ok(session, csrf_token):
        return _csrf_failure_response()
    target_kind = target_kind.strip().lower()
    target_id = target_id.strip()
    redirect_base = f"/admin/ideas/{idea_id}/"
    if target_kind not in ("none", "worker", "group"):
        return RedirectResponse(
            url=f"{redirect_base}?error=invalid-target", status_code=303
        )
    target: TaskTarget | None = None
    if target_kind != "none":
        if not _REGISTRY_ID_RE.fullmatch(target_id):
            return RedirectResponse(
                url=f"{redirect_base}?error=invalid-target", status_code=303
            )
        target = TaskTarget(kind=target_kind, id=target_id)
    elif target_id:
        # Mis-click: kind=none + id supplied.
        return RedirectResponse(
            url=f"{redirect_base}?error=invalid-target", status_code=303
        )
    store = request.app.state.store
    task_id = f"execution-{uuid.uuid4().hex[:12]}"
    try:
        if target is None:
            store.create_execution_task(task_id, idea_id)
        else:
            store.create_execution_task(task_id, idea_id, target=target)
    except StorageNotFound:
        return RedirectResponse(
            url=f"{redirect_base}?error=not-found", status_code=303
        )
    except IllegalTransition:
        return RedirectResponse(
            url=f"{redirect_base}?error=illegal-transition", status_code=303
        )
    except InvalidPrecondition:
        return RedirectResponse(
            url=f"{redirect_base}?error=invalid-precondition", status_code=303
        )
    except Exception:  # noqa: BLE001 — transport/store-domain
        return RedirectResponse(
            url=f"{redirect_base}?error=transport", status_code=303
        )
    return RedirectResponse(
        url=f"{redirect_base}?created=ok", status_code=303
    )


# ---------------------------------------------------------------------
# Experiment lifecycle (12a-3 wave 5)
# ---------------------------------------------------------------------

_TERMINATE_OUTCOMES: dict[str, tuple[str, str]] = {
    "ok": ("ok", "experiment terminated"),
    "already-terminated": (
        "ok",
        "experiment was already terminated (idempotent no-op)",
    ),
    "missing-reason": ("error", "reason is required"),
    "admin-disabled": (
        "error",
        "the admin bearer is not configured; cannot drive lifecycle ops",
    ),
    "transport": (
        "error",
        "transport failure; refresh and verify the lifecycle state",
    ),
}


@router.get(
    "/experiment/", response_class=HTMLResponse, response_model=None
)
async def experiment_detail(
    request: Request,
) -> HTMLResponse | RedirectResponse:
    """Experiment-lifecycle dashboard (12a-3).

    Surfaces the runtime ``Experiment`` (state + created_at) plus the
    terminate-experiment action when the experiment is still running.
    """
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    store = request.app.state.store
    admin_store = request.app.state.admin_store
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


@router.post("/experiment/terminate", response_model=None)
async def terminate_experiment(
    request: Request,
    csrf_token: str = Form(""),
    reason: str = Form(""),
) -> RedirectResponse | HTMLResponse:
    """POST /admin/experiment/terminate — admin-driven lifecycle transition."""
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    if not csrf_ok(session, csrf_token):
        return _csrf_failure_response()
    admin_store = request.app.state.admin_store
    if admin_store is None:
        return RedirectResponse(
            url="/admin/experiment/?error=admin-disabled", status_code=303
        )
    reason = reason.strip()
    if not reason:
        return RedirectResponse(
            url="/admin/experiment/?error=missing-reason", status_code=303
        )
    # The session.worker_id is the principal driving the transition;
    # the Store stamps it onto the experiment.terminated event.
    try:
        experiment = admin_store.terminate_experiment(
            reason=reason, terminated_by=session.worker_id
        )
    except Exception:  # noqa: BLE001 — transport/store-domain
        return RedirectResponse(
            url="/admin/experiment/?error=transport", status_code=303
        )
    # Idempotent: terminate_experiment returns success even when the
    # experiment was already terminated. We surface that as a distinct
    # banner so the operator isn't surprised by a duplicate-click.
    if experiment.state == "terminated":
        # We can't easily tell from the return value whether this call
        # OR a prior call won the race. The event log is authoritative;
        # if our reason matches the stored event we won.
        try:
            events = admin_store.replay()
        except Exception:  # noqa: BLE001 — non-fatal
            return RedirectResponse(
                url="/admin/experiment/?terminated=ok", status_code=303
            )
        winning = next(
            (e for e in events if e.type == "experiment.terminated"),
            None,
        )
        if winning is None or winning.data.get("reason") != reason:
            return RedirectResponse(
                url="/admin/experiment/?terminated=already-terminated",
                status_code=303,
            )
    return RedirectResponse(
        url="/admin/experiment/?terminated=ok", status_code=303
    )


__all__ = ["router"]
