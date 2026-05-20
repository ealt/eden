"""Admin-module routes — observability views + operator actions.

Implements the chunk 9e plan plus 12a-1b/c/12a-3 follow-ups. The
single-file `admin.py` was split into per-concern sub-modules during
the M-1 refactor (see [`docs/audits/2026-05-20-code-quality-audit.md`][audit]):

[audit]: ../../../../../../../docs/audits/2026-05-20-code-quality-audit.md

- [`index.py`](index.py) — the `/admin/` dashboard.
- [`observability.py`](observability.py) — read-only views (tasks,
  variants, events, ideas, experiment).
- [`actions.py`](actions.py) — operator mutations (task reclaim/
  reassign, dispatch-mode, create-execution-task, terminate-experiment).
- [`work_refs.py`](work_refs.py) — work-ref GC (list + delete).
- [`_common.py`](_common.py) — shared constants and helpers used
  across the sub-modules.

The URL surface is unchanged — every route still mounts at
`/admin/...`. `app.py` imports this package as `admin_routes` and
calls `app.include_router(admin_routes.router)`.

Auth-first POST discipline: every handler — GET and POST — runs
``get_session(request)`` first; an absent session redirects to
``/signin``. CSRF runs after the auth check on mutating routes.
This matches the ideator / executor / evaluator pattern.
"""

from __future__ import annotations

from fastapi import APIRouter

from .actions import router as _actions_router
from .index import router as _index_router
from .observability import router as _observability_router
from .work_refs import router as _work_refs_router

router = APIRouter()
router.include_router(_index_router)
router.include_router(_observability_router)
router.include_router(_actions_router)
router.include_router(_work_refs_router)

__all__ = ["router"]
