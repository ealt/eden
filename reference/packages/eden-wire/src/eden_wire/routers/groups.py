"""Group-registry routes (chapter 7 §7): register / list / read /
add-member / remove-member / delete.

Mutating routes call ``require_admin`` from :mod:`eden_wire.auth`
directly (with ``if deps.admin_token is not None`` guards) — the §13.1
registry-management surface is gated on the literal ``admin`` principal.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import Response

from .._dependencies import RouterDeps, check_experiment
from ..auth import require_admin
from ..models import AddGroupMemberRequest, RegisterGroupRequest


def build_router(deps: RouterDeps) -> APIRouter:
    """Return the groups ``APIRouter`` bound to ``deps``."""
    router = APIRouter(prefix="/v0/experiments/{experiment_id}/groups")
    router.post("")(_register_group(deps))
    router.get("")(_list_groups(deps))
    router.get("/{group_id}")(_read_group(deps))
    router.post("/{group_id}/members")(_add_to_group(deps))
    router.delete("/{group_id}/members/{member_id}")(_remove_from_group(deps))
    router.delete("/{group_id}")(_delete_group(deps))
    return router


def _register_group(deps: RouterDeps):
    async def register_group(
        request: Request,
        experiment_id: str,
        body: RegisterGroupRequest,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        check_experiment(deps, experiment_id, x_eden_experiment_id)
        principal = require_admin(request) if deps.admin_token is not None else None
        group = deps.store.register_group(
            body.group_id,
            members=body.members,
            created_by=principal.kind if principal is not None else None,
        )
        return group.model_dump(mode="json", exclude_none=True)

    return register_group


def _list_groups(deps: RouterDeps):
    async def list_groups(
        experiment_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        check_experiment(deps, experiment_id, x_eden_experiment_id)
        groups = deps.store.list_groups()
        return {
            "groups": [
                g.model_dump(mode="json", exclude_none=True) for g in groups
            ]
        }

    return list_groups


def _read_group(deps: RouterDeps):
    async def read_group(
        experiment_id: str,
        group_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        check_experiment(deps, experiment_id, x_eden_experiment_id)
        group = deps.store.read_group(group_id)
        return group.model_dump(mode="json", exclude_none=True)

    return read_group


def _add_to_group(deps: RouterDeps):
    async def add_to_group(
        request: Request,
        experiment_id: str,
        group_id: str,
        body: AddGroupMemberRequest,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        check_experiment(deps, experiment_id, x_eden_experiment_id)
        if deps.admin_token is not None:
            require_admin(request)
        group = deps.store.add_to_group(group_id, body.member_id)
        return group.model_dump(mode="json", exclude_none=True)

    return add_to_group


def _remove_from_group(deps: RouterDeps):
    async def remove_from_group(
        request: Request,
        experiment_id: str,
        group_id: str,
        member_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        check_experiment(deps, experiment_id, x_eden_experiment_id)
        if deps.admin_token is not None:
            require_admin(request)
        group = deps.store.remove_from_group(group_id, member_id)
        return group.model_dump(mode="json", exclude_none=True)

    return remove_from_group


def _delete_group(deps: RouterDeps):
    async def delete_group(
        request: Request,
        experiment_id: str,
        group_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> Response:
        check_experiment(deps, experiment_id, x_eden_experiment_id)
        if deps.admin_token is not None:
            require_admin(request)
        deps.store.delete_group(group_id)
        return Response(status_code=204)

    return delete_group
