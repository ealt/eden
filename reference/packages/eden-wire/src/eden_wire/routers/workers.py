"""Worker-registry routes (chapter 7 §6): register / list / read /
reissue-credential / whoami.

These routes call ``require_admin`` / ``require_worker`` from
:mod:`eden_wire.auth` directly (with ``if deps.admin_token is not None``
guards) rather than the ``_dependencies`` group helpers — the §13.1
registry-management surface is gated on the literal ``admin`` principal,
not on group membership.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, Request

from .._dependencies import RouterDeps, check_experiment
from ..auth import require_admin, require_worker
from ..models import RegisterWorkerRequest


def build_router(deps: RouterDeps) -> APIRouter:
    """Return the workers ``APIRouter`` bound to ``deps``."""
    router = APIRouter(prefix="/v0/experiments/{experiment_id}")
    router.post("/workers")(_register_worker(deps))
    router.get("/workers")(_list_workers(deps))
    router.get("/workers/{worker_id}")(_read_worker(deps))
    router.post("/workers/{worker_id}/reissue-credential")(
        _reissue_credential(deps)
    )
    router.get("/whoami")(_whoami(deps))
    return router


def _register_worker(deps: RouterDeps):
    async def register_worker(
        request: Request,
        experiment_id: str,
        body: RegisterWorkerRequest,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        check_experiment(deps, experiment_id, x_eden_experiment_id)
        # Admin-gated. require_admin raises Forbidden (with the "endpoint
        # requires authentication" path) when auth is off, so test
        # harnesses can still drive the route by passing the admin
        # bearer.
        principal = require_admin(request) if deps.admin_token is not None else None
        # The server mints the opaque worker_id; the caller supplies only
        # an optional display name + labels. ``registered_by`` is the
        # authenticated actor id (the literal ``admin`` principal here).
        worker, registration_token = deps.store.register_worker(
            name=body.name,
            labels=body.labels,
            registered_by=principal.actor_id if principal is not None else None,
        )
        resp = worker.model_dump(mode="json", exclude_none=True)
        if registration_token is not None:
            resp["registration_token"] = registration_token
        return resp

    return register_worker


def _list_workers(deps: RouterDeps):
    async def list_workers(
        request: Request,
        experiment_id: str,
        name: str | None = None,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        check_experiment(deps, experiment_id, x_eden_experiment_id)
        # Either-gated (admin OR worker). Auth was already verified by
        # the middleware; we don't classify further.
        if deps.admin_token is not None:
            _ = request.state.principal  # auth was run; principal is set
        # Optional ``?name=<n>`` exact, case-sensitive filter (§6.2).
        workers = deps.store.list_workers(name=name)
        return {
            "workers": [
                w.model_dump(mode="json", exclude_none=True) for w in workers
            ]
        }

    return list_workers


def _read_worker(deps: RouterDeps):
    async def read_worker(
        experiment_id: str,
        worker_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        check_experiment(deps, experiment_id, x_eden_experiment_id)
        worker = deps.store.read_worker(worker_id)
        return worker.model_dump(mode="json", exclude_none=True)

    return read_worker


def _reissue_credential(deps: RouterDeps):
    async def reissue_credential(
        request: Request,
        experiment_id: str,
        worker_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        check_experiment(deps, experiment_id, x_eden_experiment_id)
        if deps.admin_token is not None:
            require_admin(request)
        token = deps.store.reissue_credential(worker_id)
        worker = deps.store.read_worker(worker_id)
        resp = worker.model_dump(mode="json", exclude_none=True)
        resp["registration_token"] = token
        return resp

    return reissue_credential


def _whoami(deps: RouterDeps):
    async def whoami(
        request: Request,
        experiment_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, str]:
        check_experiment(deps, experiment_id, x_eden_experiment_id)
        # Worker-gated per §6.4: the endpoint exists to confirm the
        # caller's worker_id; an admin bearer cannot speak as a worker,
        # so this MUST 403 for admins.
        if deps.admin_token is not None:
            principal = require_worker(request)
            assert principal.worker_id is not None
            # §6.4: echo the authenticated worker's display name
            # alongside the worker_id (omitted when the worker has none).
            resp: dict[str, str] = {"worker_id": principal.worker_id}
            worker = deps.store.read_worker(principal.worker_id)
            if worker.name is not None:
                resp["name"] = worker.name
            return resp
        # Auth disabled: return a sentinel so tests still get a 200.
        return {"worker_id": "anonymous"}

    return whoami
