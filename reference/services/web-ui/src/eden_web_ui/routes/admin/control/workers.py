"""Deployment-scoped worker registry admin routes (chapter 11 §6).

Mirrors the per-experiment `/admin/workers/` module shape (server-
side Jinja, auth-first POST, closed-allowlist banners) but operates
against the deployment-level registry via
`app.state.control_plane`. Issue #146.

The control-plane client carries the admin bearer at startup
(`cli._build_control_plane_client`), so admin-gated reads + writes
go through the same client; there is no separate `admin_store`
posture here. When `control_plane` is unset, the parent
`admin/control/` router is not registered and these routes return
404 — matching the `/admin/experiments/` posture.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from eden_contracts import Group, Worker
from eden_control_plane import ControlPlaneClient
from eden_storage.errors import (
    AlreadyExists,
    InvalidPrecondition,
    ReservedIdentifier,
)
from eden_storage.errors import (
    NotFound as StorageNotFound,
)
from eden_wire.errors import BadRequest
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..._helpers import csrf_ok, get_session

router = APIRouter(prefix="/admin/control/workers")


# Worker / group identifier grammar per spec/v0/02-data-model.md §6.1.
_WORKER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_RESERVED_IDENTIFIERS = frozenset({"admin", "system", "internal"})

# Per-render cap on label rendering width (used in list view).
_LABEL_PREVIEW_MAX = 120


_WORKER_OUTCOMES: dict[str, tuple[str, str]] = {
    "ok": ("ok", "worker registered — token shown below (only time)"),
    "idempotent": ("warn", "worker already existed; no new token issued"),
    "reissued": ("ok", "credential reissued — token shown below (only time)"),
    "reserved-identifier": ("error", "this identifier is reserved"),
    "id-collides-with-group": (
        "error",
        "a group already has this identifier; worker_ids and "
        "group_ids share a namespace per spec ch02 §7.1",
    ),
    "invalid-worker-id": (
        "error",
        "worker_id must match [a-z0-9][a-z0-9_-]{0,63}",
    ),
    "invalid-labels": (
        "error",
        "label parse error — one `key=value` per line",
    ),
    "invalid-labels-line": (
        "error",
        "label parse error on a numbered line (see ?line=N)",
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
    """Resolve the banner from ``?ok=…`` / ``?warn=…`` / ``?error=…``."""
    for param in ("ok", "warn", "error"):
        key = request.query_params.get(param)
        if key is None:
            continue
        pair = table.get(key)
        if pair is None:
            return None
        level, message = pair
        if key == "invalid-labels-line":
            raw_line = request.query_params.get("line", "")
            if raw_line.isdigit() and len(raw_line) <= 5:
                message = f"label parse error on line {raw_line}"
        return {"level": level, "message": message}
    return None


class _LabelParseError(Exception):
    def __init__(self, line: int, reason: str) -> None:
        super().__init__(f"line {line}: {reason}")
        self.line = line
        self.reason = reason


def _parse_labels(raw: str) -> dict[str, str]:
    labels: dict[str, str] = {}
    for n, line in enumerate(raw.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise _LabelParseError(n, "missing `=`")
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            raise _LabelParseError(n, "empty key")
        if len(key) > 64:
            raise _LabelParseError(n, "key longer than 64 chars")
        if len(value) > 256:
            raise _LabelParseError(n, "value longer than 256 chars")
        labels[key] = value
    return labels


def _validate_worker_id(value: str) -> str | None:
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
                "check the control-plane server logs."
            ),
        },
        status_code=502,
    )


def _control_plane(request: Request) -> ControlPlaneClient:
    cp: ControlPlaneClient | None = request.app.state.control_plane
    assert cp is not None, "admin/control/workers registered without control_plane"
    return cp


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


def _group_contains_worker(
    cp: ControlPlaneClient,
    group: Group,
    target_worker_id: str,
    *,
    visited: set[str] | None = None,
    depth: int = 0,
    depth_cap: int = 10,
) -> bool:
    """Return True iff ``target_worker_id`` is in ``group``'s transitive closure.

    Walks the group DAG via ``cp.read_group`` for nested groups. The
    control plane lacks an O(1) `resolve_worker_in_group` probe so we
    walk explicitly; depth is bounded to prevent cycles in malformed
    registries (spec §7.1 forbids cycles at write time).
    """
    if visited is None:
        visited = set()
    if group.group_id in visited or depth > depth_cap:
        return False
    visited.add(group.group_id)
    for member in group.members:
        if member == target_worker_id:
            return True
        try:
            sub = cp.read_group(member)
        except StorageNotFound:
            continue
        except Exception:  # noqa: BLE001 — transport-shaped
            continue
        if _group_contains_worker(
            cp,
            sub,
            target_worker_id,
            visited=visited,
            depth=depth + 1,
            depth_cap=depth_cap,
        ):
            return True
    return False


def _groups_containing(
    cp: ControlPlaneClient,
    worker_id: str,
    *,
    all_groups: Iterable[Group] | None = None,
) -> tuple[list[str], int]:
    """Return ``(group_ids, transport_errors)`` for the worker's memberships.

    The control plane has no `resolve_worker_in_group` op, so we walk
    each group's closure ourselves (membership rooted at a group, not
    at the worker). Bounded by the deployment's group count.
    """
    try:
        groups_iter = (
            cp.list_groups() if all_groups is None else all_groups
        )
    except Exception:  # noqa: BLE001 — transport-shaped
        return ([], 1)
    out: list[str] = []
    transport_errors = 0
    for g in groups_iter:
        try:
            if _group_contains_worker(cp, g, worker_id):
                out.append(g.group_id)
        except Exception:  # noqa: BLE001 — transport-shaped
            transport_errors += 1
            continue
    return (out, transport_errors)


# ---------------------------------------------------------------------
# Routes — list + register
# ---------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse, response_model=None)
async def workers_index(request: Request) -> HTMLResponse | RedirectResponse:
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    cp = _control_plane(request)

    q_raw = request.query_params.get("q") or ""
    q = q_raw[:64].lower() if q_raw else None

    try:
        workers = cp.list_workers()
        all_groups = cp.list_groups()
    except Exception:  # noqa: BLE001 — transport-shaped
        return _read_failure_response(request, "could not load workers")

    filtered = _filter_workers(workers, q)
    rows: list[dict[str, Any]] = []
    total_transport_errors = 0
    for w in filtered:
        groups, te = _groups_containing(
            cp, w.worker_id, all_groups=all_groups
        )
        total_transport_errors += te
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
        "admin_control_workers.html",
        {
            "session": session,
            "csrf_token": session.csrf,
            "rows": rows,
            "q": q_raw,
            "membership_transport_errors": total_transport_errors,
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
    cp = _control_plane(request)

    bad = _validate_worker_id(worker_id)
    if bad is not None:
        return RedirectResponse(
            url=f"/admin/control/workers/?error={bad}", status_code=303
        )

    try:
        parsed_labels = _parse_labels(labels)
    except _LabelParseError as exc:
        return RedirectResponse(
            url=(
                f"/admin/control/workers/?error=invalid-labels-line"
                f"&line={exc.line}"
            ),
            status_code=303,
        )

    try:
        raw = cp.register_worker(worker_id, labels=parsed_labels or None)
    except ReservedIdentifier:
        return RedirectResponse(
            url="/admin/control/workers/?error=reserved-identifier",
            status_code=303,
        )
    except AlreadyExists:
        return RedirectResponse(
            url="/admin/control/workers/?error=id-collides-with-group",
            status_code=303,
        )
    except (BadRequest, InvalidPrecondition):
        return RedirectResponse(
            url="/admin/control/workers/?error=invalid-worker-id",
            status_code=303,
        )
    except Exception:  # noqa: BLE001 — transport-shaped
        return RedirectResponse(
            url="/admin/control/workers/?error=transport", status_code=303
        )

    token = raw.get("registration_token")
    worker = Worker.model_validate({k: v for k, v in raw.items() if k != "registration_token"})
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
    cp = _control_plane(request)

    try:
        worker = cp.read_worker(worker_id)
        all_groups = cp.list_groups()
    except StorageNotFound:
        raise
    except Exception:  # noqa: BLE001 — transport-shaped
        return _read_failure_response(request, "could not load worker")

    groups_of_worker, membership_transport_errors = _groups_containing(
        cp, worker_id, all_groups=all_groups
    )

    return request.app.state.templates.TemplateResponse(
        request,
        "admin_control_worker_detail.html",
        {
            "session": session,
            "csrf_token": session.csrf,
            "worker": worker,
            "labels_preview": _labels_preview(worker.labels),
            "groups": groups_of_worker,
            "membership_transport_errors": membership_transport_errors,
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
    cp = _control_plane(request)

    try:
        raw = cp.reissue_credential(worker_id)
    except StorageNotFound:
        return RedirectResponse(
            url="/admin/control/workers/?error=not-found", status_code=303
        )
    except Exception:  # noqa: BLE001 — transport-shaped
        return RedirectResponse(
            url=f"/admin/control/workers/{worker_id}/?error=transport",
            status_code=303,
        )

    token = raw.get("registration_token")
    worker = Worker.model_validate({k: v for k, v in raw.items() if k != "registration_token"})
    return _render_token_page(
        request,
        session,
        worker=worker,
        token=token,
        action="reissue",
    )


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
    response = request.app.state.templates.TemplateResponse(
        request,
        "admin_control_worker_token.html",
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


__all__ = ["router"]
