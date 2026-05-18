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
from .termination import (
    Continue,
    Terminate,
    TerminationDecision,
    TerminationPolicy,
    convergence_window_policy,
    default_termination_policy,
    env_max_variants_policy,
    max_variants_policy,
    max_wall_time_policy,
    never_terminate,
    target_condition_policy,
)
from .workers import ScriptedEvaluator, ScriptedExecutor, ScriptedIdeator

__all__ = [
    "AlreadyExists",
    "ConflictingResubmission",
    "Continue",
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
    "Terminate",
    "TerminationDecision",
    "TerminationPolicy",
    "VariantSubmission",
    "build_experiment_state_view",
    "convergence_window_policy",
    "default_policy",
    "default_termination_policy",
    "env_max_variants_policy",
    "fixed_total",
    "maintain_pending",
    "max_variants_policy",
    "max_wall_time_policy",
    "never_terminate",
    "run_orchestrator_iteration",
    "sweep_expired_claims",
    "target_condition_policy",
]
