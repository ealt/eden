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
    NotClaimed,
    NotFound,
    SqliteStore,
    Store,
    VariantSubmission,
)

from .driver import run_orchestrator_iteration
from .policies import (
    IdeationPolicy,
    default_policy,
    fixed_total,
    maintain_pending,
)
from .state_view import ExperimentStateView, build_experiment_state_view
from .sweep import sweep_expired_claims
from .workers import ScriptedEvaluator, ScriptedExecutor, ScriptedIdeator

__all__ = [
    "AlreadyExists",
    "ConflictingResubmission",
    "DispatchError",
    "EvaluationSubmission",
    "ExperimentStateView",
    "IdeaSubmission",
    "IdeationPolicy",
    "IllegalTransition",
    "InMemoryStore",
    "InvalidPrecondition",
    "NotClaimed",
    "NotFound",
    "ScriptedEvaluator",
    "ScriptedExecutor",
    "ScriptedIdeator",
    "SqliteStore",
    "Store",
    "VariantSubmission",
    "build_experiment_state_view",
    "default_policy",
    "fixed_total",
    "maintain_pending",
    "run_orchestrator_iteration",
    "sweep_expired_claims",
]
