"""Deployment-scoped group registry admin routes (chapter 11 §6).

Mirrors the per-experiment `/admin/groups/` module shape (server-
side Jinja, auth-first POST, closed-allowlist banners) but operates
against the deployment-level registry via
`app.state.control_plane`. Issue #146.

Reuses `eden_web_ui.routes.admin_groups.walk_transitive_workers`
for the transitive-membership view — the walker is data-source-
agnostic (it only needs `read_group` + `read_worker`, both of which
`ControlPlaneClient` exposes).
"""

from __future__ import annotations

import re
from typing import Any

from eden_control_plane import ControlPlaneClient
from eden_storage.errors import (
    AlreadyExists,
    CycleDetected,
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
from ...admin_groups import walk_transitive_workers

router = APIRouter(prefix="/admin/control/groups")


# Spec §6.1 / §7.1 grammar (shared with workers).
_GROUP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_RESERVED_IDENTIFIERS = frozenset({"admin", "system", "internal"})


_GROUP_OUTCOMES: dict[str, tuple[str, str]] = {
    "registered": ("ok", "group registered"),
    "added": ("ok", "member added"),
    "removed": (
        "ok",
        "member removed (idempotent if already absent)",
    ),
    "deleted": ("ok", "group deleted"),
    "cycle-detected": (
        "error",
        "adding this member would create a cycle",
    ),
    "group-not-found": ("error", "group no longer exists"),
    "reserved-identifier": (
        "error",
        "identifier is reserved (admin / system / internal)",
    ),
    "reserved-member-id": (
        "error",
        "member_id is reserved (admin / system / internal)",
    ),
    "invalid-group-id": (
        "error",
        "group_id must match [a-z0-9][a-z0-9_-]{0,63}",
    ),
    "invalid-member-id": (
        "error",
        "member_id must match [a-z0-9][a-z0-9_-]{0,63}",
    ),
    "invalid-members": (
        "error",
        "one of the initial members failed validation",
    ),
    "already-exists": ("error", "a group with this id already exists"),
    "id-collides-with-worker": (
        "error",
        "a worker already has this identifier; worker_ids and "
        "group_ids share a namespace per spec ch02 §7.1",
    ),
    "transport": (
        "error",
        "transport or server error; refresh to retry",
    ),
}


def _outcome(
    request: Request, table: dict[str, tuple[str, str]]
) -> dict[str, str] | None:
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


def _validate_group_id(value: str) -> str | None:
    if value in _RESERVED_IDENTIFIERS:
        return "reserved-identifier"
    if not _GROUP_ID_RE.match(value):
        return "invalid-group-id"
    return None


def _validate_member_id(value: str) -> str | None:
    if value in _RESERVED_IDENTIFIERS:
        return "reserved-member-id"
    if not _GROUP_ID_RE.match(value):
        return "invalid-member-id"
    return None


def _parse_member_lines(raw: str) -> tuple[list[str], str | None]:
    members: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        bad = _validate_member_id(stripped)
        if bad is not None:
            return ([], "invalid-members")
        members.append(stripped)
    return (members, None)


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
    assert cp is not None, "admin/control/groups registered without control_plane"
    return cp


# ---------------------------------------------------------------------
# Routes — list + register
# ---------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse, response_model=None)
async def groups_index(request: Request) -> HTMLResponse | RedirectResponse:
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    cp = _control_plane(request)

    q_raw = request.query_params.get("q") or ""
    q = q_raw[:64].lower() if q_raw else None

    try:
        groups = cp.list_groups()
    except Exception:  # noqa: BLE001 — transport-shaped
        return _read_failure_response(request, "could not load groups")

    rows: list[dict[str, Any]] = []
    total_transport_errors = 0
    any_truncated = False
    for g in groups:
        if q and q not in g.group_id.lower():
            continue
        walk = walk_transitive_workers(cp, g.group_id)
        total_transport_errors += walk["transport_errors"]
        if walk["truncated_breadth"] or walk["truncated_depth"]:
            any_truncated = True
        transitive_worker_count = len(walk["workers"])
        transitive_label = (
            f"≥{transitive_worker_count}"
            if (walk["truncated_breadth"] or walk["truncated_depth"])
            else str(transitive_worker_count)
        )
        rows.append(
            {
                "group_id": g.group_id,
                "created_at": g.created_at,
                "created_by": g.created_by,
                "member_count": len(g.members),
                "transitive_worker_count": transitive_worker_count,
                "transitive_worker_label": transitive_label,
                "members_preview": list(g.members[:3]),
                "members_more": max(0, len(g.members) - 3),
            }
        )

    return request.app.state.templates.TemplateResponse(
        request,
        "admin_control_groups.html",
        {
            "session": session,
            "csrf_token": session.csrf,
            "rows": rows,
            "q": q_raw,
            "membership_transport_errors": total_transport_errors,
            "any_truncated": any_truncated,
            "outcome": _outcome(request, _GROUP_OUTCOMES),
        },
    )


@router.post("/", response_model=None)
async def groups_register(
    request: Request,
    csrf_token: str = Form(""),
    group_id: str = Form(""),
    members: str = Form(""),
) -> RedirectResponse | HTMLResponse:
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    if not csrf_ok(session, csrf_token):
        return _csrf_failure_response()
    cp = _control_plane(request)

    bad = _validate_group_id(group_id)
    if bad is not None:
        return RedirectResponse(
            url=f"/admin/control/groups/?error={bad}", status_code=303
        )

    initial_members, bad_members = _parse_member_lines(members)
    if bad_members is not None:
        return RedirectResponse(
            url="/admin/control/groups/?error=invalid-members",
            status_code=303,
        )

    # Pre-flight worker-collision check so the AlreadyExists banner
    # below distinguishes "group with this id exists" from the
    # cross-registry collision per spec ch02 §7.1.
    worker_collision = False
    try:
        cp.read_worker(group_id)
        worker_collision = True
    except StorageNotFound:
        pass
    except Exception:  # noqa: BLE001 — transport-shaped
        pass
    try:
        cp.register_group(group_id, members=initial_members or None)
    except ReservedIdentifier:
        return RedirectResponse(
            url="/admin/control/groups/?error=reserved-identifier",
            status_code=303,
        )
    except CycleDetected:
        return RedirectResponse(
            url="/admin/control/groups/?error=cycle-detected",
            status_code=303,
        )
    except AlreadyExists:
        if worker_collision:
            return RedirectResponse(
                url="/admin/control/groups/?error=id-collides-with-worker",
                status_code=303,
            )
        return RedirectResponse(
            url="/admin/control/groups/?error=already-exists",
            status_code=303,
        )
    except (BadRequest, InvalidPrecondition):
        return RedirectResponse(
            url="/admin/control/groups/?error=invalid-group-id",
            status_code=303,
        )
    except Exception:  # noqa: BLE001 — transport-shaped
        return RedirectResponse(
            url="/admin/control/groups/?error=transport", status_code=303
        )

    return RedirectResponse(
        url=f"/admin/control/groups/{group_id}/?ok=registered",
        status_code=303,
    )


# ---------------------------------------------------------------------
# Routes — detail
# ---------------------------------------------------------------------


@router.get("/{group_id}/", response_class=HTMLResponse, response_model=None)
async def group_detail(
    group_id: str, request: Request
) -> HTMLResponse | RedirectResponse:
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    cp = _control_plane(request)

    try:
        group = cp.read_group(group_id)
    except StorageNotFound:
        raise
    except Exception:  # noqa: BLE001 — transport-shaped
        return _read_failure_response(request, "could not load group")

    try:
        walk = walk_transitive_workers(cp, group_id)
    except Exception:  # noqa: BLE001 — transport-shaped
        return _read_failure_response(
            request, "could not walk group membership"
        )

    return request.app.state.templates.TemplateResponse(
        request,
        "admin_control_group_detail.html",
        {
            "session": session,
            "csrf_token": session.csrf,
            "group": group,
            "transitive": walk,
            "outcome": _outcome(request, _GROUP_OUTCOMES),
        },
    )


# ---------------------------------------------------------------------
# Routes — mutate
# ---------------------------------------------------------------------


@router.post("/{group_id}/members", response_model=None)
async def group_add_member(
    group_id: str,
    request: Request,
    csrf_token: str = Form(""),
    member_id: str = Form(""),
) -> RedirectResponse | HTMLResponse:
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    if not csrf_ok(session, csrf_token):
        return _csrf_failure_response()
    cp = _control_plane(request)

    bad = _validate_member_id(member_id)
    if bad is not None:
        return RedirectResponse(
            url=f"/admin/control/groups/{group_id}/?error={bad}",
            status_code=303,
        )

    try:
        cp.add_to_group(group_id, member_id)
    except CycleDetected:
        return RedirectResponse(
            url=f"/admin/control/groups/{group_id}/?error=cycle-detected",
            status_code=303,
        )
    except StorageNotFound:
        return RedirectResponse(
            url="/admin/control/groups/?error=group-not-found",
            status_code=303,
        )
    except ReservedIdentifier:
        return RedirectResponse(
            url=f"/admin/control/groups/{group_id}/?error=reserved-member-id",
            status_code=303,
        )
    except (BadRequest, InvalidPrecondition):
        return RedirectResponse(
            url=f"/admin/control/groups/{group_id}/?error=invalid-member-id",
            status_code=303,
        )
    except Exception:  # noqa: BLE001 — transport-shaped
        return RedirectResponse(
            url=f"/admin/control/groups/{group_id}/?error=transport",
            status_code=303,
        )

    return RedirectResponse(
        url=f"/admin/control/groups/{group_id}/?ok=added", status_code=303
    )


@router.post(
    "/{group_id}/members/{member_id}/remove", response_model=None
)
async def group_remove_member(
    group_id: str,
    member_id: str,
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
        cp.remove_from_group(group_id, member_id)
    except StorageNotFound:
        return RedirectResponse(
            url="/admin/control/groups/?error=group-not-found",
            status_code=303,
        )
    except Exception:  # noqa: BLE001 — transport-shaped
        return RedirectResponse(
            url=f"/admin/control/groups/{group_id}/?error=transport",
            status_code=303,
        )

    return RedirectResponse(
        url=f"/admin/control/groups/{group_id}/?ok=removed",
        status_code=303,
    )


@router.post("/{group_id}/delete", response_model=None)
async def group_delete(
    group_id: str,
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
        cp.delete_group(group_id)
    except StorageNotFound:
        return RedirectResponse(
            url="/admin/control/groups/?error=group-not-found",
            status_code=303,
        )
    except Exception:  # noqa: BLE001 — transport-shaped
        return RedirectResponse(
            url=f"/admin/control/groups/{group_id}/?error=transport",
            status_code=303,
        )

    return RedirectResponse(
        url="/admin/control/groups/?ok=deleted", status_code=303
    )


__all__ = ["router"]
