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

EXPERIMENT_CONFIG_CASES: list[Case] = [
    Case(
        "minimal",
        {
            "parallel_variants": 2,
            "max_variants": 50,
            "max_wall_time": "4h",
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
        },
        True,
    ),
    Case(
        "with_optional",
        {
            "parallel_variants": 4,
            "max_variants": 200,
            "max_wall_time": "30m",
            "evaluation_schema": {"loss": "real", "tokens": "integer", "note": "text"},
            "objective": {"expr": "loss", "direction": "minimize"},
            "convergence_window": 20,
            "target_condition": "loss < 0.01",
        },
        True,
    ),
    Case(
        "missing_parallel_variants",
        {
            "max_variants": 50,
            "max_wall_time": "4h",
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
        },
        False,
    ),
    Case(
        "parallel_variants_zero",
        {
            "parallel_variants": 0,
            "max_variants": 50,
            "max_wall_time": "4h",
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
        },
        False,
    ),
    Case(
        "wall_time_zero_prefix",
        {
            "parallel_variants": 2,
            "max_variants": 50,
            "max_wall_time": "0s",
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
        },
        False,
    ),
    Case(
        "wall_time_bad_unit",
        {
            "parallel_variants": 2,
            "max_variants": 50,
            "max_wall_time": "4y",
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
        },
        False,
    ),
    Case(
        "evaluation_schema_empty",
        {
            "parallel_variants": 2,
            "max_variants": 50,
            "max_wall_time": "4h",
            "evaluation_schema": {},
            "objective": {"expr": "accuracy", "direction": "maximize"},
        },
        False,
    ),
    Case(
        "evaluation_schema_reserved_key",
        {
            "parallel_variants": 2,
            "max_variants": 50,
            "max_wall_time": "4h",
            "evaluation_schema": {"variant_id": "text"},
            "objective": {"expr": "variant_id", "direction": "maximize"},
        },
        False,
    ),
    Case(
        "objective_invalid_direction",
        {
            "parallel_variants": 2,
            "max_variants": 50,
            "max_wall_time": "4h",
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "sideways"},
        },
        False,
    ),
    Case(
        "parallel_variants_bool",
        {
            "parallel_variants": True,
            "max_variants": 50,
            "max_wall_time": "4h",
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
        },
        False,
    ),
    Case(
        "max_variants_string",
        {
            "parallel_variants": 2,
            "max_variants": "50",
            "max_wall_time": "4h",
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
        },
        False,
    ),
    Case(
        "convergence_window_string",
        {
            "parallel_variants": 2,
            "max_variants": 50,
            "max_wall_time": "4h",
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "convergence_window": "3",
        },
        False,
    ),
    Case(
        "convergence_window_null",
        {
            "parallel_variants": 2,
            "max_variants": 50,
            "max_wall_time": "4h",
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "convergence_window": None,
        },
        False,
    ),
    Case(
        "dispatch_mode_all_auto",
        {
            "parallel_variants": 2,
            "max_variants": 50,
            "max_wall_time": "4h",
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "dispatch_mode": {
                "ideation_creation": "auto",
                "execution_dispatch": "auto",
                "evaluation_dispatch": "auto",
                "integration": "auto",
            },
        },
        True,
    ),
    Case(
        "dispatch_mode_mixed",
        {
            "parallel_variants": 2,
            "max_variants": 50,
            "max_wall_time": "4h",
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "dispatch_mode": {
                "evaluation_dispatch": "manual",
                "integration": "manual",
            },
        },
        True,
    ),
    Case(
        "dispatch_mode_unknown_key_tolerated",
        {
            "parallel_variants": 2,
            "max_variants": 50,
            "max_wall_time": "4h",
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            # Per 02-data-model.md §2.5: unknown keys are tolerated
            # by the schema and ignored by conforming implementations.
            "dispatch_mode": {"future_decision": "auto"},
        },
        True,
    ),
    Case(
        "dispatch_mode_invalid_value",
        {
            "parallel_variants": 2,
            "max_variants": 50,
            "max_wall_time": "4h",
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
            "max_variants": 50,
            "max_wall_time": "4h",
            "evaluation_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "dispatch_mode": None,
        },
        False,
    ),
]


_VALID_CLAIM = {
    "worker_id": "worker-a",
    "claimed_at": _DT,
}

TASK_CASES: list[Case] = [
    Case(
        "ideation_pending",
        {
            "task_id": "t-1",
            "kind": "ideation",
            "state": "pending",
            "payload": {"experiment_id": "exp-1"},
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
            "payload": {"experiment_id": "exp-1"},
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
            "payload": {"experiment_id": "exp-1"},
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
            "payload": {"experiment_id": "exp-1"},
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
            "payload": {"experiment_id": "exp-1"},
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
            "payload": {"experiment_id": "exp-1"},
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
            "payload": {"experiment_id": "exp-1"},
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
            "payload": {"experiment_id": "exp-1"},
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
            "payload": {"experiment_id": "exp-1"},
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
            "payload": {"experiment_id": "exp-1"},
            "claim": {"worker_id": "w", "claimed_at": "2026-04-23 12:00:00"},
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
            "payload": {"experiment_id": "exp-1"},
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
            "payload": {"experiment_id": "exp-1"},
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
            "payload": {"experiment_id": "exp-1"},
            "target": {"kind": "worker", "id": "eric"},
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
            "target": {"kind": "group", "id": "humans"},
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
            "submitted_by": "evaluator-a",
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
            "payload": {"experiment_id": "exp-1"},
            "target": {"kind": "anyone", "id": "eric"},
            "created_at": _DT,
            "updated_at": _DT,
        },
        False,
    ),
    Case(
        "target_id_uppercase",
        {
            "task_id": "t-22",
            "kind": "ideation",
            "state": "pending",
            "payload": {"experiment_id": "exp-1"},
            "target": {"kind": "worker", "id": "Eric"},
            "created_at": _DT,
            "updated_at": _DT,
        },
        False,
    ),
    Case(
        "target_id_leading_hyphen",
        {
            "task_id": "t-23",
            "kind": "ideation",
            "state": "pending",
            "payload": {"experiment_id": "exp-1"},
            "target": {"kind": "worker", "id": "-eric"},
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
            "payload": {"experiment_id": "exp-1"},
            "target": {"kind": "worker"},
            "created_at": _DT,
            "updated_at": _DT,
        },
        False,
    ),
    Case(
        "submitted_by_uppercase",
        {
            "task_id": "t-25",
            "kind": "ideation",
            "state": "completed",
            "payload": {"experiment_id": "exp-1"},
            "submitted_by": "Worker-A",
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
            "payload": {"experiment_id": "exp-1"},
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
            "experiment_id": "exp-1",
            "data": {"note": "anything"},
        },
        True,
    ),
    Case(
        "type_without_dot",
        {
            "event_id": "evt-bad-1",
            "type": "claimed",
            "occurred_at": _DT,
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
            "data": {"task_id": "t-1", "worker_id": "w-1"},
        },
        False,
    ),
    Case(
        "missing_experiment_id",
        {
            "event_id": "evt-bad-4",
            "type": "task.claimed",
            "occurred_at": _DT,
            "data": {"task_id": "t-1", "worker_id": "w-1"},
        },
        False,
    ),
    Case(
        "data_not_object",
        {
            "event_id": "evt-bad-5",
            "type": "task.claimed",
            "occurred_at": _DT,
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
            "data": {"task_id": "t-1", "worker_id": "w-1"},
        },
        True,
    ),
    Case(
        "task_claimed_missing_worker",
        {
            "event_id": "evt-tcl-2",
            "type": "task.claimed",
            "occurred_at": _DT,
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
            "data": {
                "task_id": "t-1",
                "new_target": {"kind": "worker", "id": "eric"},
                "reason": "operator",
                "reassigned_by": "admin-eric",
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
            "experiment_id": "exp-1",
            "data": {
                "task_id": "t-2",
                "new_target": {"kind": "group", "id": "humans"},
                "reason": "misrouted",
                "reassigned_by": "ops-1",
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
            "experiment_id": "exp-1",
            "data": {
                "task_id": "t-3",
                "new_target": None,
                "reason": "open up to any worker",
                "reassigned_by": "ops-1",
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
            "experiment_id": "exp-1",
            "data": {
                "task_id": "t-4",
                "reason": "operator",
                "reassigned_by": "ops-1",
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
            "experiment_id": "exp-1",
            "data": {
                "task_id": "t-5",
                "new_target": None,
                "reason": "",
                "reassigned_by": "ops-1",
            },
        },
        False,
    ),
    Case(
        "task_reassigned_actor_uppercase",
        {
            "event_id": "evt-tre-6",
            "type": "task.reassigned",
            "occurred_at": _DT,
            "experiment_id": "exp-1",
            "data": {
                "task_id": "t-6",
                "new_target": None,
                "reason": "operator",
                "reassigned_by": "Admin-Eric",
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
            "experiment_id": "exp-1",
            "data": {
                "task_id": "t-7",
                "new_target": {"kind": "anyone", "id": "eric"},
                "reason": "operator",
                "reassigned_by": "ops-1",
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
            "experiment_id": "exp-1",
            "data": {
                "dispatch_mode": {
                    "ideation_creation": "auto",
                    "execution_dispatch": "auto",
                    "evaluation_dispatch": "manual",
                    "integration": "auto",
                },
                "changed": {"evaluation_dispatch": "manual"},
                "updated_by": "admin-eric",
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
            "experiment_id": "exp-1",
            "data": {
                "dispatch_mode": {
                    "ideation_creation": "auto",
                    "execution_dispatch": "auto",
                    "evaluation_dispatch": "auto",
                    "integration": "auto",
                },
                "updated_by": "admin-eric",
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
            "experiment_id": "exp-1",
            "data": {
                "dispatch_mode": {"ideation_creation": "paused"},
                "changed": {"ideation_creation": "paused"},
                "updated_by": "admin-eric",
            },
        },
        False,
    ),
    Case(
        "experiment_dispatch_mode_changed_actor_uppercase",
        {
            "event_id": "evt-edm-4",
            "type": "experiment.dispatch_mode_changed",
            "occurred_at": _DT,
            "experiment_id": "exp-1",
            "data": {
                "dispatch_mode": {"integration": "manual"},
                "changed": {"integration": "manual"},
                "updated_by": "Admin",
            },
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
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
            "data": {"variant_id": "variant-1", "idea_id": "idea-1"},
        },
        True,
    ),
    Case(
        "variant_succeeded_ok",
        {
            "event_id": "evt-ts-200",
            "type": "variant.succeeded",
            "occurred_at": _DT,
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
            "slug": "s",
            "priority": 0.0,
            "parent_commits": [_SHA1],
            "artifacts_uri": "s3://b/",
            "state": "drafting",
            "created_at": _DT,
            "created_by": "ideator-a",
        },
        True,
    ),
    Case(
        "created_by_uppercase",
        {
            "idea_id": "idea-16",
            "experiment_id": "exp-1",
            "slug": "s",
            "priority": 0.0,
            "parent_commits": [_SHA1],
            "artifacts_uri": "s3://b/",
            "state": "drafting",
            "created_at": _DT,
            "created_by": "Ideator-A",
        },
        False,
    ),
    Case(
        "created_by_null",
        {
            "idea_id": "idea-17",
            "experiment_id": "exp-1",
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
]


VARIANT_CASES: list[Case] = [
    Case(
        "starting_minimal",
        {
            "variant_id": "variant-1",
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
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
            "experiment_id": "exp-1",
            "idea_id": "idea-1",
            "status": "success",
            "parent_commits": [_SHA1],
            "branch": "work/v-12",
            "commit_sha": "b" * 40,
            "evaluation": {"accuracy": 0.9},
            "executed_by": "exec-a",
            "evaluated_by": "eval-b",
            "started_at": _DT,
            "completed_at": _DT2,
        },
        True,
    ),
    Case(
        "executed_by_uppercase",
        {
            "variant_id": "variant-13",
            "experiment_id": "exp-1",
            "idea_id": "idea-1",
            "status": "starting",
            "parent_commits": [_SHA1],
            "started_at": _DT,
            "executed_by": "Exec-A",
        },
        False,
    ),
    Case(
        "evaluated_by_null",
        {
            "variant_id": "variant-14",
            "experiment_id": "exp-1",
            "idea_id": "idea-1",
            "status": "starting",
            "parent_commits": [_SHA1],
            "started_at": _DT,
            "evaluated_by": None,
        },
        False,
    ),
]


WORKER_CASES: list[Case] = [
    Case(
        "minimal",
        {
            "worker_id": "eric",
            "experiment_id": "exp-1",
            "registered_at": _DT,
        },
        True,
    ),
    Case(
        "full",
        {
            "worker_id": "exec-a",
            "experiment_id": "exp-1",
            "registered_at": _DT,
            "registered_by": "admin",
            "labels": {"role": "executor", "model": "claude-opus-4-7"},
        },
        True,
    ),
    Case(
        "id_max_length",
        {
            "worker_id": "a" + "b" * 63,
            "experiment_id": "exp-1",
            "registered_at": _DT,
        },
        True,
    ),
    Case(
        "id_underscore_in_middle",
        {
            "worker_id": "exec_a",
            "experiment_id": "exp-1",
            "registered_at": _DT,
        },
        True,
    ),
    Case(
        "id_uppercase",
        {
            "worker_id": "Eric",
            "experiment_id": "exp-1",
            "registered_at": _DT,
        },
        False,
    ),
    Case(
        "id_leading_hyphen",
        {
            "worker_id": "-eric",
            "experiment_id": "exp-1",
            "registered_at": _DT,
        },
        False,
    ),
    Case(
        "id_leading_underscore",
        {
            "worker_id": "_internal",
            "experiment_id": "exp-1",
            "registered_at": _DT,
        },
        False,
    ),
    Case(
        "id_too_long",
        {
            "worker_id": "a" + "b" * 64,
            "experiment_id": "exp-1",
            "registered_at": _DT,
        },
        False,
    ),
    Case(
        "id_with_colon",
        {
            "worker_id": "eric:secret",
            "experiment_id": "exp-1",
            "registered_at": _DT,
        },
        False,
    ),
    Case(
        "labels_non_string_value",
        {
            "worker_id": "eric",
            "experiment_id": "exp-1",
            "registered_at": _DT,
            "labels": {"count": 3},
        },
        False,
    ),
    Case(
        "missing_registered_at",
        {
            "worker_id": "eric",
            "experiment_id": "exp-1",
        },
        False,
    ),
]


GROUP_CASES: list[Case] = [
    Case(
        "minimal_empty_members",
        {
            "group_id": "humans",
            "experiment_id": "exp-1",
            "members": [],
            "created_at": _DT,
        },
        True,
    ),
    Case(
        "with_members",
        {
            "group_id": "team-a",
            "experiment_id": "exp-1",
            "members": ["eric", "alice", "agents"],
            "created_at": _DT,
            "created_by": "admin",
        },
        True,
    ),
    Case(
        "id_uppercase",
        {
            "group_id": "Humans",
            "experiment_id": "exp-1",
            "members": [],
            "created_at": _DT,
        },
        False,
    ),
    Case(
        "member_uppercase",
        {
            "group_id": "team-b",
            "experiment_id": "exp-1",
            "members": ["Eric"],
            "created_at": _DT,
        },
        False,
    ),
    Case(
        "member_leading_hyphen",
        {
            "group_id": "team-c",
            "experiment_id": "exp-1",
            "members": ["-eric"],
            "created_at": _DT,
        },
        False,
    ),
    Case(
        "missing_members",
        {
            "group_id": "humans",
            "experiment_id": "exp-1",
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


ALL_CASES: dict[str, list[Case]] = {
    "experiment-config": EXPERIMENT_CONFIG_CASES,
    "task": TASK_CASES,
    "event": EVENT_CASES,
    "idea": IDEA_CASES,
    "variant": VARIANT_CASES,
    "evaluation-schema": EVALUATION_SCHEMA_CASES,
    "worker": WORKER_CASES,
    "group": GROUP_CASES,
}
