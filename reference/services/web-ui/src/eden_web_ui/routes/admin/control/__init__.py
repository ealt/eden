"""Deployment-scoped admin routes (chapter 11 §6).

Mounts the `/admin/control/workers/` and `/admin/control/groups/`
surfaces against the deployment-level registry (the chapter 7 §15
endpoints under `/v0/control/`). Mirrors the per-experiment
`/admin/workers/` / `/admin/groups/` shape but resolves through
`app.state.control_plane` instead of `app.state.store`.

Only registered when `make_app(control_plane=...)` is set — same
gate as `/admin/experiments/`. Issue #146.
"""

from __future__ import annotations

from fastapi import APIRouter

from .groups import router as _groups_router
from .workers import router as _workers_router

router = APIRouter()
router.include_router(_workers_router)
router.include_router(_groups_router)

__all__ = ["router"]
