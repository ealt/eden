"""Admin-module routes for the group registry — list / detail / register / mutate / delete.

Implements plan §D.5 of phase 12a-1b. Mirrors the chunk-9e admin
module shape (server-side Jinja, auth-first POST, closed-allowlist
banners). Admin-gated writes route through the active experiment's
admin store (``resolve_active_context(request).admin_store``, issue
#145); reads use the active experiment's worker store (either-gated
wire endpoints accept the worker bearer).

The transitive-membership view is walked client-side via repeated
``read_group`` calls — ``StoreClient`` exposes ``resolve_worker_in_group``
as a yes/no probe but no "list transitive workers" op. Plan §3.5 caps
the walk at depth ≤10, breadth ≤1000.
"""

from __future__ import annotations

import re
from typing import Any

from eden_contracts._common import MEMBER_ID_PATTERN, _check_display_name
from eden_storage import RESERVED_GROUP_NAMES
from eden_storage.errors import (
    CycleDetected,
    InvalidName,
    InvalidPrecondition,
    ReservedIdentifier,
)
from eden_storage.errors import (
    NotFound as StorageNotFound,
)
from eden_wire.errors import BadRequest
from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from ._display import sort_by_name_then_id
from ._helpers import csrf_ok, get_session, resolve_active_context

router = APIRouter(prefix="/admin/groups")


# Opaque member-id grammar (``wkr_*`` / ``grp_*``) per
# spec/v0/02-data-model.md §1.6 (identity rename #128). Initial-member
# lines must each be a well-formed opaque member id.
_MEMBER_ID_RE = re.compile(MEMBER_ID_PATTERN)

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
    "reserved-name": (
        "error",
        "this name is reserved (admins / orchestrators)",
    ),
    "invalid-name": (
        "error",
        "name must be 1–128 visible characters (no control chars; "
        "no leading/trailing whitespace)",
    ),
    "invalid-member-id": (
        "error",
        "member_id must be an opaque wkr_*/grp_* id",
    ),
    "invalid-members": (
        "error",
        "one of the initial members failed validation",
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


def _validate_group_name(value: str) -> str | None:
    """Return a banner-key for an invalid display ``name``, else ``None``.

    Validates the operator-supplied group display name against the
    reserved group names + the display-name grammar (#128). The opaque
    ``group_id`` is minted server-side.
    """
    if value in RESERVED_GROUP_NAMES:
        return "reserved-name"
    try:
        _check_display_name(value)
    except ValueError:
        return "invalid-name"
    return None


def _validate_member_id(value: str) -> str | None:
    if not _MEMBER_ID_RE.match(value):
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


# slop-allow: graph traversal closure is most readable as a single
# function. Extraction would force a separate helper to maintain
# (visited, worklist) state, making the recursion harder to follow,
# not easier. 101 lines — 1 over threshold (audit L-V).
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
    active = resolve_active_context(request)
    if isinstance(active, Response):
        return active
    store = active.store
    admin_store = active.admin_store

    q_raw = request.query_params.get("q") or ""
    q = q_raw[:64].lower() if q_raw else None
    # Exact display-name filter → wire ``?name=`` (#128).
    name_raw = request.query_params.get("name") or ""
    name_filter = name_raw[:128] if name_raw else None

    try:
        groups = (
            store.list_groups(name=name_filter)
            if name_filter is not None
            else store.list_groups()
        )
    except Exception:  # noqa: BLE001 — transport/store-domain
        return _read_failure_response(request, "could not load groups")

    # Sort by name-then-id; reserved-name rows (admins / orchestrators)
    # are grouped into a labelled section in the template via the
    # ``is_reserved`` flag (#128 / plan §5.6).
    ordered = sort_by_name_then_id(groups, id_attr="group_id")
    reserved_rows: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    total_transport_errors = 0
    any_truncated = False
    for g in ordered:
        if q and q not in g.group_id.lower() and q not in (g.name or "").lower():
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
        is_reserved = g.name in RESERVED_GROUP_NAMES
        row = {
            "group_id": g.group_id,
            "name": g.name,
            "is_reserved": is_reserved,
            "created_at": g.created_at,
            "created_by": g.created_by,
            "member_count": len(g.members),
            "transitive_worker_count": transitive_worker_count,
            "transitive_worker_label": transitive_label,
            "members_preview": list(g.members[:3]),
            "members_more": max(0, len(g.members) - 3),
        }
        (reserved_rows if is_reserved else rows).append(row)

    return request.app.state.templates.TemplateResponse(
        request,
        "admin_groups.html",
        {
            "session": session,
            "csrf_token": session.csrf,
            "rows": rows,
            "reserved_rows": reserved_rows,
            "q": q_raw,
            "name_filter": name_raw,
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
    name: str = Form(""),
    members: str = Form(""),
) -> RedirectResponse | HTMLResponse:
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    if not csrf_ok(session, csrf_token):
        return _csrf_failure_response_redirect()
    active = resolve_active_context(request)
    if isinstance(active, Response):
        return active
    admin_store = active.admin_store
    if admin_store is None:
        return RedirectResponse(
            url="/admin/groups/?error=admin-disabled", status_code=303
        )

    # Registration mints the opaque group_id server-side; the operator
    # supplies only an OPTIONAL display name (#128). Empty/whitespace-only
    # name → a nameless group; otherwise the RAW name is validated.
    name = name if name.strip() else ""
    if name:
        bad = _validate_group_name(name)
        if bad is not None:
            return RedirectResponse(
                url=f"/admin/groups/?error={bad}", status_code=303
            )

    initial_members, bad_members = _parse_member_lines(members)
    if bad_members is not None:
        return RedirectResponse(
            url="/admin/groups/?error=invalid-members", status_code=303
        )

    try:
        group = admin_store.register_group(
            name or None, members=initial_members or None
        )
    except ReservedIdentifier:
        return RedirectResponse(
            url="/admin/groups/?error=reserved-name", status_code=303
        )
    except InvalidName:
        return RedirectResponse(
            url="/admin/groups/?error=invalid-name", status_code=303
        )
    except CycleDetected:
        return RedirectResponse(
            url="/admin/groups/?error=cycle-detected", status_code=303
        )
    except (BadRequest, InvalidPrecondition):
        return RedirectResponse(
            url="/admin/groups/?error=invalid-name", status_code=303
        )
    except Exception:  # noqa: BLE001 — transport-shaped
        return RedirectResponse(
            url="/admin/groups/?error=transport", status_code=303
        )

    return RedirectResponse(
        url=f"/admin/groups/{group.group_id}/?ok=registered", status_code=303
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
    active = resolve_active_context(request)
    if isinstance(active, Response):
        return active
    store = active.store
    admin_store = active.admin_store

    try:
        group = store.read_group(group_id)
        all_workers = store.list_workers()
        all_groups = store.list_groups()
    except StorageNotFound:
        raise
    except Exception:  # noqa: BLE001 — transport-shaped
        return _read_failure_response(request, "could not load group")

    try:
        walk = walk_transitive_workers(store, group_id)
    except Exception:  # noqa: BLE001
        return _read_failure_response(request, "could not walk group membership")

    # id → name maps for rendering members + attribution as
    # ``<name> (<id>)`` (#128). Members may be workers OR groups.
    member_names: dict[str, str] = {
        w.worker_id: w.name for w in all_workers if w.name
    }
    member_names.update({g.group_id: g.name for g in all_groups if g.name})

    return request.app.state.templates.TemplateResponse(
        request,
        "admin_group_detail.html",
        {
            "session": session,
            "csrf_token": session.csrf,
            "group": group,
            "transitive": walk,
            "member_names": member_names,
            "worker_names": member_names,
            "admin_enabled": admin_store is not None,
            "is_reserved": group.name in RESERVED_GROUP_NAMES,
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
    active = resolve_active_context(request)
    if isinstance(active, Response):
        return active
    admin_store = active.admin_store
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
    active = resolve_active_context(request)
    if isinstance(active, Response):
        return active
    admin_store = active.admin_store
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
    active = resolve_active_context(request)
    if isinstance(active, Response):
        return active
    admin_store = active.admin_store
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
