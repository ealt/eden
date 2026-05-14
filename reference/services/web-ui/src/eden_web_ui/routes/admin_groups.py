"""Admin-module routes for the group registry — list / detail / register / mutate / delete.

Implements plan §D.5 of phase 12a-1b. Mirrors the chunk-9e admin
module shape (server-side Jinja, auth-first POST, closed-allowlist
banners). Admin-gated writes route through ``app.state.admin_store``;
reads use ``app.state.store`` (either-gated wire endpoints accept
the worker bearer).

The transitive-membership view is walked client-side via repeated
``read_group`` calls — ``StoreClient`` exposes ``resolve_worker_in_group``
as a yes/no probe but no "list transitive workers" op. Plan §3.5 caps
the walk at depth ≤10, breadth ≤1000.
"""

from __future__ import annotations

import re
from typing import Any

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

from ._helpers import csrf_ok, get_session

router = APIRouter(prefix="/admin/groups")


# Spec §6.1 / §7.1 grammar (shared with workers).
_GROUP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_RESERVED_IDENTIFIERS = frozenset({"admin", "system", "internal"})

# Plan §3.5 walk caps. Constants exposed at module level so tests
# can monkeypatch them when constructing pathological graphs.
GROUP_WALK_DEPTH_CAP = 10
GROUP_WALK_BREADTH_CAP = 1000


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
    "admin-disabled": (
        "error",
        "admin token not configured; mutation unavailable",
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
    """Parse a multi-line `member_id` form input.

    Returns ``(members, banner_key)``. ``banner_key`` is non-``None``
    if one of the lines failed grammar / reserved validation.
    """
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
                "check the task-store-server logs."
            ),
        },
        status_code=502,
    )


# ---------------------------------------------------------------------
# Transitive-membership walk
# ---------------------------------------------------------------------


def walk_transitive_workers(
    store: Any,
    group_id: str,
    *,
    depth_cap: int = GROUP_WALK_DEPTH_CAP,
    breadth_cap: int = GROUP_WALK_BREADTH_CAP,
) -> dict[str, Any]:
    """DFS the group DAG and return reachable workers.

    Returns a dict with keys:
    - ``workers``: ``[{"worker_id": ..., "via": [group_id, ...]}]`` —
      the first path discovered for each worker, capped by
      ``depth_cap`` and ``breadth_cap``.
    - ``truncated_depth``: bool — at least one branch hit the depth cap.
    - ``truncated_breadth``: bool — the breadth cap stopped the walk.
    - ``visited_groups``: int — number of groups read during the walk.

    Cycle protection: the ``visited`` set guards against the (already
    spec-prevented at write-time) case of a group containing itself
    transitively, so the walk terminates even if the registry is
    malformed.
    """
    workers: dict[str, list[str]] = {}
    dangling: set[str] = set()
    visited_groups: set[str] = set()
    truncated_depth = False
    truncated_breadth = False
    transport_errors = 0

    # Stack entries: (group_id, path_so_far)
    stack: list[tuple[str, list[str]]] = [(group_id, [])]
    while stack:
        if len(workers) >= breadth_cap:
            truncated_breadth = True
            break
        gid, path = stack.pop()
        if gid in visited_groups:
            continue
        visited_groups.add(gid)
        if len(path) > depth_cap:
            truncated_depth = True
            continue
        try:
            group = store.read_group(gid)
        except StorageNotFound:
            # Dangling reference per spec §7.1 — resolves to
            # membership=false. Skip silently.
            continue
        except Exception:  # noqa: BLE001 — transport-shaped
            transport_errors += 1
            continue
        new_path = [*path, gid]
        for member in group.members:
            # Member may be a worker_id OR a group_id. Try the
            # group route first; if it 404s, probe the worker
            # registry to distinguish "registered worker" from
            # "dangling identifier" (per spec §7.1 a dangling
            # member resolves to membership=false; rendering it as
            # a member of the closure would mislead operators about
            # who can actually claim a group-targeted task).
            try:
                store.read_group(member)
            except StorageNotFound:
                # Not a group; check worker registry.
                try:
                    store.read_worker(member)
                except StorageNotFound:
                    # Dangling identifier — neither worker nor
                    # group. Record separately so the template can
                    # surface it as a "may want to clean up"
                    # advisory without claiming the identifier is
                    # actually a claimant.
                    dangling.add(member)
                    continue
                except Exception:  # noqa: BLE001 — transport-shaped
                    transport_errors += 1
                    continue
                # Registered worker: record first discovery path.
                if member not in workers:
                    if len(workers) >= breadth_cap:
                        truncated_breadth = True
                        break
                    workers[member] = new_path
                continue
            except Exception:  # noqa: BLE001 — transport-shaped
                transport_errors += 1
                continue
            # It's a group; recurse.
            stack.append((member, new_path))

    return {
        "workers": [
            {"worker_id": wid, "via": via}
            for wid, via in sorted(workers.items())
        ],
        "dangling": sorted(dangling),
        "truncated_depth": truncated_depth,
        "truncated_breadth": truncated_breadth,
        "transport_errors": transport_errors,
        "visited_groups": len(visited_groups),
    }


# ---------------------------------------------------------------------
# Routes — list + register
# ---------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse, response_model=None)
async def groups_index(request: Request) -> HTMLResponse | RedirectResponse:
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    store = request.app.state.store
    admin_store = request.app.state.admin_store

    q_raw = request.query_params.get("q") or ""
    q = q_raw[:64].lower() if q_raw else None

    try:
        groups = store.list_groups()
    except Exception:  # noqa: BLE001 — transport/store-domain
        return _read_failure_response(request, "could not load groups")

    rows: list[dict[str, Any]] = []
    total_transport_errors = 0
    any_truncated = False
    for g in groups:
        if q and q not in g.group_id.lower():
            continue
        walk = walk_transitive_workers(store, g.group_id)
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
        "admin_groups.html",
        {
            "session": session,
            "csrf_token": session.csrf,
            "rows": rows,
            "q": q_raw,
            "admin_enabled": admin_store is not None,
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
        return _csrf_failure_response_redirect()
    admin_store = request.app.state.admin_store
    if admin_store is None:
        return RedirectResponse(
            url="/admin/groups/?error=admin-disabled", status_code=303
        )

    bad = _validate_group_id(group_id)
    if bad is not None:
        return RedirectResponse(
            url=f"/admin/groups/?error={bad}", status_code=303
        )

    initial_members, bad_members = _parse_member_lines(members)
    if bad_members is not None:
        return RedirectResponse(
            url="/admin/groups/?error=invalid-members", status_code=303
        )

    # Pre-flight worker-collision check so the AlreadyExists banner
    # below distinguishes "group with this id exists" from the
    # cross-registry collision per spec ch02 §7.1. We check the worker
    # registry first; the store also enforces both (defense in
    # depth), and a wire-side StorageNotFound here is fine.
    worker_collision = False
    try:
        admin_store.read_worker(group_id)
        worker_collision = True
    except StorageNotFound:
        pass
    except Exception:  # noqa: BLE001 — transport-shaped
        # If the preflight read fails, let the wire's own collision
        # check be authoritative and fall through.
        pass
    try:
        admin_store.register_group(
            group_id, members=initial_members or None
        )
    except ReservedIdentifier:
        return RedirectResponse(
            url="/admin/groups/?error=reserved-identifier", status_code=303
        )
    except CycleDetected:
        return RedirectResponse(
            url="/admin/groups/?error=cycle-detected", status_code=303
        )
    except AlreadyExists:
        # Distinguish "a group with this id exists" from the
        # cross-registry collision (worker_ids / group_ids share a
        # namespace per spec ch02 §7.1). The store raises the same
        # `AlreadyExists` for both cases; we use the preflight
        # `worker_collision` flag to surface a clearer banner.
        if worker_collision:
            return RedirectResponse(
                url="/admin/groups/?error=id-collides-with-worker",
                status_code=303,
            )
        return RedirectResponse(
            url="/admin/groups/?error=already-exists", status_code=303
        )
    except (BadRequest, InvalidPrecondition):
        return RedirectResponse(
            url="/admin/groups/?error=invalid-group-id", status_code=303
        )
    except Exception:  # noqa: BLE001 — transport-shaped
        return RedirectResponse(
            url="/admin/groups/?error=transport", status_code=303
        )

    return RedirectResponse(
        url=f"/admin/groups/{group_id}/?ok=registered", status_code=303
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
    store = request.app.state.store
    admin_store = request.app.state.admin_store

    try:
        group = store.read_group(group_id)
    except StorageNotFound:
        raise
    except Exception:  # noqa: BLE001 — transport-shaped
        return _read_failure_response(request, "could not load group")

    try:
        walk = walk_transitive_workers(store, group_id)
    except Exception:  # noqa: BLE001
        return _read_failure_response(request, "could not walk group membership")

    return request.app.state.templates.TemplateResponse(
        request,
        "admin_group_detail.html",
        {
            "session": session,
            "csrf_token": session.csrf,
            "group": group,
            "transitive": walk,
            "admin_enabled": admin_store is not None,
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
        return _csrf_failure_response_redirect()
    admin_store = request.app.state.admin_store
    if admin_store is None:
        return RedirectResponse(
            url=f"/admin/groups/{group_id}/?error=admin-disabled",
            status_code=303,
        )

    bad = _validate_member_id(member_id)
    if bad is not None:
        return RedirectResponse(
            url=f"/admin/groups/{group_id}/?error={bad}", status_code=303
        )

    try:
        admin_store.add_to_group(group_id, member_id)
    except CycleDetected:
        return RedirectResponse(
            url=f"/admin/groups/{group_id}/?error=cycle-detected",
            status_code=303,
        )
    except StorageNotFound:
        return RedirectResponse(
            url="/admin/groups/?error=group-not-found", status_code=303
        )
    except ReservedIdentifier:
        return RedirectResponse(
            url=f"/admin/groups/{group_id}/?error=reserved-member-id",
            status_code=303,
        )
    except (BadRequest, InvalidPrecondition):
        return RedirectResponse(
            url=f"/admin/groups/{group_id}/?error=invalid-member-id",
            status_code=303,
        )
    except Exception:  # noqa: BLE001 — transport-shaped
        return RedirectResponse(
            url=f"/admin/groups/{group_id}/?error=transport",
            status_code=303,
        )

    return RedirectResponse(
        url=f"/admin/groups/{group_id}/?ok=added", status_code=303
    )


@router.post("/{group_id}/members/{member_id}/remove", response_model=None)
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
        return _csrf_failure_response_redirect()
    admin_store = request.app.state.admin_store
    if admin_store is None:
        return RedirectResponse(
            url=f"/admin/groups/{group_id}/?error=admin-disabled",
            status_code=303,
        )

    try:
        admin_store.remove_from_group(group_id, member_id)
    except StorageNotFound:
        return RedirectResponse(
            url="/admin/groups/?error=group-not-found", status_code=303
        )
    except Exception:  # noqa: BLE001 — transport-shaped
        return RedirectResponse(
            url=f"/admin/groups/{group_id}/?error=transport",
            status_code=303,
        )

    return RedirectResponse(
        url=f"/admin/groups/{group_id}/?ok=removed", status_code=303
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
        return _csrf_failure_response_redirect()
    admin_store = request.app.state.admin_store
    if admin_store is None:
        return RedirectResponse(
            url=f"/admin/groups/{group_id}/?error=admin-disabled",
            status_code=303,
        )

    try:
        admin_store.delete_group(group_id)
    except StorageNotFound:
        return RedirectResponse(
            url="/admin/groups/?error=group-not-found", status_code=303
        )
    except Exception:  # noqa: BLE001 — transport-shaped
        return RedirectResponse(
            url=f"/admin/groups/{group_id}/?error=transport",
            status_code=303,
        )

    return RedirectResponse(
        url="/admin/groups/?ok=deleted", status_code=303
    )


def _csrf_failure_response_redirect() -> HTMLResponse:
    return HTMLResponse(content="CSRF token missing or invalid", status_code=403)


__all__ = [
    "GROUP_WALK_BREADTH_CAP",
    "GROUP_WALK_DEPTH_CAP",
    "router",
    "walk_transitive_workers",
]
