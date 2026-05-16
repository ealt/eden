"""Admin module — read-only ideas surface (phase 12a-1c, wave 4).

Mirrors the structural shape of [`admin_workers`](admin_workers.py) and
[`admin_groups`](admin_groups.py): server-side Jinja, no JS required,
session-cookie auth-first GET discipline, closed-allowlist banners.

Read-only by design (plan §2 decision 2): the wire surface in 12a-1
defines neither idea-creation nor idea-deletion for an admin user, so
this module exposes no mutating routes. Idea creation continues to
flow through the ideator module.
"""

from __future__ import annotations

from typing import Any

from eden_contracts import Idea
from eden_storage.errors import NotFound as StorageNotFound
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ._helpers import get_session, read_idea_content
from ._lineage import lineage_for_idea

router = APIRouter(prefix="/admin/ideas")

_IDEA_STATES = ("drafting", "ready", "dispatched", "completed")
_INVALID_FILTER = "__invalid__"


def _coerce_filter(raw: str | None, allowed: tuple[str, ...]) -> str | None:
    """Map ``raw`` to a value in ``allowed``, ``None`` (no filter), or ``_INVALID_FILTER``."""
    if raw is None or raw == "*" or raw == "":
        return None
    if raw in allowed:
        return raw
    return _INVALID_FILTER


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


@router.get("/", response_class=HTMLResponse, response_model=None)
async def ideas_index(request: Request) -> HTMLResponse | RedirectResponse:
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    store = request.app.state.store

    state = _coerce_filter(request.query_params.get("state"), _IDEA_STATES)
    if state == _INVALID_FILTER:
        return request.app.state.templates.TemplateResponse(
            request,
            "admin_ideas.html",
            {
                "session": session,
                "rows": [],
                "selected_state": request.query_params.get("state", "*"),
                "idea_states": _IDEA_STATES,
            },
        )

    try:
        ideas = store.list_ideas(state=state)
        variants = store.list_variants()
    except Exception:  # noqa: BLE001 — transport / store-domain
        return _read_failure_response(request, "could not load ideas")

    variant_count_by_idea: dict[str, int] = {}
    for v in variants:
        variant_count_by_idea[v.idea_id] = (
            variant_count_by_idea.get(v.idea_id, 0) + 1
        )

    rows: list[dict[str, Any]] = []
    for idea in ideas:
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
            "selected_state": state or "*",
            "idea_states": _IDEA_STATES,
        },
    )


@router.get("/{idea_id}/", response_class=HTMLResponse, response_model=None)
async def idea_detail(
    idea_id: str, request: Request
) -> HTMLResponse | RedirectResponse:
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    store = request.app.state.store
    artifacts_dir = request.app.state.artifacts_dir

    try:
        idea: Idea = store.read_idea(idea_id)
    except StorageNotFound:
        # Propagate to the app-wide handler (renders the standard
        # 404 page). Mirrors chunk-9e variant_detail behavior.
        raise
    except Exception:  # noqa: BLE001 — transport / store-domain
        return _read_failure_response(request, "could not load idea")

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
        },
    )


__all__ = ["router"]
