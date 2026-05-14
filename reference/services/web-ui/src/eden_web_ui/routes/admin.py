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
from collections.abc import Callable
from datetime import datetime
from typing import Any

from eden_contracts import Event, Task, Variant
from eden_storage.errors import IllegalTransition
from eden_storage.errors import NotFound as StorageNotFound
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ._helpers import csrf_ok, get_session, read_variant_artifact
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
        related, total_related = _events_for_variant(
            events_full, store, variant_id=variant_id, idea_id=variant.idea_id
        )
    except StorageNotFound:
        raise
    except Exception:  # noqa: BLE001 — transport/store-domain
        return _read_failure_response(request, "could not load variant")

    inline_artifact = read_variant_artifact(variant.artifacts_uri, artifacts_dir)

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
        },
    )


def _events_for_variant(
    events_full: list[Event],
    store: Any,
    *,
    variant_id: str,
    idea_id: str,
) -> tuple[list[Event], int]:
    """Return (events_correlated_to_this_variant_in_replay_order, total_match_count).

    Correlation per chunk-9e plan §A.5: variant-id direct match + task-id
    match for the execution task that produced this variant + task-id
    match for any evaluation task whose payload references this variant.
    """
    exec_task_ids = {
        t.task_id
        for t in store.list_tasks(kind="execution")
        if t.payload.idea_id == idea_id
    }
    eval_task_ids = {
        t.task_id
        for t in store.list_tasks(kind="evaluation")
        if t.payload.variant_id == variant_id
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


__all__ = ["router"]
