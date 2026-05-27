"""Dispatch-mode routes (chapter 7 §2.8): companion read + admin-gated
partial-merge update.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, Request

from .._dependencies import RouterDeps, check_experiment, enforce_in_any_group
from ..errors import BadRequest
from ..models import DispatchModeResponse, DispatchModeUpdateRequest


def build_router(deps: RouterDeps) -> APIRouter:
    """Return the dispatch_mode ``APIRouter`` bound to ``deps``."""
    router = APIRouter(prefix="/v0/experiments/{experiment_id}/dispatch_mode")
    router.get("")(_read_dispatch_mode(deps))
    router.patch("")(_update_dispatch_mode(deps))
    return router


def _read_dispatch_mode(deps: RouterDeps):
    async def read_dispatch_mode(
        request: Request,
        experiment_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        """§2.8 companion read endpoint (MAY-level per spec).

        Wave-3 exposes the read because the StoreClient's read-back
        ladder for PATCH transport-indeterminate failures needs it.
        Either-auth (admin OR worker) — same posture as ``GET /events``
        and the other read endpoints.
        """
        check_experiment(deps, experiment_id, x_eden_experiment_id)
        if deps.admin_token is not None:
            _ = request.state.principal  # ensure auth was run
        mode = deps.store.read_dispatch_mode()
        return mode.model_dump(mode="json", exclude_none=True)

    return read_dispatch_mode


def _update_dispatch_mode(deps: RouterDeps):
    async def update_dispatch_mode(
        request: Request,
        experiment_id: str,
        body: DispatchModeUpdateRequest,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        """§2.8 admin-group-gated partial-merge update.

        Stamps `updated_by` from the authenticated principal; the request
        body MUST NOT carry the field (the model's ``extra="allow"`` lets
        unknown dispatch_mode keys round-trip per §2.5, but the server
        itself sources `updated_by` from auth).
        """
        check_experiment(deps, experiment_id, x_eden_experiment_id)
        updated_by = enforce_in_any_group(deps, request, ("admins",))
        # Value-grammar validation lives at the wire layer so a bad value
        # (including on an unknown extra="allow" key) becomes a 400
        # BadRequest per chapter 04 §7.1 / chapter 07 §2.8, not a 409
        # invalid-precondition (the store-side check exists as
        # defense-in-depth but is reachable only via direct Store
        # callers). The closed value-set is `auto` / `manual`.
        #
        # Walk the FULL body — known declared fields plus `model_extra`
        # (the `extra="allow"` round-trip slot) — BEFORE
        # `exclude_none=True` collapses null values away. A payload like
        # `{"future_key": null}` would otherwise dump to `{}` and slip
        # through as a vacuous 200 OK.
        known_fields = {
            "termination",
            "ideation_creation",
            "execution_dispatch",
            "evaluation_dispatch",
            "integration",
        }
        all_keys: dict[str, Any] = {}
        for fname in known_fields:
            v = getattr(body, fname, None)
            if v is not None:
                all_keys[fname] = v
        if body.model_extra:
            all_keys.update(body.model_extra)
        for key, value in all_keys.items():
            if value not in ("auto", "manual"):
                raise BadRequest(
                    f"dispatch_mode.{key} value {value!r} is not 'auto' or 'manual'"
                )
        # The known-field subset (sans the unknown extras the wire
        # tolerates but doesn't persist) is what flows to the Store.
        updates = body.model_dump(mode="json", exclude_none=True)
        result = deps.store.update_dispatch_mode(updates, updated_by=updated_by)
        return DispatchModeResponse.model_validate(
            result.model_dump(mode="json", exclude_none=True)
        ).model_dump(mode="json", exclude_none=True)

    return update_dispatch_mode
