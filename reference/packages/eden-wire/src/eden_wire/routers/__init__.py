"""Per-resource ``APIRouter`` modules for the EDEN wire binding.

Each module exposes ``build_router(deps: RouterDeps) -> APIRouter``;
``eden_wire.server.make_app`` constructs the shared
:class:`eden_wire._dependencies.RouterDeps` once and includes each
router. See issue #115 (F-3) for the regroup rationale.
"""
