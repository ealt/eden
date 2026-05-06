"""Reference dispatch loop for EDEN v0.

The store interface and backends moved to ``eden-storage`` in
Phase 6. This package now contains the orchestrator-iteration body
(``run_orchestrator_iteration``) and the scripted role workers; the
store types are re-exported from ``eden-storage`` for backward
compatibility with pre-Phase-6 import paths.
"""

from eden_storage import (
    AlreadyExists,
    ConflictingResubmission,
    DispatchError,
    EvaluationSubmission,
    IdeaSubmission,
    IllegalTransition,
    InMemoryStore,
    InvalidPrecondition,
    NotFound,
    SqliteStore,
    Store,
    VariantSubmission,
    WrongToken,
)

from .driver import run_orchestrator_iteration
from .sweep import sweep_expired_claims
from .workers import ScriptedEvaluator, ScriptedExecutor, ScriptedIdeator

__all__ = [
    "AlreadyExists",
    "ConflictingResubmission",
    "DispatchError",
    "EvaluationSubmission",
    "IllegalTransition",
    "VariantSubmission",
    "InMemoryStore",
    "InvalidPrecondition",
    "NotFound",
    "IdeaSubmission",
    "ScriptedEvaluator",
    "ScriptedExecutor",
    "ScriptedIdeator",
    "SqliteStore",
    "Store",
    "WrongToken",
    "run_orchestrator_iteration",
    "sweep_expired_claims",
]
