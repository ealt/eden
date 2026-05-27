"""Idea routes (chapter 7 §3): create / list / read / mark-ready."""

from __future__ import annotations

from typing import Any

from eden_contracts import Idea
from eden_storage import Store
from fastapi import APIRouter, Body, Header, Query, Request
from fastapi.responses import Response
from pydantic import ValidationError

from .._dependencies import RouterDeps, check_experiment, enforce_worker, stamp_created_by
from ..errors import BadRequest


def build_router(deps: RouterDeps) -> APIRouter:
    """Return the ideas ``APIRouter`` bound to ``deps``."""
    router = APIRouter(prefix="/v0/experiments/{experiment_id}/ideas")
    router.post("")(_create_idea(deps))
    router.get("")(_list_ideas(deps))
    router.get("/{idea_id}")(_read_idea(deps))
    router.post("/{idea_id}/mark-ready")(_mark_idea_ready(deps))
    return router


def _create_idea(deps: RouterDeps):
    async def create_idea(
        request: Request,
        experiment_id: str,
        body: dict[str, Any] = Body(...),
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        check_experiment(deps, experiment_id, x_eden_experiment_id)
        enforce_worker(deps, request)
        body = stamp_created_by(deps, request, body)
        try:
            idea = Idea.model_validate(body)
        except ValidationError as exc:
            raise BadRequest(str(exc)) from exc
        deps.store.create_idea(idea)
        # §3: response body is the idea per idea.schema.json with an
        # OPTIONAL advisory `warnings` array (issue #121). Warnings are
        # non-normative — clients MUST NOT rely on them for correctness.
        out = deps.store.read_idea(idea.idea_id).model_dump(
            mode="json", exclude_none=True
        )
        warnings = _slug_conflict_warnings(deps.store, idea)
        if warnings:
            out["warnings"] = warnings
        return out

    return create_idea


def _list_ideas(deps: RouterDeps):
    async def list_ideas(
        experiment_id: str,
        state: str | None = Query(None),
        x_eden_experiment_id: str | None = Header(None),
    ) -> list[dict[str, Any]]:
        check_experiment(deps, experiment_id, x_eden_experiment_id)
        ideas = deps.store.list_ideas(state=state)
        return [p.model_dump(mode="json", exclude_none=True) for p in ideas]

    return list_ideas


def _read_idea(deps: RouterDeps):
    async def read_idea(
        experiment_id: str,
        idea_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        check_experiment(deps, experiment_id, x_eden_experiment_id)
        return deps.store.read_idea(idea_id).model_dump(
            mode="json", exclude_none=True
        )

    return read_idea


def _mark_idea_ready(deps: RouterDeps):
    async def mark_idea_ready(
        request: Request,
        experiment_id: str,
        idea_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> Response:
        check_experiment(deps, experiment_id, x_eden_experiment_id)
        enforce_worker(deps, request)
        deps.store.mark_idea_ready(idea_id)
        return Response(status_code=204)

    return mark_idea_ready


def _slug_conflict_warnings(store: Store, idea: Idea) -> list[str]:
    """Soft-check (issue #121): return advisory warnings for slug collisions.

    Slug uniqueness is not a protocol invariant — idea identity is by
    ``idea_id`` (spec/v0/02-data-model.md §5.1) and variant branches
    embed the unique ``variant_id`` so collisions are harmless in
    lineage. This helper surfaces collisions to operators at idea-
    creation time so a duplicate slug isn't noticed only when browsing
    ``/admin/ideas/``.
    """
    matches = [
        other.idea_id
        for other in store.list_ideas()
        if other.slug == idea.slug and other.idea_id != idea.idea_id
    ]
    if not matches:
        return []
    quoted = ", ".join(f"{mid!r}" for mid in matches)
    return [f"slug {idea.slug!r} is already used by idea(s) {quoted}"]
