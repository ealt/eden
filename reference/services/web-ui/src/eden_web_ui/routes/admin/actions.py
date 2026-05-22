"""Admin mutating routes — operator actions on tasks / dispatch-mode / ideas / experiment."""

from __future__ import annotations

import uuid
from typing import Any

from eden_contracts import TaskTarget
from eden_storage.errors import IllegalTransition, InvalidPrecondition
from eden_storage.errors import NotFound as StorageNotFound
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .._helpers import csrf_ok, get_session
from ._common import (
    _DISPATCH_MODE_KEYS,
    _DISPATCH_MODE_OUTCOMES,
    _DISPATCH_MODE_VALUES,
    _REASSIGN_OUTCOMES,
    _REASSIGN_TARGET_KINDS,
    _REGISTRY_ID_RE,
    _csrf_failure_response,
    _outcome,
    _read_failure_response,
)

router = APIRouter(prefix="/admin")


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
    """Render the per-task reassign form (12a-2 §2.7)."""
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


class _ReassignBail(Exception):
    """Raised by ``_resolve_new_target`` to short-circuit the route with a banner."""

    def __init__(self, error: str) -> None:
        super().__init__(error)
        self.error = error


def _resolve_new_target(
    store: Any,
    target_kind: str,
    target_id: str,
    target_id_worker: str,
    target_id_group: str,
) -> TaskTarget | None:
    """Resolve operator form input into a ``TaskTarget`` (or ``None`` for "any worker").

    Raises :class:`_ReassignBail` with the closed-allowlist banner key
    when the input is malformed, the id fails the §6.1 grammar, the
    registry read fails, or the resolved id isn't registered.
    """
    if target_kind == "none":
        return None
    # The form offers per-kind dropdowns AND a manual override text
    # field, with the manual field taking precedence when both are
    # supplied. Pick the active value in that order so the operator
    # can either click a dropdown row or paste a registered id
    # directly without flipping between fields.
    fallback = target_id_worker if target_kind == "worker" else target_id_group
    resolved_id = target_id.strip() or fallback.strip()
    if not _REGISTRY_ID_RE.fullmatch(resolved_id):
        raise _ReassignBail("invalid-target")
    # Existence check against the registry. The wave-2 Store contract
    # requires the target to identify a real worker / group for the
    # §3.5 eligibility ladder to admit any claimant. We surface
    # "unknown-target" here instead of letting the claim flow
    # silently produce WorkerNotEligible errors on every attempt.
    try:
        registered = (
            {w.worker_id for w in store.list_workers()}
            if target_kind == "worker"
            else {g.group_id for g in store.list_groups()}
        )
    except Exception as exc:  # noqa: BLE001 — registry read transport blip
        raise _ReassignBail("transport") from exc
    if resolved_id not in registered:
        raise _ReassignBail("unknown-target")
    return TaskTarget(kind=target_kind, id=resolved_id)  # type: ignore[arg-type]


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
    """Reassign a task to a new target via :meth:`Store.reassign_task`."""
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    if not csrf_ok(session, csrf_token):
        return _csrf_failure_response()
    store = request.app.state.store

    redirect_base = f"/admin/tasks/{task_id}/reassign"
    reason = reason.strip()
    if not reason:
        return RedirectResponse(
            url=f"{redirect_base}?error=missing-reason", status_code=303
        )
    if target_kind not in _REASSIGN_TARGET_KINDS:
        return RedirectResponse(
            url=f"{redirect_base}?error=invalid-target", status_code=303
        )

    try:
        new_target = _resolve_new_target(
            store, target_kind, target_id, target_id_worker, target_id_group
        )
    except _ReassignBail as bail:
        return RedirectResponse(
            url=f"{redirect_base}?error={bail.error}", status_code=303
        )

    actor = request.app.state.worker_id
    try:
        updated = store.reassign_task(
            task_id, new_target, reason=reason, reassigned_by=actor
        )
    except InvalidPrecondition:
        # Submitted / terminal tasks; OR same-target no-op on the
        # claim-state path. The route can't distinguish without
        # re-reading, so it reports "illegal-state" for both.
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
    # an audit-log entry isn't surprising.
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
    """Return True iff the most recent event referencing ``task_id`` is task.reassigned."""
    try:
        events = store.replay()
    except Exception:  # noqa: BLE001 — best-effort heuristic
        return False
    for ev in reversed(events):
        if ev.data.get("task_id") == task_id:
            return ev.type == "task.reassigned"
    return False


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
    """Apply a 4-key dispatch_mode update."""
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
    """Admin-driven execution-task creation (12a-3 §6.5 authority lift)."""
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
    try:
        experiment = admin_store.terminate_experiment(
            reason=reason, terminated_by=session.worker_id
        )
    except Exception:  # noqa: BLE001 — transport/store-domain
        return RedirectResponse(
            url="/admin/experiment/?error=transport", status_code=303
        )
    if experiment.state == "terminated":
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
