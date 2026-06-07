"""Shared module-level constants and helpers for the admin sub-package.

Sub-modules (index, observability, actions, work_refs) all import from
here. The split was driven by the
[`docs/audits/2026-05-20-code-quality-audit.md`](../../../../../../../docs/audits/2026-05-20-code-quality-audit.md)
M-1 refactor: the legacy single-file `admin.py` had grown to 1239 SLOC
across 32 handlers, with MI 5.12, accumulated across chunks 9e + 12a-1b
+ 12a-1c + 12c. The split keeps each sub-module under 500 SLOC while
preserving the original `/admin/...` URL surface (the package
re-exports a single `router`).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from typing import Any

from eden_contracts import Task, Variant
from eden_contracts._common import MEMBER_ID_PATTERN
from fastapi import Request
from fastapi.responses import HTMLResponse

_DEFAULT_EVENTS_LIMIT = 200
_MAX_EVENTS_LIMIT = 1000
_TRIAL_DETAIL_EVENT_CAP = 50

_KIND_VALUES = ("ideation", "execution", "evaluation")
_STATE_VALUES = ("pending", "claimed", "submitted", "completed", "failed")
_VARIANT_STATUS_VALUES = ("starting", "success", "error", "evaluation_error")
_IDEA_STATE_VALUES = ("drafting", "ready", "dispatched", "completed")

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
        "target id must be an opaque wkr_*/grp_* member id; pick a worker or group from the lists",
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

# Opaque member-id grammar (``wkr_*`` / ``grp_*``) per
# spec/v0/02-data-model.md §1.6 (identity rename #128). A reassign /
# create-execution target id is a TaskTarget.id (a MemberId).
_MEMBER_ID_RE = re.compile(MEMBER_ID_PATTERN)

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
_WORK_REF_RE = re.compile(r"^refs/heads/work/[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)*$")

_CREATE_EXECUTION_OUTCOMES: dict[str, tuple[str, str]] = {
    "ok": ("ok", "execution task created"),
    "invalid-precondition": (
        "error",
        "this idea is not 'ready' or already has a live execution task",
    ),
    "invalid-target": (
        "error",
        "target id must be an opaque wkr_*/grp_* member id; pick worker / group / none",
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


def _csrf_failure_response() -> HTMLResponse:
    return HTMLResponse(content="CSRF token missing or invalid", status_code=403)


def _read_failure_response(request: Request, message: str) -> HTMLResponse:
    """Inline placeholder for transport-shaped read failures (plan §G)."""
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
    """Resolve the action-result banner via closed-allowlist lookup."""
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


def _repo_has_origin(repo: Any) -> bool:
    """Return True if the GitRepo has an origin remote configured."""
    try:
        result = repo._run(["remote"], check=False)
    except Exception:  # noqa: BLE001
        return False
    return "origin" in result.stdout.split()
