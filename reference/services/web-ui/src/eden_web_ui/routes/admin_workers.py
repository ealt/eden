"""Admin-module routes for the worker registry — list / detail / register / reissue.

Implements plan §D.4 of phase 12a-1b. Mirrors the chunk-9e admin
module shape: server-side Jinja, no JS required, ``itsdangerous``-
signed session cookies, per-session CSRF, closed-allowlist banner
copy. Auth-first POST discipline: ``get_session(request)`` runs
before ``csrf_ok`` on every mutating route.

Worker-registry write paths (register / reissue) require an admin
bearer; the route layer pulls a ``StoreClient`` bearing
``admin:<admin_token>`` from ``app.state.admin_store``. When that
is ``None`` (postures B / C from plan §D.3) the templates render
write controls disabled and POSTs short-circuit with
``?error=admin-disabled``. Read paths use ``app.state.store``
(worker bearer), which is sufficient for the either-gated
``list_workers`` / ``read_worker`` endpoints (chapter 07 §6.1).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from eden_contracts import Event, Idea, Task, Variant, Worker
from eden_storage.errors import (
    InvalidPrecondition,
    ReservedIdentifier,
)
from eden_storage.errors import (
    NotFound as StorageNotFound,
)
from eden_wire.errors import BadRequest
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ._helpers import csrf_ok, get_session

router = APIRouter(prefix="/admin/workers")


# Worker / group identifier grammar per spec/v0/02-data-model.md §6.1.
_WORKER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_RESERVED_IDENTIFIERS = frozenset({"admin", "system", "internal"})

# Per-render cap on the worker-detail attribution view; matches the
# chunk-9e _TRIAL_DETAIL_EVENT_CAP convention.
_ATTRIBUTION_CAP = 50

# Per-render cap on label rendering width (used in list view).
_LABEL_PREVIEW_MAX = 120

# Banner-key allowlist (plan §D.6). Unknown keys render no banner.
_WORKER_OUTCOMES: dict[str, tuple[str, str]] = {
    "ok": ("ok", "worker registered — token shown below (only time)"),
    "idempotent": ("warn", "worker already existed; no new token issued"),
    "reissued": ("ok", "credential reissued — token shown below (only time)"),
    "reserved-identifier": ("error", "this identifier is reserved"),
    "invalid-worker-id": (
        "error",
        "worker_id must match [a-z0-9][a-z0-9_-]{0,63}",
    ),
    "invalid-labels": (
        "error",
        "label parse error — one `key=value` per line",
    ),
    "admin-disabled": (
        "error",
        "admin token not configured; registration unavailable",
    ),
    "not-found": ("error", "worker does not exist"),
    "transport": (
        "error",
        "transport or server error; refresh to retry",
    ),
}


def _outcome(
    request: Request, table: dict[str, tuple[str, str]]
) -> dict[str, str] | None:
    """Resolve the banner from ``?ok=…`` / ``?warn=…`` / ``?error=…``.

    Closed allowlist — unknown keys yield no banner. The querystring
    parameter names map onto the rendered banner levels so a single
    success / warn / error keyword path is uniform across this module.
    """
    for param in ("ok", "warn", "error"):
        key = request.query_params.get(param)
        if key is None:
            continue
        pair = table.get(key)
        if pair is None:
            return None
        level, message = pair
        return {"level": level, "message": message}
    return None


def _parse_labels(raw: str) -> dict[str, str]:
    """Parse multi-line ``key=value`` form input into a label dict.

    Blank lines and ``#``-prefixed lines are skipped. Each remaining
    line MUST contain ``=``; key non-empty; key ≤64 chars; value
    ≤256 chars. Raises ``ValueError`` with a 1-indexed line number
    on bad input.
    """
    labels: dict[str, str] = {}
    for n, line in enumerate(raw.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            msg = f"line {n}: missing `=`"
            raise ValueError(msg)
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            msg = f"line {n}: empty key"
            raise ValueError(msg)
        if len(key) > 64:
            msg = f"line {n}: key longer than 64 chars"
            raise ValueError(msg)
        if len(value) > 256:
            msg = f"line {n}: value longer than 256 chars"
            raise ValueError(msg)
        labels[key] = value
    return labels


def _validate_worker_id(value: str) -> str | None:
    """Return a banner-key name if ``value`` is invalid, else ``None``."""
    if value in _RESERVED_IDENTIFIERS:
        return "reserved-identifier"
    if not _WORKER_ID_RE.match(value):
        return "invalid-worker-id"
    return None


def _labels_preview(labels: dict[str, str] | None) -> str:
    if not labels:
        return ""
    joined = ", ".join(f"{k}={v}" for k, v in sorted(labels.items()))
    if len(joined) > _LABEL_PREVIEW_MAX:
        return joined[: _LABEL_PREVIEW_MAX - 1] + "…"
    return joined


def _csrf_failure_response() -> HTMLResponse:
    return HTMLResponse(content="CSRF token missing or invalid", status_code=403)


def _read_failure_response(request: Request, message: str) -> HTMLResponse:
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


def _filter_match(needle: str, *haystacks: str) -> bool:
    needle = needle.lower()
    return any(needle in hay.lower() for hay in haystacks)


def _filter_workers(
    workers: Iterable[Worker], q: str | None
) -> list[Worker]:
    if not q:
        return list(workers)
    out: list[Worker] = []
    for w in workers:
        labels = " ".join(
            f"{k}={v}" for k, v in (w.labels or {}).items()
        )
        if _filter_match(q, w.worker_id, labels):
            out.append(w)
    return out


def _groups_containing(
    store: Any, worker_id: str, *, all_groups: Iterable[Any] | None = None
) -> list[str]:
    """Return ``group_id``s whose transitive membership includes ``worker_id``.

    The transitive walk happens server-side over the wire via
    ``resolve_worker_in_group`` (one call per group). Bounded by the
    number of groups in the experiment; per plan §3.5 reference
    deployments stay under ≤10 groups so this is fine.
    """
    groups_iter = (
        store.list_groups() if all_groups is None else all_groups
    )
    out: list[str] = []
    for g in groups_iter:
        try:
            if store.resolve_worker_in_group(worker_id, g.group_id):
                out.append(g.group_id)
        except Exception:  # noqa: BLE001 — transport-shaped
            # Skip on transport blip rather than 500ing the page;
            # the operator can refresh.
            continue
    return out


# ---------------------------------------------------------------------
# Worker filter applied across modules — also consumed by admin.py
# ---------------------------------------------------------------------


def coerce_worker_filter(raw: str | None) -> str | None:
    """Return the worker filter value or ``"__invalid__"`` (sentinel) or ``None``.

    Used by the new ``/admin/workers/`` list-filter input AND by the
    chunk-12a-1b extension to ``/admin/tasks/?worker=…`` /
    ``/admin/variants/?worker=…`` (plan §D.8). The sentinel is the
    same shape chunk-9e uses for invalid kind/state values.
    """
    if raw is None or raw == "" or raw == "*":
        return None
    if not _WORKER_ID_RE.match(raw):
        return "__invalid__"
    return raw


WORKER_FILTER_INVALID = "__invalid__"


# ---------------------------------------------------------------------
# Routes — list + register
# ---------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse, response_model=None)
async def workers_index(request: Request) -> HTMLResponse | RedirectResponse:
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    store = request.app.state.store
    admin_store = request.app.state.admin_store

    q_raw = request.query_params.get("q") or ""
    q = q_raw[:64].lower() if q_raw else None

    try:
        workers = store.list_workers()
        all_groups = store.list_groups()
    except Exception:  # noqa: BLE001 — transport/store-domain
        return _read_failure_response(request, "could not load workers")

    filtered = _filter_workers(workers, q)
    rows: list[dict[str, Any]] = []
    for w in filtered:
        groups = _groups_containing(store, w.worker_id, all_groups=all_groups)
        rows.append(
            {
                "worker_id": w.worker_id,
                "registered_at": w.registered_at,
                "registered_by": w.registered_by,
                "labels_preview": _labels_preview(w.labels),
                "groups_preview": groups[:3],
                "groups_more": max(0, len(groups) - 3),
            }
        )

    return request.app.state.templates.TemplateResponse(
        request,
        "admin_workers.html",
        {
            "session": session,
            "csrf_token": session.csrf,
            "rows": rows,
            "q": q_raw,
            "admin_enabled": admin_store is not None,
            "outcome": _outcome(request, _WORKER_OUTCOMES),
        },
    )


@router.post("/", response_model=None)
async def workers_register(
    request: Request,
    csrf_token: str = Form(""),
    worker_id: str = Form(""),
    labels: str = Form(""),
) -> RedirectResponse | HTMLResponse:
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    if not csrf_ok(session, csrf_token):
        return _csrf_failure_response()
    admin_store = request.app.state.admin_store
    if admin_store is None:
        return RedirectResponse(
            url="/admin/workers/?error=admin-disabled", status_code=303
        )

    bad = _validate_worker_id(worker_id)
    if bad is not None:
        return RedirectResponse(
            url=f"/admin/workers/?error={bad}", status_code=303
        )

    try:
        parsed_labels = _parse_labels(labels)
    except ValueError:
        return RedirectResponse(
            url="/admin/workers/?error=invalid-labels", status_code=303
        )

    try:
        worker, token = admin_store.register_worker(
            worker_id, labels=parsed_labels or None
        )
    except ReservedIdentifier:
        return RedirectResponse(
            url="/admin/workers/?error=reserved-identifier", status_code=303
        )
    except (BadRequest, InvalidPrecondition):
        return RedirectResponse(
            url="/admin/workers/?error=invalid-worker-id", status_code=303
        )
    except Exception:  # noqa: BLE001 — transport-shaped
        return RedirectResponse(
            url="/admin/workers/?error=transport", status_code=303
        )

    return _render_token_page(
        request,
        session,
        worker=worker,
        token=token,
        action="register",
    )


# ---------------------------------------------------------------------
# Routes — detail + reissue
# ---------------------------------------------------------------------


@router.get("/{worker_id}/", response_class=HTMLResponse, response_model=None)
async def worker_detail(
    worker_id: str, request: Request
) -> HTMLResponse | RedirectResponse:
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    store = request.app.state.store
    admin_store = request.app.state.admin_store

    try:
        worker = store.read_worker(worker_id)
        all_groups = store.list_groups()
        tasks = store.list_tasks()
        ideas = store.list_ideas()
        variants = store.list_variants()
        events_full = store.replay()
    except StorageNotFound:
        raise
    except Exception:  # noqa: BLE001 — transport/store-domain
        return _read_failure_response(request, "could not load worker")

    groups_of_worker = _groups_containing(
        store, worker_id, all_groups=all_groups
    )

    attributed_tasks = _attribution_tasks(tasks, worker_id)
    attributed_ideas = _attribution_ideas(ideas, worker_id)
    attributed_variants = _attribution_variants(variants, worker_id)
    related_events = _attribution_events(events_full, worker_id)

    return request.app.state.templates.TemplateResponse(
        request,
        "admin_worker_detail.html",
        {
            "session": session,
            "csrf_token": session.csrf,
            "worker": worker,
            "labels_preview": _labels_preview(worker.labels),
            "groups": groups_of_worker,
            "tasks": attributed_tasks,
            "ideas": attributed_ideas,
            "variants": attributed_variants,
            "events": related_events,
            "admin_enabled": admin_store is not None,
            "outcome": _outcome(request, _WORKER_OUTCOMES),
        },
    )


@router.post("/{worker_id}/reissue-credential", response_model=None)
async def worker_reissue(
    worker_id: str,
    request: Request,
    csrf_token: str = Form(""),
) -> RedirectResponse | HTMLResponse:
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    if not csrf_ok(session, csrf_token):
        return _csrf_failure_response()
    admin_store = request.app.state.admin_store
    if admin_store is None:
        return RedirectResponse(
            url=f"/admin/workers/{worker_id}/?error=admin-disabled",
            status_code=303,
        )

    try:
        token = admin_store.reissue_credential(worker_id)
        worker = admin_store.read_worker(worker_id)
    except StorageNotFound:
        return RedirectResponse(
            url="/admin/workers/?error=not-found", status_code=303
        )
    except Exception:  # noqa: BLE001 — transport-shaped
        return RedirectResponse(
            url=f"/admin/workers/{worker_id}/?error=transport",
            status_code=303,
        )

    return _render_token_page(
        request,
        session,
        worker=worker,
        token=token,
        action="reissue",
    )


# ---------------------------------------------------------------------
# Helpers — attribution
# ---------------------------------------------------------------------


def _attribution_tasks(
    tasks: Iterable[Task], worker_id: str
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in tasks:
        claim_worker = t.claim.worker_id if t.claim is not None else None
        if (
            claim_worker == worker_id
            or t.submitted_by == worker_id
            or t.created_by == worker_id
        ):
            out.append(
                {
                    "task_id": t.task_id,
                    "kind": t.kind,
                    "state": t.state,
                    "claim_worker": claim_worker,
                    "submitted_by": t.submitted_by,
                    "created_by": t.created_by,
                    "updated_at": t.updated_at,
                }
            )
    out.sort(key=lambda r: r["updated_at"] or "", reverse=True)
    return out[:_ATTRIBUTION_CAP]


def _attribution_ideas(
    ideas: Iterable[Idea], worker_id: str
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i in ideas:
        if i.created_by == worker_id:
            out.append(
                {
                    "idea_id": i.idea_id,
                    "slug": i.slug,
                    "state": i.state,
                    "created_at": i.created_at,
                }
            )
    out.sort(key=lambda r: r["created_at"] or "", reverse=True)
    return out[:_ATTRIBUTION_CAP]


def _attribution_variants(
    variants: Iterable[Variant], worker_id: str
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for v in variants:
        if v.executed_by == worker_id or v.evaluated_by == worker_id:
            out.append(
                {
                    "variant_id": v.variant_id,
                    "idea_id": v.idea_id,
                    "status": v.status,
                    "executed_by": v.executed_by,
                    "evaluated_by": v.evaluated_by,
                    "started_at": v.started_at,
                    "completed_at": v.completed_at,
                }
            )
    out.sort(
        key=lambda r: r["completed_at"] or r["started_at"] or "",
        reverse=True,
    )
    return out[:_ATTRIBUTION_CAP]


_ATTRIBUTION_KEYS = (
    "worker_id",
    "claimant_worker_id",
    "created_by",
    "submitted_by",
    "executed_by",
    "evaluated_by",
)


def _attribution_events(
    events: Iterable[Event], worker_id: str
) -> list[Event]:
    related: list[Event] = []
    for ev in events:
        data = ev.data or {}
        for key in _ATTRIBUTION_KEYS:
            if data.get(key) == worker_id:
                related.append(ev)
                break
    return list(reversed(related))[:_ATTRIBUTION_CAP]


# ---------------------------------------------------------------------
# Helpers — token-page render
# ---------------------------------------------------------------------


def _render_token_page(
    request: Request,
    session: Any,
    *,
    worker: Worker,
    token: str | None,
    action: str,
) -> HTMLResponse:
    """Render the one-shot ``admin_worker_token.html`` page.

    ``action="register"`` distinguishes the fresh-register banner
    ("token shown below (only time)") from the idempotent re-register
    banner ("worker already existed; no new token was issued"). For
    ``action="reissue"`` the page always carries a token (the wire is
    non-idempotent for reissue per spec §6.3); the banner emphasizes
    "the previous credential is now invalid".

    The HTTP response carries ``Cache-Control: no-store`` so that
    browsers don't aggressively cache the rendered token (plan §8.2).
    """
    response = request.app.state.templates.TemplateResponse(
        request,
        "admin_worker_token.html",
        {
            "session": session,
            "worker": worker,
            "labels_preview": _labels_preview(worker.labels),
            "token": token,
            "action": action,
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


__all__ = [
    "WORKER_FILTER_INVALID",
    "coerce_worker_filter",
    "router",
]
