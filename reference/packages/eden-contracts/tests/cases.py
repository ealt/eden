"""Accept/reject corpus, shared by the per-model and parity tests.

Each case is a ``(name, data, should_pass)`` tuple. The corpus is the
single source of truth; its pairs drive both the Pydantic model tests
and the JSON Schema parity test. An asymmetry between the two would
mean a drift in the parity surface and is what the schema-parity job
catches.
"""

from __future__ import annotations

from typing import Any, NamedTuple


class Case(NamedTuple):
    """One fixture: a value to validate and whether validation must succeed."""

    name: str
    data: Any
    should_pass: bool


_DT = "2026-04-23T12:00:00Z"
_DT2 = "2026-04-23T12:30:00.123Z"
_SHA1 = "a" * 40
_SHA256 = "a" * 40 + "b" * 24

# --- opaque-id fixtures (spec/v0/02-data-model.md §1.6) ---
# Valid 26-char lowercase Crockford-base32 suffix (no i/l/o/u).
_ULID = "01hqs3m4n5p6q7r8s9t0v1w2x3"
_ULID2 = "01hqs3m4n5p6q7r8s9t0v1w2x4"
_ULID3 = "01hqs3m4n5p6q7r8s9t0v1w2x5"
_EXP = f"exp_{_ULID}"
_WKR = f"wkr_{_ULID}"
_WKR2 = f"wkr_{_ULID2}"
_WKR3 = f"wkr_{_ULID3}"
_GRP = f"grp_{_ULID}"
# Crockford forbids i/l/o/u; this suffix smuggles an `i` in.
_ULID_BAD_CHAR = "01hqs3m4n5p6q7r8s9t0v1w2xi"
# 25- and 27-char suffixes (wrong length).
_ULID_SHORT = "01hqs3m4n5p6q7r8s9t0v1w2x"
_ULID_LONG = "01hqs3m4n5p6q7r8s9t0v1w2x34"

EXPERIMENT_CONFIG_CASES: list[Case] = [
    Case(
        "minimal",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
        },
        True,
    ),
    Case(
        "with_legacy_fields_as_extras",
        # 12a-3 dropped max_variants / max_wall_time / convergence_window /
        # target_condition from the normative schema; legacy configs that
        # still carry them are accepted as additional top-level properties
        # under 02-data-model.md §2.3's forward-compatibility rule.
        {
            "parallel_variants": 4,
            "evaluation_schema": {"loss": "real", "tokens": "integer", "note": "text"},
            "objective": {"expr": "loss", "direction": "minimize"},
            "max_variants": 200,
            "max_wall_time": "30m",
            "convergence_window": 20,
            "target_condition": "loss < 0.01",
        },
        True,
    ),
    Case(
        "missing_parallel_variants",
        {
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
        },
        False,
    ),
    Case(
        "parallel_variants_zero",
        {
            "parallel_variants": 0,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
        },
        False,
    ),
    Case(
        "evaluation_schema_empty",
        {
            "parallel_variants": 2,
            "evaluation_schema": {},
            "objective": {"expr": "accuracy", "direction": "maximize"},
        },
        False,
    ),
    Case(
        "evaluation_schema_reserved_key",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"variant_id": "text"},
            "objective": {"expr": "variant_id", "direction": "maximize"},
        },
        False,
    ),
    Case(
        "objective_invalid_direction",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "sideways"},
        },
        False,
    ),
    Case(
        "parallel_variants_bool",
        {
            "parallel_variants": True,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
        },
        False,
    ),
    Case(
        "dispatch_mode_all_auto",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "dispatch_mode": {
                "termination": "auto",
                "ideation_creation": "auto",
                "execution_dispatch": "auto",
                "evaluation_dispatch": "auto",
                "integration": "auto",
            },
            # termination == "auto" REQUIRES a termination_policy (cross-field
            # rule, enforced on both schema + Pydantic sides).
            "termination_policy": {"kind": "never_terminate"},
        },
        True,
    ),
    Case(
        "dispatch_mode_mixed",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "dispatch_mode": {
                "termination": "manual",
                "evaluation_dispatch": "manual",
                "integration": "manual",
            },
        },
        True,
    ),
    Case(
        "dispatch_mode_termination_only",
        # 12a-3: a deployment that wants policy-driven termination flips
        # only the new key; the four operational keys default to "auto".
        # Issue #157: termination == "auto" now REQUIRES a termination_policy.
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "dispatch_mode": {"termination": "auto"},
            "termination_policy": {"kind": "max_variants", "target": 50},
        },
        True,
    ),
    Case(
        "dispatch_mode_unknown_key_tolerated",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            # Per 02-data-model.md §2.4: unknown keys are tolerated
            # by the schema and ignored by conforming implementations.
            "dispatch_mode": {"future_decision": "auto"},
        },
        True,
    ),
    Case(
        "dispatch_mode_invalid_value",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "dispatch_mode": {"ideation_creation": "paused"},
        },
        False,
    ),
    Case(
        "dispatch_mode_null",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "dispatch_mode": None,
        },
        False,
    ),
    Case(
        "ideation_policy_maintain_pending_minimal",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "ideation_policy": {"kind": "maintain_pending"},
        },
        True,
    ),
    Case(
        "ideation_policy_maintain_pending_full",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "ideation_policy": {
                "kind": "maintain_pending",
                "target": 5,
                "max_total": 100,
            },
        },
        True,
    ),
    Case(
        "ideation_policy_maintain_pending_unbounded_max_total",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "ideation_policy": {
                "kind": "maintain_pending",
                "target": 3,
                "max_total": None,
            },
        },
        True,
    ),
    Case(
        "ideation_policy_fixed_total_ok",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "ideation_policy": {"kind": "fixed_total", "total": 10},
        },
        True,
    ),
    Case(
        "ideation_policy_unknown_extra_key_tolerated",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "ideation_policy": {
                "kind": "maintain_pending",
                "target": 3,
                "future_arg": "ignored",
            },
        },
        True,
    ),
    Case(
        "ideation_policy_bad_kind",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "ideation_policy": {"kind": "round_robin"},
        },
        False,
    ),
    Case(
        "ideation_policy_missing_kind",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "ideation_policy": {"target": 3},
        },
        False,
    ),
    Case(
        "ideation_policy_maintain_pending_target_zero",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "ideation_policy": {"kind": "maintain_pending", "target": 0},
        },
        False,
    ),
    Case(
        "ideation_policy_maintain_pending_negative_max_total",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "ideation_policy": {
                "kind": "maintain_pending",
                "target": 3,
                "max_total": -1,
            },
        },
        False,
    ),
    Case(
        "ideation_policy_fixed_total_missing_total",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "ideation_policy": {"kind": "fixed_total"},
        },
        False,
    ),
    Case(
        "ideation_policy_fixed_total_zero",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "ideation_policy": {"kind": "fixed_total", "total": 0},
        },
        False,
    ),
    Case(
        "ideation_policy_null",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "ideation_policy": None,
        },
        False,
    ),
    # --- termination_policy (issue #157) ---
    Case(
        "termination_policy_never_terminate",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "termination_policy": {"kind": "never_terminate"},
        },
        True,
    ),
    Case(
        "termination_policy_max_variants_ok",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "termination_policy": {"kind": "max_variants", "target": 200},
        },
        True,
    ),
    Case(
        "termination_policy_max_variants_missing_target",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "termination_policy": {"kind": "max_variants"},
        },
        False,
    ),
    Case(
        "termination_policy_max_variants_target_zero",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "termination_policy": {"kind": "max_variants", "target": 0},
        },
        False,
    ),
    Case(
        "termination_policy_max_wall_time_ok",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "termination_policy": {"kind": "max_wall_time", "duration": "PT2H"},
        },
        True,
    ),
    Case(
        "termination_policy_max_wall_time_missing_duration",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "termination_policy": {"kind": "max_wall_time"},
        },
        False,
    ),
    Case(
        "termination_policy_max_wall_time_duration_not_iso",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "termination_policy": {"kind": "max_wall_time", "duration": "2h"},
        },
        False,
    ),
    Case(
        "termination_policy_max_wall_time_duration_zero",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "termination_policy": {"kind": "max_wall_time", "duration": "PT0S"},
        },
        False,
    ),
    Case(
        "termination_policy_convergence_window_ok",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "termination_policy": {
                "kind": "convergence_window",
                "metric": "accuracy",
                "window": 10,
                "direction": "minimize",
            },
        },
        True,
    ),
    Case(
        "termination_policy_convergence_window_default_direction",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "termination_policy": {
                "kind": "convergence_window",
                "metric": "accuracy",
                "window": 5,
            },
        },
        True,
    ),
    Case(
        "termination_policy_convergence_window_missing_window",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "termination_policy": {"kind": "convergence_window", "metric": "accuracy"},
        },
        False,
    ),
    Case(
        "termination_policy_convergence_window_missing_metric",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "termination_policy": {"kind": "convergence_window", "window": 5},
        },
        False,
    ),
    Case(
        "termination_policy_convergence_window_window_zero",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "termination_policy": {
                "kind": "convergence_window",
                "metric": "accuracy",
                "window": 0,
            },
        },
        False,
    ),
    Case(
        "termination_policy_target_condition_ok",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "termination_policy": {
                "kind": "target_condition",
                "metric": "accuracy",
                "threshold": 0.95,
                "direction": "maximize",
            },
        },
        True,
    ),
    Case(
        "termination_policy_target_condition_missing_threshold",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "termination_policy": {"kind": "target_condition", "metric": "accuracy"},
        },
        False,
    ),
    Case(
        "termination_policy_target_condition_missing_metric",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "termination_policy": {"kind": "target_condition", "threshold": 0.95},
        },
        False,
    ),
    Case(
        "termination_policy_unknown_extra_key_tolerated",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "termination_policy": {
                "kind": "max_variants",
                "target": 10,
                "future_arg": "ignored",
            },
        },
        True,
    ),
    Case(
        "termination_policy_bad_kind",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "termination_policy": {"kind": "halt_now"},
        },
        False,
    ),
    Case(
        "termination_policy_missing_kind",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "termination_policy": {"target": 10},
        },
        False,
    ),
    Case(
        "termination_policy_null",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "termination_policy": None,
        },
        False,
    ),
    # --- cross-field: termination_policy required when termination == "auto" ---
    Case(
        "termination_auto_with_policy",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "dispatch_mode": {"termination": "auto"},
            "termination_policy": {"kind": "never_terminate"},
        },
        True,
    ),
    Case(
        "termination_auto_without_policy",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "dispatch_mode": {"termination": "auto"},
        },
        False,
    ),
    Case(
        "termination_manual_with_policy",
        # termination == "manual" (the default): a termination_policy MAY be
        # present (ignored at runtime) and is schema-valid.
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "dispatch_mode": {"termination": "manual"},
            "termination_policy": {"kind": "max_variants", "target": 10},
        },
        True,
    ),
    Case(
        "termination_manual_without_policy",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "dispatch_mode": {"termination": "manual"},
        },
        True,
    ),
    # --- max_quiescent_iterations (issue #157) ---
    Case(
        "max_quiescent_iterations_ok",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "max_quiescent_iterations": 30,
        },
        True,
    ),
    Case(
        "max_quiescent_iterations_below_min",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "max_quiescent_iterations": 1,
        },
        False,
    ),
    Case(
        "max_quiescent_iterations_bool",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "max_quiescent_iterations": True,
        },
        False,
    ),
    Case(
        "max_quiescent_iterations_null",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "max_quiescent_iterations": None,
        },
        False,
    ),
    # --- *_task_deadline scalars (issue #157) ---
    Case(
        "task_deadlines_ok",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "ideation_task_deadline": 120.0,
            "execution_task_deadline": 600.0,
            "evaluation_task_deadline": 300.0,
        },
        True,
    ),
    Case(
        "ideation_task_deadline_zero",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "ideation_task_deadline": 0,
        },
        False,
    ),
    Case(
        "execution_task_deadline_negative",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "execution_task_deadline": -1.0,
        },
        False,
    ),
    Case(
        "evaluation_task_deadline_null",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "evaluation_task_deadline": None,
        },
        False,
    ),
    # --- baseline block (02-data-model.md §2.7) ---
    Case(
        "baseline_enabled_true",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "baseline": {"enabled": True},
        },
        True,
    ),
    Case(
        "baseline_disabled",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "baseline": {"enabled": False},
        },
        True,
    ),
    Case(
        "baseline_with_metrics_override",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "baseline": {"metrics": {"accuracy": 0.5}},
        },
        True,
    ),
    Case(
        "baseline_enabled_with_metrics",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "baseline": {"enabled": True, "metrics": {"accuracy": 0.5}},
        },
        True,
    ),
    Case(
        "baseline_empty_block",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "baseline": {},
        },
        True,
    ),
    Case(
        "baseline_disabled_with_metrics_rejected",
        # Suppressing a baseline while supplying its metrics is a config error
        # (02-data-model.md §2.7).
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "baseline": {"enabled": False, "metrics": {"accuracy": 0.5}},
        },
        False,
    ),
    Case(
        "baseline_enabled_explicit_null_rejected",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "baseline": {"enabled": None},
        },
        False,
    ),
    # --- auto_checkpoint block (issue #131) ---
    Case(
        "auto_checkpoint_full_block",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "auto_checkpoint": {
                "enabled": True,
                "interval_seconds": 1800,
                "retention_count": 4,
                "on_terminate": True,
            },
        },
        True,
    ),
    Case(
        "auto_checkpoint_empty_block",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "auto_checkpoint": {},
        },
        True,
    ),
    Case(
        "auto_checkpoint_enabled_only",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "auto_checkpoint": {"enabled": False},
        },
        True,
    ),
    Case(
        "auto_checkpoint_fractional_interval_ok",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "auto_checkpoint": {"interval_seconds": 0.5},
        },
        True,
    ),
    Case(
        "auto_checkpoint_interval_zero_rejected",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "auto_checkpoint": {"interval_seconds": 0},
        },
        False,
    ),
    Case(
        "auto_checkpoint_interval_negative_rejected",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "auto_checkpoint": {"interval_seconds": -1},
        },
        False,
    ),
    Case(
        "auto_checkpoint_retention_zero_rejected",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "auto_checkpoint": {"retention_count": 0},
        },
        False,
    ),
    Case(
        "auto_checkpoint_unknown_key_rejected",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "auto_checkpoint": {"destination": "/tmp/x"},
        },
        False,
    ),
    Case(
        "auto_checkpoint_enabled_wrong_type_rejected",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "auto_checkpoint": {"enabled": "yes"},
        },
        False,
    ),
    Case(
        "auto_checkpoint_retention_non_integer_rejected",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "auto_checkpoint": {"retention_count": 2.5},
        },
        False,
    ),
    Case(
        "auto_checkpoint_enabled_explicit_null_rejected",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "auto_checkpoint": {"enabled": None},
        },
        False,
    ),
    Case(
        "auto_checkpoint_interval_explicit_null_rejected",
        {
            "parallel_variants": 2,
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "auto_checkpoint": {"interval_seconds": None},
        },
        False,
    ),
]


_VALID_CLAIM = {
    "worker_id": _WKR,
    "claimed_at": _DT,
}

TASK_CASES: list[Case] = [
    Case(
        "ideation_pending",
        {
            "task_id": "t-1",
            "kind": "ideation",
            "state": "pending",
            "payload": {"experiment_id": _EXP},
            "created_at": _DT,
            "updated_at": _DT,
        },
        True,
    ),
    Case(
        "execution_claimed",
        {
            "task_id": "t-2",
            "kind": "execution",
            "state": "claimed",
            "payload": {"idea_id": "idea-1"},
            "claim": _VALID_CLAIM,
            "created_at": _DT,
            "updated_at": _DT,
        },
        True,
    ),
    Case(
        "evaluation_submitted",
        {
            "task_id": "t-3",
            "kind": "evaluation",
            "state": "submitted",
            "payload": {"variant_id": "variant-1"},
            "claim": {**_VALID_CLAIM, "expires_at": _DT2},
            "created_at": _DT,
            "updated_at": _DT2,
        },
        True,
    ),
    Case(
        "ideation_completed_no_claim",
        {
            "task_id": "t-4",
            "kind": "ideation",
            "state": "completed",
            "payload": {"experiment_id": _EXP},
            "created_at": _DT,
            "updated_at": _DT2,
        },
        True,
    ),
    Case(
        "ideation_failed_no_claim",
        {
            "task_id": "t-5",
            "kind": "ideation",
            "state": "failed",
            "payload": {"experiment_id": _EXP},
            "created_at": _DT,
            "updated_at": _DT2,
        },
        True,
    ),
    Case(
        "claimed_without_claim",
        {
            "task_id": "t-6",
            "kind": "ideation",
            "state": "claimed",
            "payload": {"experiment_id": _EXP},
            "created_at": _DT,
            "updated_at": _DT,
        },
        False,
    ),
    Case(
        "submitted_without_claim",
        {
            "task_id": "t-7",
            "kind": "evaluation",
            "state": "submitted",
            "payload": {"variant_id": "variant-1"},
            "created_at": _DT,
            "updated_at": _DT,
        },
        False,
    ),
    Case(
        "pending_with_claim",
        {
            "task_id": "t-8",
            "kind": "ideation",
            "state": "pending",
            "payload": {"experiment_id": _EXP},
            "claim": _VALID_CLAIM,
            "created_at": _DT,
            "updated_at": _DT,
        },
        False,
    ),
    Case(
        "completed_with_claim",
        {
            "task_id": "t-9",
            "kind": "ideation",
            "state": "completed",
            "payload": {"experiment_id": _EXP},
            "claim": _VALID_CLAIM,
            "created_at": _DT,
            "updated_at": _DT,
        },
        False,
    ),
    Case(
        "ideation_task_missing_experiment_id",
        {
            "task_id": "t-10",
            "kind": "ideation",
            "state": "pending",
            "payload": {},
            "created_at": _DT,
            "updated_at": _DT,
        },
        False,
    ),
    Case(
        "execution_task_missing_idea_id",
        {
            "task_id": "t-11",
            "kind": "execution",
            "state": "pending",
            "payload": {"experiment_id": _EXP},
            "created_at": _DT,
            "updated_at": _DT,
        },
        False,
    ),
    Case(
        "invalid_kind",
        {
            "task_id": "t-12",
            "kind": "integrate",
            "state": "pending",
            "payload": {},
            "created_at": _DT,
            "updated_at": _DT,
        },
        False,
    ),
    Case(
        "invalid_state",
        {
            "task_id": "t-13",
            "kind": "ideation",
            "state": "running",
            "payload": {"experiment_id": _EXP},
            "created_at": _DT,
            "updated_at": _DT,
        },
        False,
    ),
    Case(
        "missing_updated_at",
        {
            "task_id": "t-14",
            "kind": "ideation",
            "state": "pending",
            "payload": {"experiment_id": _EXP},
            "created_at": _DT,
        },
        False,
    ),
    Case(
        "claim_bad_datetime",
        {
            "task_id": "t-15",
            "kind": "ideation",
            "state": "claimed",
            "payload": {"experiment_id": _EXP},
            "claim": {"worker_id": _WKR, "claimed_at": "2026-04-23 12:00:00"},
            "created_at": _DT,
            "updated_at": _DT,
        },
        False,
    ),
    Case(
        "impossible_datetime",
        {
            "task_id": "t-16",
            "kind": "ideation",
            "state": "pending",
            "payload": {"experiment_id": _EXP},
            "created_at": "2026-99-99T12:00:00Z",
            "updated_at": _DT,
        },
        False,
    ),
    Case(
        "claim_null_on_pending",
        {
            "task_id": "t-17",
            "kind": "ideation",
            "state": "pending",
            "payload": {"experiment_id": _EXP},
            "claim": None,
            "created_at": _DT,
            "updated_at": _DT,
        },
        False,
    ),
    Case(
        "target_worker_ok",
        {
            "task_id": "t-18",
            "kind": "ideation",
            "state": "pending",
            "payload": {"experiment_id": _EXP},
            "target": {"kind": "worker", "id": _WKR},
            "created_by": "admin",
            "created_at": _DT,
            "updated_at": _DT,
        },
        True,
    ),
    Case(
        "target_group_ok",
        {
            "task_id": "t-19",
            "kind": "execution",
            "state": "pending",
            "payload": {"idea_id": "idea-1"},
            "target": {"kind": "group", "id": _GRP},
            "created_at": _DT,
            "updated_at": _DT,
        },
        True,
    ),
    Case(
        "created_by_worker_actor_ok",
        {
            "task_id": "t-18b",
            "kind": "ideation",
            "state": "pending",
            "payload": {"experiment_id": _EXP},
            "created_by": _WKR,
            "created_at": _DT,
            "updated_at": _DT,
        },
        True,
    ),
    Case(
        "submitted_by_persists_after_terminal",
        {
            "task_id": "t-20",
            "kind": "evaluation",
            "state": "completed",
            "payload": {"variant_id": "variant-1"},
            "submitted_by": _WKR,
            "created_at": _DT,
            "updated_at": _DT2,
        },
        True,
    ),
    Case(
        "target_invalid_kind",
        {
            "task_id": "t-21",
            "kind": "ideation",
            "state": "pending",
            "payload": {"experiment_id": _EXP},
            "target": {"kind": "anyone", "id": _WKR},
            "created_at": _DT,
            "updated_at": _DT,
        },
        False,
    ),
    Case(
        "target_id_legacy_kebab",
        {
            "task_id": "t-22",
            "kind": "ideation",
            "state": "pending",
            "payload": {"experiment_id": _EXP},
            "target": {"kind": "worker", "id": "executor-host-1"},
            "created_at": _DT,
            "updated_at": _DT,
        },
        False,
    ),
    Case(
        "target_id_bad_crockford_char",
        {
            "task_id": "t-23",
            "kind": "ideation",
            "state": "pending",
            "payload": {"experiment_id": _EXP},
            "target": {"kind": "worker", "id": f"wkr_{_ULID_BAD_CHAR}"},
            "created_at": _DT,
            "updated_at": _DT,
        },
        False,
    ),
    Case(
        "target_missing_id",
        {
            "task_id": "t-24",
            "kind": "ideation",
            "state": "pending",
            "payload": {"experiment_id": _EXP},
            "target": {"kind": "worker"},
            "created_at": _DT,
            "updated_at": _DT,
        },
        False,
    ),
    Case(
        "submitted_by_legacy_kebab",
        {
            "task_id": "t-25",
            "kind": "ideation",
            "state": "completed",
            "payload": {"experiment_id": _EXP},
            "submitted_by": "worker-a",
            "created_at": _DT,
            "updated_at": _DT,
        },
        False,
    ),
    Case(
        "submitted_by_wrong_prefix",
        {
            "task_id": "t-25b",
            "kind": "ideation",
            "state": "completed",
            "payload": {"experiment_id": _EXP},
            "submitted_by": _GRP,
            "created_at": _DT,
            "updated_at": _DT,
        },
        False,
    ),
    Case(
        "created_by_bad_actor_group",
        {
            "task_id": "t-25c",
            "kind": "ideation",
            "state": "pending",
            "payload": {"experiment_id": _EXP},
            "created_by": _GRP,
            "created_at": _DT,
            "updated_at": _DT,
        },
        False,
    ),
    Case(
        "target_id_wrong_length",
        {
            "task_id": "t-25d",
            "kind": "ideation",
            "state": "pending",
            "payload": {"experiment_id": _EXP},
            "target": {"kind": "worker", "id": f"wkr_{_ULID_SHORT}"},
            "created_at": _DT,
            "updated_at": _DT,
        },
        False,
    ),
    Case(
        "target_null_on_pending",
        {
            "task_id": "t-26",
            "kind": "ideation",
            "state": "pending",
            "payload": {"experiment_id": _EXP},
            "target": None,
            "created_at": _DT,
            "updated_at": _DT,
        },
        False,
    ),
]


EVENT_CASES: list[Case] = [
    # --- envelope-only (§1) and unregistered-type behavior (§3.5) ---
    Case(
        "unregistered_type_open_data",
        {
            "event_id": "evt-unreg-1",
            "type": "operator.paused",
            "occurred_at": _DT2,
            "experiment_id": _EXP,
            "data": {"note": "anything"},
        },
        True,
    ),
    Case(
        "envelope_experiment_id_legacy_kebab",
        {
            "event_id": "evt-bad-exp-1",
            "type": "operator.paused",
            "occurred_at": _DT2,
            "experiment_id": "exp-1",
            "data": {"note": "anything"},
        },
        False,
    ),
    Case(
        "type_without_dot",
        {
            "event_id": "evt-bad-1",
            "type": "claimed",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": {},
        },
        False,
    ),
    Case(
        "type_uppercase",
        {
            "event_id": "evt-bad-2",
            "type": "Task.Claimed",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": {},
        },
        False,
    ),
    Case(
        "occurred_at_with_offset",
        {
            "event_id": "evt-bad-3",
            "type": "task.claimed",
            "occurred_at": "2026-04-23T12:00:00+00:00",
            "experiment_id": _EXP,
            "data": {"task_id": "t-1", "worker_id": _WKR},
        },
        False,
    ),
    Case(
        "missing_experiment_id",
        {
            "event_id": "evt-bad-4",
            "type": "task.claimed",
            "occurred_at": _DT,
            "data": {"task_id": "t-1", "worker_id": _WKR},
        },
        False,
    ),
    Case(
        "data_not_object",
        {
            "event_id": "evt-bad-5",
            "type": "task.claimed",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": "hello",
        },
        False,
    ),
    # --- registered types: task.* ---
    Case(
        "task_created_ok",
        {
            "event_id": "evt-tc-1",
            "type": "task.created",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": {"task_id": "t-1", "kind": "ideation"},
        },
        True,
    ),
    Case(
        "task_created_bad_kind",
        {
            "event_id": "evt-tc-2",
            "type": "task.created",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": {"task_id": "t-1", "kind": "integrate"},
        },
        False,
    ),
    Case(
        "task_created_missing_kind",
        {
            "event_id": "evt-tc-3",
            "type": "task.created",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": {"task_id": "t-1"},
        },
        False,
    ),
    Case(
        "task_claimed_ok",
        {
            "event_id": "evt-tcl-1",
            "type": "task.claimed",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": {"task_id": "t-1", "worker_id": _WKR},
        },
        True,
    ),
    Case(
        "task_claimed_worker_legacy_kebab",
        {
            "event_id": "evt-tcl-1b",
            "type": "task.claimed",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": {"task_id": "t-1", "worker_id": "worker-a"},
        },
        False,
    ),
    Case(
        "task_claimed_worker_wrong_prefix",
        {
            "event_id": "evt-tcl-1c",
            "type": "task.claimed",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": {"task_id": "t-1", "worker_id": _GRP},
        },
        False,
    ),
    Case(
        "task_claimed_missing_worker",
        {
            "event_id": "evt-tcl-2",
            "type": "task.claimed",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": {"task_id": "t-1"},
        },
        False,
    ),
    Case(
        "task_submitted_ok",
        {
            "event_id": "evt-ts-1",
            "type": "task.submitted",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": {"task_id": "t-1"},
        },
        True,
    ),
    Case(
        "task_completed_ok",
        {
            "event_id": "evt-tcp-1",
            "type": "task.completed",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": {"task_id": "t-1"},
        },
        True,
    ),
    Case(
        "task_failed_ok",
        {
            "event_id": "evt-tf-1",
            "type": "task.failed",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": {"task_id": "t-1", "reason": "worker_error"},
        },
        True,
    ),
    Case(
        "task_failed_bad_reason",
        {
            "event_id": "evt-tf-2",
            "type": "task.failed",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": {"task_id": "t-1", "reason": "oops"},
        },
        False,
    ),
    Case(
        "task_reclaimed_ok",
        {
            "event_id": "evt-variant-1",
            "type": "task.reclaimed",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": {"task_id": "t-1", "cause": "expired"},
        },
        True,
    ),
    Case(
        "task_reclaimed_bad_cause",
        {
            "event_id": "evt-variant-2",
            "type": "task.reclaimed",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": {"task_id": "t-1", "cause": "timeout"},
        },
        False,
    ),
    # --- registered types: task.reassigned ---
    Case(
        "task_reassigned_to_worker_ok",
        {
            "event_id": "evt-tre-1",
            "type": "task.reassigned",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": {
                "task_id": "t-1",
                "new_target": {"kind": "worker", "id": _WKR},
                "reason": "operator",
                "reassigned_by": "admin",
            },
        },
        True,
    ),
    Case(
        "task_reassigned_to_group_ok",
        {
            "event_id": "evt-tre-2",
            "type": "task.reassigned",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": {
                "task_id": "t-2",
                "new_target": {"kind": "group", "id": _GRP},
                "reason": "misrouted",
                "reassigned_by": _WKR2,
            },
        },
        True,
    ),
    Case(
        "task_reassigned_to_null_ok",
        {
            "event_id": "evt-tre-3",
            "type": "task.reassigned",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": {
                "task_id": "t-3",
                "new_target": None,
                "reason": "open up to any worker",
                "reassigned_by": _WKR2,
            },
        },
        True,
    ),
    Case(
        "task_reassigned_missing_new_target",
        {
            "event_id": "evt-tre-4",
            "type": "task.reassigned",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": {
                "task_id": "t-4",
                "reason": "operator",
                "reassigned_by": _WKR2,
            },
        },
        False,
    ),
    Case(
        "task_reassigned_empty_reason",
        {
            "event_id": "evt-tre-5",
            "type": "task.reassigned",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": {
                "task_id": "t-5",
                "new_target": None,
                "reason": "",
                "reassigned_by": _WKR2,
            },
        },
        False,
    ),
    Case(
        "task_reassigned_actor_legacy_kebab",
        {
            "event_id": "evt-tre-6",
            "type": "task.reassigned",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": {
                "task_id": "t-6",
                "new_target": None,
                "reason": "operator",
                "reassigned_by": "admin-eric",
            },
        },
        False,
    ),
    Case(
        "task_reassigned_actor_wrong_prefix",
        {
            "event_id": "evt-tre-6b",
            "type": "task.reassigned",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": {
                "task_id": "t-6b",
                "new_target": None,
                "reason": "operator",
                "reassigned_by": _GRP,
            },
        },
        False,
    ),
    Case(
        "task_reassigned_new_target_legacy_kebab",
        {
            "event_id": "evt-tre-6c",
            "type": "task.reassigned",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": {
                "task_id": "t-6c",
                "new_target": {"kind": "worker", "id": "executor-host-1"},
                "reason": "operator",
                "reassigned_by": "admin",
            },
        },
        False,
    ),
    Case(
        "task_reassigned_bad_target_kind",
        {
            "event_id": "evt-tre-7",
            "type": "task.reassigned",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": {
                "task_id": "t-7",
                "new_target": {"kind": "anyone", "id": _WKR},
                "reason": "operator",
                "reassigned_by": _WKR2,
            },
        },
        False,
    ),
    # --- registered types: experiment.dispatch_mode_changed ---
    Case(
        "experiment_dispatch_mode_changed_ok",
        {
            "event_id": "evt-edm-1",
            "type": "experiment.dispatch_mode_changed",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": {
                "dispatch_mode": {
                    "ideation_creation": "auto",
                    "execution_dispatch": "auto",
                    "evaluation_dispatch": "manual",
                    "integration": "auto",
                },
                "changed": {"evaluation_dispatch": "manual"},
                "updated_by": "admin",
            },
        },
        True,
    ),
    Case(
        "experiment_dispatch_mode_changed_missing_changed",
        {
            "event_id": "evt-edm-2",
            "type": "experiment.dispatch_mode_changed",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": {
                "dispatch_mode": {
                    "ideation_creation": "auto",
                    "execution_dispatch": "auto",
                    "evaluation_dispatch": "auto",
                    "integration": "auto",
                },
                "updated_by": "admin",
            },
        },
        False,
    ),
    Case(
        "experiment_dispatch_mode_changed_invalid_value",
        {
            "event_id": "evt-edm-3",
            "type": "experiment.dispatch_mode_changed",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": {
                "dispatch_mode": {"ideation_creation": "paused"},
                "changed": {"ideation_creation": "paused"},
                "updated_by": "admin",
            },
        },
        False,
    ),
    Case(
        "experiment_dispatch_mode_changed_actor_legacy_kebab",
        {
            "event_id": "evt-edm-4",
            "type": "experiment.dispatch_mode_changed",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": {
                "dispatch_mode": {"integration": "manual"},
                "changed": {"integration": "manual"},
                "updated_by": "admin-eric",
            },
        },
        False,
    ),
    # --- registered types: experiment.terminated (12a-3) ---
    Case(
        "experiment_terminated_ok",
        {
            "event_id": "evt-term-1",
            "type": "experiment.terminated",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": {
                "reason": "max_variants policy reached",
                "terminated_by": "admin",
            },
        },
        True,
    ),
    Case(
        "experiment_terminated_missing_reason",
        {
            "event_id": "evt-term-2",
            "type": "experiment.terminated",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": {"terminated_by": "admin"},
        },
        False,
    ),
    Case(
        "experiment_terminated_actor_legacy_kebab",
        {
            "event_id": "evt-term-3",
            "type": "experiment.terminated",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": {"reason": "done", "terminated_by": "admin-eric"},
        },
        False,
    ),
    Case(
        "experiment_terminated_actor_worker_ok",
        {
            "event_id": "evt-term-4",
            "type": "experiment.terminated",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": {"reason": "policy reached", "terminated_by": _WKR},
        },
        True,
    ),
    # --- registered types: experiment.policy_error (12a-3) ---
    Case(
        "experiment_policy_error_ok",
        {
            "event_id": "evt-pe-1",
            "type": "experiment.policy_error",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": {
                "policy_kind": "termination",
                "error_type": "ValueError",
                "error_message": "policy callable raised: bad config",
            },
        },
        True,
    ),
    Case(
        "experiment_policy_error_missing_type",
        {
            "event_id": "evt-pe-2",
            "type": "experiment.policy_error",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": {"policy_kind": "termination", "error_message": "x"},
        },
        False,
    ),
    # --- registered types: idea.* ---
    Case(
        "idea_drafted_ok",
        {
            "event_id": "evt-pd-1",
            "type": "idea.drafted",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": {"idea_id": "idea-1"},
        },
        True,
    ),
    Case(
        "idea_ready_ok",
        {
            "event_id": "evt-pr-1",
            "type": "idea.ready",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": {"idea_id": "idea-1"},
        },
        True,
    ),
    Case(
        "idea_dispatched_ok",
        {
            "event_id": "evt-pdi-1",
            "type": "idea.dispatched",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": {"idea_id": "idea-1", "task_id": "t-1"},
        },
        True,
    ),
    Case(
        "idea_dispatched_missing_task",
        {
            "event_id": "evt-pdi-2",
            "type": "idea.dispatched",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": {"idea_id": "idea-1"},
        },
        False,
    ),
    Case(
        "idea_completed_ok",
        {
            "event_id": "evt-pc-1",
            "type": "idea.completed",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": {"idea_id": "idea-1", "task_id": "t-1"},
        },
        True,
    ),
    # --- registered types: variant.* ---
    Case(
        "variant_started_ok",
        {
            "event_id": "evt-ts-100",
            "type": "variant.started",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": {"variant_id": "variant-1", "idea_id": "idea-1"},
        },
        True,
    ),
    Case(
        "variant_started_baseline_ok",
        {
            "event_id": "evt-ts-101",
            "type": "variant.started",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": {"variant_id": "baseline", "kind": "baseline"},
        },
        True,
    ),
    Case(
        "variant_started_missing_idea_id_not_baseline",
        {
            "event_id": "evt-ts-102",
            "type": "variant.started",
            "occurred_at": _DT,
            "experiment_id": "exp-1",
            "data": {"variant_id": "variant-1"},
        },
        False,
    ),
    Case(
        "variant_started_baseline_may_carry_idea_id",
        {
            "event_id": "evt-ts-103",
            "type": "variant.started",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": {"variant_id": "baseline", "kind": "baseline", "idea_id": "idea-1"},
        },
        True,
    ),
    Case(
        "variant_succeeded_ok",
        {
            "event_id": "evt-ts-200",
            "type": "variant.succeeded",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": {"variant_id": "variant-1", "commit_sha": _SHA1},
        },
        True,
    ),
    Case(
        "variant_succeeded_bad_sha",
        {
            "event_id": "evt-ts-201",
            "type": "variant.succeeded",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": {"variant_id": "variant-1", "commit_sha": "abc123"},
        },
        False,
    ),
    Case(
        "variant_errored_ok",
        {
            "event_id": "evt-te-1",
            "type": "variant.errored",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": {"variant_id": "variant-1"},
        },
        True,
    ),
    Case(
        "variant_eval_errored_ok",
        {
            "event_id": "evt-tee-1",
            "type": "variant.evaluation_errored",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": {"variant_id": "variant-1"},
        },
        True,
    ),
    Case(
        "variant_integrated_ok",
        {
            "event_id": "evt-ti-1",
            "type": "variant.integrated",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": {"variant_id": "variant-1", "variant_commit_sha": _SHA256},
        },
        True,
    ),
    Case(
        "variant_integrated_missing_sha",
        {
            "event_id": "evt-ti-2",
            "type": "variant.integrated",
            "occurred_at": _DT,
            "experiment_id": _EXP,
            "data": {"variant_id": "variant-1"},
        },
        False,
    ),
]


IDEA_CASES: list[Case] = [
    Case(
        "drafting",
        {
            "idea_id": "idea-1",
            "experiment_id": _EXP,
            "slug": "improve-tokenizer",
            "priority": 0.5,
            "parent_commits": [_SHA1],
            "artifacts_uri": "s3://bucket/ideas/p-1/",
            "state": "drafting",
            "created_at": _DT,
        },
        True,
    ),
    Case(
        "completed_sha256",
        {
            "idea_id": "idea-2",
            "experiment_id": _EXP,
            "slug": "x",
            "priority": -1.0,
            "parent_commits": [_SHA256],
            "artifacts_uri": "file:///tmp/p-2",
            "state": "completed",
            "created_at": _DT2,
        },
        True,
    ),
    Case(
        "slug_uppercase",
        {
            "idea_id": "idea-3",
            "experiment_id": _EXP,
            "slug": "Improve-Tokenizer",
            "priority": 0.0,
            "parent_commits": [_SHA1],
            "artifacts_uri": "s3://b/",
            "state": "drafting",
            "created_at": _DT,
        },
        False,
    ),
    Case(
        "slug_leading_dash",
        {
            "idea_id": "idea-4",
            "experiment_id": _EXP,
            "slug": "-improve",
            "priority": 0.0,
            "parent_commits": [_SHA1],
            "artifacts_uri": "s3://b/",
            "state": "drafting",
            "created_at": _DT,
        },
        False,
    ),
    Case(
        "bad_state",
        {
            "idea_id": "idea-5",
            "experiment_id": _EXP,
            "slug": "s",
            "priority": 0.0,
            "parent_commits": [_SHA1],
            "artifacts_uri": "s3://b/",
            "state": "accepted",
            "created_at": _DT,
        },
        False,
    ),
    Case(
        "empty_parent_commits",
        {
            "idea_id": "idea-6",
            "experiment_id": _EXP,
            "slug": "s",
            "priority": 0.0,
            "parent_commits": [],
            "artifacts_uri": "s3://b/",
            "state": "drafting",
            "created_at": _DT,
        },
        False,
    ),
    Case(
        "bad_commit_sha_length",
        {
            "idea_id": "idea-7",
            "experiment_id": _EXP,
            "slug": "s",
            "priority": 0.0,
            "parent_commits": ["abc123"],
            "artifacts_uri": "s3://b/",
            "state": "drafting",
            "created_at": _DT,
        },
        False,
    ),
    Case(
        "bad_commit_sha_uppercase",
        {
            "idea_id": "idea-8",
            "experiment_id": _EXP,
            "slug": "s",
            "priority": 0.0,
            "parent_commits": ["A" * 40],
            "artifacts_uri": "s3://b/",
            "state": "drafting",
            "created_at": _DT,
        },
        False,
    ),
    Case(
        "priority_bool",
        {
            "idea_id": "idea-9",
            "experiment_id": _EXP,
            "slug": "s",
            "priority": True,
            "parent_commits": [_SHA1],
            "artifacts_uri": "s3://b/",
            "state": "drafting",
            "created_at": _DT,
        },
        False,
    ),
    Case(
        "priority_string",
        {
            "idea_id": "idea-10",
            "experiment_id": _EXP,
            "slug": "s",
            "priority": "1.5",
            "parent_commits": [_SHA1],
            "artifacts_uri": "s3://b/",
            "state": "drafting",
            "created_at": _DT,
        },
        False,
    ),
    Case(
        "artifacts_uri_no_scheme",
        {
            "idea_id": "idea-11",
            "experiment_id": _EXP,
            "slug": "s",
            "priority": 0.0,
            "parent_commits": [_SHA1],
            "artifacts_uri": "just a plain string",
            "state": "drafting",
            "created_at": _DT,
        },
        False,
    ),
    Case(
        "artifacts_uri_space_in_host",
        {
            "idea_id": "idea-13",
            "experiment_id": _EXP,
            "slug": "s",
            "priority": 0.0,
            "parent_commits": [_SHA1],
            "artifacts_uri": "http://exa mple.com/path",
            "state": "drafting",
            "created_at": _DT,
        },
        False,
    ),
    Case(
        "artifacts_uri_bad_percent_encoding",
        {
            "idea_id": "idea-14",
            "experiment_id": _EXP,
            "slug": "s",
            "priority": 0.0,
            "parent_commits": [_SHA1],
            "artifacts_uri": "http://example.com/bad%ZZ",
            "state": "drafting",
            "created_at": _DT,
        },
        False,
    ),
    Case(
        "created_at_impossible",
        {
            "idea_id": "idea-12",
            "experiment_id": _EXP,
            "slug": "s",
            "priority": 0.0,
            "parent_commits": [_SHA1],
            "artifacts_uri": "s3://b/",
            "state": "drafting",
            "created_at": "2026-13-32T25:00:00Z",
        },
        False,
    ),
    Case(
        "created_by_ok",
        {
            "idea_id": "idea-15",
            "experiment_id": _EXP,
            "slug": "s",
            "priority": 0.0,
            "parent_commits": [_SHA1],
            "artifacts_uri": "s3://b/",
            "state": "drafting",
            "created_at": _DT,
            "created_by": _WKR,
        },
        True,
    ),
    Case(
        "created_by_legacy_kebab",
        {
            "idea_id": "idea-16",
            "experiment_id": _EXP,
            "slug": "s",
            "priority": 0.0,
            "parent_commits": [_SHA1],
            "artifacts_uri": "s3://b/",
            "state": "drafting",
            "created_at": _DT,
            "created_by": "ideator-a",
        },
        False,
    ),
    Case(
        "created_by_wrong_prefix",
        {
            "idea_id": "idea-16b",
            "experiment_id": _EXP,
            "slug": "s",
            "priority": 0.0,
            "parent_commits": [_SHA1],
            "artifacts_uri": "s3://b/",
            "state": "drafting",
            "created_at": _DT,
            "created_by": _GRP,
        },
        False,
    ),
    Case(
        "experiment_id_legacy_kebab",
        {
            "idea_id": "idea-16c",
            "experiment_id": "exp-1",
            "slug": "s",
            "priority": 0.0,
            "parent_commits": [_SHA1],
            "artifacts_uri": "s3://b/",
            "state": "drafting",
            "created_at": _DT,
        },
        False,
    ),
    Case(
        "created_by_null",
        {
            "idea_id": "idea-17",
            "experiment_id": _EXP,
            "slug": "s",
            "priority": 0.0,
            "parent_commits": [_SHA1],
            "artifacts_uri": "s3://b/",
            "state": "drafting",
            "created_at": _DT,
            "created_by": None,
        },
        False,
    ),
    Case(
        "intended_executor_worker",
        # 12a-3: the ideator MAY tag a routing hint that the
        # orchestrator's execution_dispatch copies to task.target.
        {
            "idea_id": "idea-18",
            "experiment_id": _EXP,
            "slug": "s",
            "priority": 0.0,
            "parent_commits": [_SHA1],
            "artifacts_uri": "s3://b/",
            "state": "drafting",
            "created_at": _DT,
            "intended_executor": {"kind": "worker", "id": _WKR},
        },
        True,
    ),
    Case(
        "intended_executor_group",
        {
            "idea_id": "idea-19",
            "experiment_id": _EXP,
            "slug": "s",
            "priority": 0.0,
            "parent_commits": [_SHA1],
            "artifacts_uri": "s3://b/",
            "state": "drafting",
            "created_at": _DT,
            "intended_executor": {"kind": "group", "id": _GRP},
        },
        True,
    ),
    Case(
        "intended_executor_bad_kind",
        {
            "idea_id": "idea-20",
            "experiment_id": _EXP,
            "slug": "s",
            "priority": 0.0,
            "parent_commits": [_SHA1],
            "artifacts_uri": "s3://b/",
            "state": "drafting",
            "created_at": _DT,
            "intended_executor": {"kind": "team", "id": _GRP},
        },
        False,
    ),
    Case(
        "intended_executor_null",
        {
            "idea_id": "idea-21",
            "experiment_id": _EXP,
            "slug": "s",
            "priority": 0.0,
            "parent_commits": [_SHA1],
            "artifacts_uri": "s3://b/",
            "state": "drafting",
            "created_at": _DT,
            "intended_executor": None,
        },
        False,
    ),
    Case(
        "intended_evaluator_worker",
        {
            "idea_id": "idea-22",
            "experiment_id": _EXP,
            "slug": "s",
            "priority": 0.0,
            "parent_commits": [_SHA1],
            "artifacts_uri": "s3://b/",
            "state": "drafting",
            "created_at": _DT,
            "intended_evaluator": {"kind": "worker", "id": _WKR},
        },
        True,
    ),
    Case(
        "intended_evaluator_group",
        {
            "idea_id": "idea-23",
            "experiment_id": _EXP,
            "slug": "s",
            "priority": 0.0,
            "parent_commits": [_SHA1],
            "artifacts_uri": "s3://b/",
            "state": "drafting",
            "created_at": _DT,
            "intended_evaluator": {"kind": "group", "id": _GRP},
        },
        True,
    ),
    Case(
        "intended_evaluator_bad_kind",
        {
            "idea_id": "idea-24",
            "experiment_id": _EXP,
            "slug": "s",
            "priority": 0.0,
            "parent_commits": [_SHA1],
            "artifacts_uri": "s3://b/",
            "state": "drafting",
            "created_at": _DT,
            "intended_evaluator": {"kind": "team", "id": _GRP},
        },
        False,
    ),
    Case(
        "intended_evaluator_null",
        {
            "idea_id": "idea-25",
            "experiment_id": _EXP,
            "slug": "s",
            "priority": 0.0,
            "parent_commits": [_SHA1],
            "artifacts_uri": "s3://b/",
            "state": "drafting",
            "created_at": _DT,
            "intended_evaluator": None,
        },
        False,
    ),
    Case(
        "both_intendeds_set",
        {
            "idea_id": "idea-26",
            "experiment_id": _EXP,
            "slug": "s",
            "priority": 0.0,
            "parent_commits": [_SHA1],
            "artifacts_uri": "s3://b/",
            "state": "drafting",
            "created_at": _DT,
            "intended_executor": {"kind": "worker", "id": _WKR},
            "intended_evaluator": {"kind": "worker", "id": _WKR2},
        },
        True,
    ),
]


VARIANT_CASES: list[Case] = [
    Case(
        "starting_minimal",
        {
            "variant_id": "variant-1",
            "experiment_id": _EXP,
            "idea_id": "idea-1",
            "status": "starting",
            "parent_commits": [_SHA1],
            "started_at": _DT,
        },
        True,
    ),
    Case(
        "success_full",
        {
            "variant_id": "variant-2",
            "experiment_id": _EXP,
            "idea_id": "idea-1",
            "status": "success",
            "parent_commits": [_SHA1],
            "branch": "work/variant-2",
            "commit_sha": "b" * 40,
            "variant_commit_sha": "c" * 40,
            "artifacts_uri": "s3://bucket/variant-2",
            "description": "improves accuracy",
            "evaluation": {"accuracy": 0.91, "tokens": 12345},
            "started_at": _DT,
            "completed_at": _DT2,
        },
        True,
    ),
    Case(
        "evaluation_error",
        {
            "variant_id": "variant-3",
            "experiment_id": _EXP,
            "idea_id": "idea-1",
            "status": "evaluation_error",
            "parent_commits": [_SHA1],
            "started_at": _DT,
        },
        True,
    ),
    Case(
        "branch_not_work",
        {
            "variant_id": "variant-4",
            "experiment_id": _EXP,
            "idea_id": "idea-1",
            "status": "starting",
            "parent_commits": [_SHA1],
            "branch": "feature/foo",
            "started_at": _DT,
        },
        False,
    ),
    Case(
        "bad_status",
        {
            "variant_id": "variant-5",
            "experiment_id": _EXP,
            "idea_id": "idea-1",
            "status": "running",
            "parent_commits": [_SHA1],
            "started_at": _DT,
        },
        False,
    ),
    Case(
        "bad_commit_sha",
        {
            "variant_id": "variant-6",
            "experiment_id": _EXP,
            "idea_id": "idea-1",
            "status": "success",
            "parent_commits": [_SHA1],
            "commit_sha": "not-hex",
            "started_at": _DT,
        },
        False,
    ),
    Case(
        "empty_parent_commits",
        {
            "variant_id": "variant-7",
            "experiment_id": _EXP,
            "idea_id": "idea-1",
            "status": "starting",
            "parent_commits": [],
            "started_at": _DT,
        },
        False,
    ),
    Case(
        "metrics_explicit_null",
        {
            "variant_id": "variant-8",
            "experiment_id": _EXP,
            "idea_id": "idea-1",
            "status": "starting",
            "parent_commits": [_SHA1],
            "evaluation": None,
            "started_at": _DT,
        },
        False,
    ),
    Case(
        "artifacts_uri_no_scheme",
        {
            "variant_id": "variant-9",
            "experiment_id": _EXP,
            "idea_id": "idea-1",
            "status": "success",
            "parent_commits": [_SHA1],
            "artifacts_uri": "not a uri",
            "started_at": _DT,
        },
        False,
    ),
    Case(
        "artifacts_uri_space_in_host",
        {
            "variant_id": "variant-11",
            "experiment_id": _EXP,
            "idea_id": "idea-1",
            "status": "success",
            "parent_commits": [_SHA1],
            "artifacts_uri": "http://exa mple.com/path",
            "started_at": _DT,
        },
        False,
    ),
    Case(
        "completed_at_null",
        {
            "variant_id": "variant-10",
            "experiment_id": _EXP,
            "idea_id": "idea-1",
            "status": "starting",
            "parent_commits": [_SHA1],
            "started_at": _DT,
            "completed_at": None,
        },
        False,
    ),
    Case(
        "executed_and_evaluated_by_ok",
        {
            "variant_id": "variant-12",
            "experiment_id": _EXP,
            "idea_id": "idea-1",
            "status": "success",
            "parent_commits": [_SHA1],
            "branch": "work/v-12",
            "commit_sha": "b" * 40,
            "evaluation": {"accuracy": 0.9},
            "executed_by": _WKR,
            "evaluated_by": _WKR2,
            "started_at": _DT,
            "completed_at": _DT2,
        },
        True,
    ),
    Case(
        "executed_by_legacy_kebab",
        {
            "variant_id": "variant-13",
            "experiment_id": _EXP,
            "idea_id": "idea-1",
            "status": "starting",
            "parent_commits": [_SHA1],
            "started_at": _DT,
            "executed_by": "exec-a",
        },
        False,
    ),
    Case(
        "executed_by_wrong_prefix",
        {
            "variant_id": "variant-13b",
            "experiment_id": _EXP,
            "idea_id": "idea-1",
            "status": "starting",
            "parent_commits": [_SHA1],
            "started_at": _DT,
            "executed_by": _GRP,
        },
        False,
    ),
    Case(
        "experiment_id_legacy_kebab",
        {
            "variant_id": "variant-13c",
            "experiment_id": "exp-1",
            "idea_id": "idea-1",
            "status": "starting",
            "parent_commits": [_SHA1],
            "started_at": _DT,
        },
        False,
    ),
    Case(
        "evaluated_by_null",
        {
            "variant_id": "variant-14",
            "experiment_id": _EXP,
            "idea_id": "idea-1",
            "status": "starting",
            "parent_commits": [_SHA1],
            "started_at": _DT,
            "evaluated_by": None,
        },
        False,
    ),
    Case(
        "executor_artifacts_uri_ok",
        {
            "variant_id": "variant-15",
            "experiment_id": _EXP,
            "idea_id": "idea-1",
            "status": "success",
            "parent_commits": [_SHA1],
            "commit_sha": "b" * 40,
            "artifacts_uri": "s3://bucket/eval/v-15",
            "executor_artifacts_uri": "s3://bucket/exec/v-15",
            "started_at": _DT,
            "completed_at": _DT2,
        },
        True,
    ),
    Case(
        "executor_artifacts_uri_no_scheme",
        {
            "variant_id": "variant-16",
            "experiment_id": _EXP,
            "idea_id": "idea-1",
            "status": "success",
            "parent_commits": [_SHA1],
            "executor_artifacts_uri": "not a uri",
            "started_at": _DT,
        },
        False,
    ),
    Case(
        "executor_artifacts_uri_null",
        {
            "variant_id": "variant-17",
            "experiment_id": _EXP,
            "idea_id": "idea-1",
            "status": "starting",
            "parent_commits": [_SHA1],
            "started_at": _DT,
            "executor_artifacts_uri": None,
        },
        False,
    ),
    # --- Baseline variants (kind == "baseline"; 02-data-model.md §9.4) ---
    Case(
        "baseline_starting_no_idea_id",
        {
            "variant_id": "baseline",
            "experiment_id": _EXP,
            "kind": "baseline",
            "status": "starting",
            "parent_commits": [_SHA1],
            "commit_sha": _SHA1,
            "started_at": _DT,
        },
        True,
    ),
    Case(
        "baseline_success_with_evaluation",
        {
            "variant_id": "baseline",
            "experiment_id": _EXP,
            "kind": "baseline",
            "status": "success",
            "parent_commits": [_SHA1],
            "commit_sha": _SHA1,
            "evaluation": {"accuracy": 0.5},
            "started_at": _DT,
            "completed_at": _DT2,
        },
        True,
    ),
    Case(
        "baseline_may_carry_idea_id",
        {
            "variant_id": "baseline",
            "experiment_id": _EXP,
            "kind": "baseline",
            "idea_id": "idea-1",
            "status": "starting",
            "parent_commits": [_SHA1],
            "commit_sha": _SHA1,
            "started_at": _DT,
        },
        True,
    ),
    Case(
        "ordinary_variant_missing_idea_id_rejected",
        {
            "variant_id": "variant-18",
            "experiment_id": "exp-1",
            "status": "starting",
            "parent_commits": [_SHA1],
            "started_at": _DT,
        },
        False,
    ),
    Case(
        "unknown_kind_rejected",
        {
            "variant_id": "variant-19",
            "experiment_id": "exp-1",
            "kind": "experimental",
            "status": "starting",
            "parent_commits": [_SHA1],
            "started_at": _DT,
        },
        False,
    ),
    Case(
        "baseline_explicit_null_kind_rejected",
        {
            "variant_id": "variant-20",
            "experiment_id": "exp-1",
            "kind": None,
            "idea_id": "idea-1",
            "status": "starting",
            "parent_commits": [_SHA1],
            "started_at": _DT,
        },
        False,
    ),
]


WORKER_CASES: list[Case] = [
    Case(
        "minimal",
        {
            "worker_id": _WKR,
            "experiment_id": _EXP,
            "registered_at": _DT,
        },
        True,
    ),
    Case(
        "full",
        {
            "worker_id": _WKR,
            "name": "Eric (laptop)",
            "experiment_id": _EXP,
            "registered_at": _DT,
            "registered_by": "admin",
            "labels": {"role": "executor", "model": "claude-opus-4-7"},
        },
        True,
    ),
    Case(
        "registered_by_worker_actor_ok",
        {
            "worker_id": _WKR,
            "experiment_id": _EXP,
            "registered_at": _DT,
            "registered_by": _WKR2,
        },
        True,
    ),
    # --- worker_id grammar ---
    Case(
        "id_legacy_kebab",
        {
            "worker_id": "executor-host-1",
            "experiment_id": _EXP,
            "registered_at": _DT,
        },
        False,
    ),
    Case(
        "id_wrong_prefix",
        {
            "worker_id": _GRP,
            "experiment_id": _EXP,
            "registered_at": _DT,
        },
        False,
    ),
    Case(
        "id_suffix_too_short",
        {
            "worker_id": f"wkr_{_ULID_SHORT}",
            "experiment_id": _EXP,
            "registered_at": _DT,
        },
        False,
    ),
    Case(
        "id_suffix_too_long",
        {
            "worker_id": f"wkr_{_ULID_LONG}",
            "experiment_id": _EXP,
            "registered_at": _DT,
        },
        False,
    ),
    Case(
        "id_bad_crockford_char",
        {
            "worker_id": f"wkr_{_ULID_BAD_CHAR}",
            "experiment_id": _EXP,
            "registered_at": _DT,
        },
        False,
    ),
    Case(
        "id_uppercase_suffix",
        {
            "worker_id": f"wkr_{_ULID.upper()}",
            "experiment_id": _EXP,
            "registered_at": _DT,
        },
        False,
    ),
    # --- experiment_id grammar ---
    Case(
        "experiment_id_legacy_kebab",
        {
            "worker_id": _WKR,
            "experiment_id": "exp-1",
            "registered_at": _DT,
        },
        False,
    ),
    # --- registered_by (actor) grammar ---
    Case(
        "registered_by_legacy_kebab",
        {
            "worker_id": _WKR,
            "experiment_id": _EXP,
            "registered_at": _DT,
            "registered_by": "admin-eric",
        },
        False,
    ),
    Case(
        "registered_by_wrong_prefix",
        {
            "worker_id": _WKR,
            "experiment_id": _EXP,
            "registered_at": _DT,
            "registered_by": _GRP,
        },
        False,
    ),
    # --- name (display-name) grammar ---
    Case(
        "name_empty",
        {
            "worker_id": _WKR,
            "name": "",
            "experiment_id": _EXP,
            "registered_at": _DT,
        },
        False,
    ),
    Case(
        "name_too_long",
        {
            "worker_id": _WKR,
            "name": "x" * 129,
            "experiment_id": _EXP,
            "registered_at": _DT,
        },
        False,
    ),
    Case(
        "name_leading_whitespace",
        {
            "worker_id": _WKR,
            "name": " Eric",
            "experiment_id": _EXP,
            "registered_at": _DT,
        },
        False,
    ),
    Case(
        "name_control_char",
        {
            "worker_id": _WKR,
            "name": "Eric\tlaptop",
            "experiment_id": _EXP,
            "registered_at": _DT,
        },
        False,
    ),
    Case(
        "name_null",
        {
            "worker_id": _WKR,
            "name": None,
            "experiment_id": _EXP,
            "registered_at": _DT,
        },
        False,
    ),
    Case(
        "labels_non_string_value",
        {
            "worker_id": _WKR,
            "experiment_id": _EXP,
            "registered_at": _DT,
            "labels": {"count": 3},
        },
        False,
    ),
    Case(
        "missing_registered_at",
        {
            "worker_id": _WKR,
            "experiment_id": _EXP,
        },
        False,
    ),
]


GROUP_CASES: list[Case] = [
    Case(
        "minimal_empty_members",
        {
            "group_id": _GRP,
            "experiment_id": _EXP,
            "members": [],
            "created_at": _DT,
        },
        True,
    ),
    Case(
        "with_members_and_name",
        {
            "group_id": _GRP,
            "name": "Team A",
            "experiment_id": _EXP,
            "members": [_WKR, _WKR2, _GRP],
            "created_at": _DT,
            "created_by": "admin",
        },
        True,
    ),
    Case(
        "created_by_worker_actor_ok",
        {
            "group_id": _GRP,
            "experiment_id": _EXP,
            "members": [],
            "created_at": _DT,
            "created_by": _WKR,
        },
        True,
    ),
    # --- group_id grammar ---
    Case(
        "id_legacy_kebab",
        {
            "group_id": "humans",
            "experiment_id": _EXP,
            "members": [],
            "created_at": _DT,
        },
        False,
    ),
    Case(
        "id_wrong_prefix",
        {
            "group_id": _WKR,
            "experiment_id": _EXP,
            "members": [],
            "created_at": _DT,
        },
        False,
    ),
    Case(
        "id_bad_crockford_char",
        {
            "group_id": f"grp_{_ULID_BAD_CHAR}",
            "experiment_id": _EXP,
            "members": [],
            "created_at": _DT,
        },
        False,
    ),
    # --- members (member) grammar ---
    Case(
        "member_legacy_kebab",
        {
            "group_id": _GRP,
            "experiment_id": _EXP,
            "members": ["eric"],
            "created_at": _DT,
        },
        False,
    ),
    Case(
        "member_wrong_prefix",
        {
            "group_id": _GRP,
            "experiment_id": _EXP,
            "members": [f"exp_{_ULID}"],
            "created_at": _DT,
        },
        False,
    ),
    Case(
        "member_suffix_too_short",
        {
            "group_id": _GRP,
            "experiment_id": _EXP,
            "members": [f"wkr_{_ULID_SHORT}"],
            "created_at": _DT,
        },
        False,
    ),
    # --- created_by (actor) grammar ---
    Case(
        "created_by_wrong_prefix",
        {
            "group_id": _GRP,
            "experiment_id": _EXP,
            "members": [],
            "created_at": _DT,
            "created_by": _GRP,
        },
        False,
    ),
    # --- name (display-name) grammar ---
    Case(
        "name_empty",
        {
            "group_id": _GRP,
            "name": "",
            "experiment_id": _EXP,
            "members": [],
            "created_at": _DT,
        },
        False,
    ),
    Case(
        "name_trailing_whitespace",
        {
            "group_id": _GRP,
            "name": "Team A ",
            "experiment_id": _EXP,
            "members": [],
            "created_at": _DT,
        },
        False,
    ),
    # --- experiment_id grammar ---
    Case(
        "experiment_id_legacy_kebab",
        {
            "group_id": _GRP,
            "experiment_id": "exp-1",
            "members": [],
            "created_at": _DT,
        },
        False,
    ),
    Case(
        "missing_members",
        {
            "group_id": _GRP,
            "experiment_id": _EXP,
            "created_at": _DT,
        },
        False,
    ),
]


EVALUATION_SCHEMA_CASES: list[Case] = [
    Case("one_real", {"accuracy": "real"}, True),
    Case("mixed", {"accuracy": "real", "tokens": "integer", "note": "text"}, True),
    Case("leading_underscore", {"_internal": "integer"}, True),
    Case("empty", {}, False),
    Case("reserved_variant_id", {"variant_id": "text"}, False),
    Case("reserved_completed_at", {"completed_at": "text"}, False),
    Case("invalid_type", {"x": "float"}, False),
    Case("numeric_first_char", {"1accuracy": "real"}, False),
    Case("hyphen_in_name", {"accu-racy": "real"}, False),
]


EXPERIMENT_CASES: list[Case] = [
    Case(
        "running_minimal",
        {"experiment_id": _EXP, "state": "running", "created_at": _DT},
        True,
    ),
    Case(
        "with_base_commit_sha",
        {
            "experiment_id": _EXP,
            "state": "running",
            "created_at": _DT,
            "base_commit_sha": _SHA1,
        },
        True,
    ),
    Case(
        "base_commit_sha_sha256",
        {
            "experiment_id": _EXP,
            "state": "terminated",
            "created_at": _DT,
            "base_commit_sha": _SHA256,
        },
        True,
    ),
    Case(
        "base_commit_sha_explicit_null_rejected",
        {
            "experiment_id": _EXP,
            "state": "running",
            "created_at": _DT,
            "base_commit_sha": None,
        },
        False,
    ),
    Case(
        "base_commit_sha_malformed_rejected",
        {
            "experiment_id": _EXP,
            "state": "running",
            "created_at": _DT,
            "base_commit_sha": "not-a-sha",
        },
        False,
    ),
]


_HEX32 = "0" * 32

ARTIFACT_METADATA_CASES: list[Case] = [
    Case(
        "minimal",
        {
            "opaque_id": _HEX32,
            "created_by": "eric",
            "size_bytes": 1024,
            "content_type": "application/gzip",
            "created_at": _DT,
        },
        True,
    ),
    Case(
        "admin_depositor",
        {
            "opaque_id": "a1b2c3d4" * 4,
            "created_by": "admin",
            "size_bytes": 0,
            "content_type": "text/markdown",
            "created_at": _DT2,
        },
        True,
    ),
    Case(
        "opaque_id_too_short",
        {
            "opaque_id": "0" * 31,
            "created_by": "eric",
            "size_bytes": 1,
            "content_type": "text/plain",
            "created_at": _DT,
        },
        False,
    ),
    Case(
        "opaque_id_uppercase_hex",
        {
            "opaque_id": "A" * 32,
            "created_by": "eric",
            "size_bytes": 1,
            "content_type": "text/plain",
            "created_at": _DT,
        },
        False,
    ),
    Case(
        "opaque_id_with_path_separator",
        {
            "opaque_id": "0" * 30 + "/0",
            "created_by": "eric",
            "size_bytes": 1,
            "content_type": "text/plain",
            "created_at": _DT,
        },
        False,
    ),
    Case(
        "created_by_empty",
        {
            "opaque_id": _HEX32,
            "created_by": "",
            "size_bytes": 1,
            "content_type": "text/plain",
            "created_at": _DT,
        },
        False,
    ),
    Case(
        "size_bytes_negative",
        {
            "opaque_id": _HEX32,
            "created_by": "eric",
            "size_bytes": -1,
            "content_type": "text/plain",
            "created_at": _DT,
        },
        False,
    ),
    Case(
        "content_type_empty",
        {
            "opaque_id": _HEX32,
            "created_by": "eric",
            "size_bytes": 1,
            "content_type": "",
            "created_at": _DT,
        },
        False,
    ),
    Case(
        "missing_created_at",
        {
            "opaque_id": _HEX32,
            "created_by": "eric",
            "size_bytes": 1,
            "content_type": "text/plain",
        },
        False,
    ),
]


ALL_CASES: dict[str, list[Case]] = {
    "experiment-config": EXPERIMENT_CONFIG_CASES,
    "experiment": EXPERIMENT_CASES,
    "task": TASK_CASES,
    "event": EVENT_CASES,
    "idea": IDEA_CASES,
    "variant": VARIANT_CASES,
    "evaluation-schema": EVALUATION_SCHEMA_CASES,
    "worker": WORKER_CASES,
    "group": GROUP_CASES,
    "artifact-metadata": ARTIFACT_METADATA_CASES,
}
