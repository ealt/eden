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
            "parallel_trials": 2,
            "max_trials": 50,
            "max_wall_time": "4h",
            "metrics_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
        },
        True,
    ),
    Case(
        "with_optional",
        {
            "parallel_trials": 4,
            "max_trials": 200,
            "max_wall_time": "30m",
            "metrics_schema": {"loss": "real", "tokens": "integer", "note": "text"},
            "objective": {"expr": "loss", "direction": "minimize"},
            "convergence_window": 20,
            "target_condition": "loss < 0.01",
        },
        True,
    ),
    Case(
        "missing_parallel_trials",
        {
            "max_trials": 50,
            "max_wall_time": "4h",
            "metrics_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
        },
        False,
    ),
    Case(
        "parallel_trials_zero",
        {
            "parallel_trials": 0,
            "max_trials": 50,
            "max_wall_time": "4h",
            "metrics_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
        },
        False,
    ),
    Case(
        "wall_time_zero_prefix",
        {
            "parallel_trials": 2,
            "max_trials": 50,
            "max_wall_time": "0s",
            "metrics_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
        },
        False,
    ),
    Case(
        "wall_time_bad_unit",
        {
            "parallel_trials": 2,
            "max_trials": 50,
            "max_wall_time": "4y",
            "metrics_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
        },
        False,
    ),
    Case(
        "metrics_schema_empty",
        {
            "parallel_trials": 2,
            "max_trials": 50,
            "max_wall_time": "4h",
            "metrics_schema": {},
            "objective": {"expr": "accuracy", "direction": "maximize"},
        },
        False,
    ),
    Case(
        "metrics_schema_reserved_key",
        {
            "parallel_trials": 2,
            "max_trials": 50,
            "max_wall_time": "4h",
            "metrics_schema": {"trial_id": "text"},
            "objective": {"expr": "trial_id", "direction": "maximize"},
        },
        False,
    ),
    Case(
        "objective_invalid_direction",
        {
            "parallel_trials": 2,
            "max_trials": 50,
            "max_wall_time": "4h",
            "metrics_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "sideways"},
        },
        False,
    ),
    Case(
        "parallel_trials_bool",
        {
            "parallel_trials": True,
            "max_trials": 50,
            "max_wall_time": "4h",
            "metrics_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
        },
        False,
    ),
    Case(
        "max_trials_string",
        {
            "parallel_trials": 2,
            "max_trials": "50",
            "max_wall_time": "4h",
            "metrics_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
        },
        False,
    ),
    Case(
        "convergence_window_string",
        {
            "parallel_trials": 2,
            "max_trials": 50,
            "max_wall_time": "4h",
            "metrics_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "convergence_window": "3",
        },
        False,
    ),
    Case(
        "convergence_window_null",
        {
            "parallel_trials": 2,
            "max_trials": 50,
            "max_wall_time": "4h",
            "metrics_schema": {"accuracy": "real"},
            "objective": {"expr": "accuracy", "direction": "maximize"},
            "convergence_window": None,
        },
        False,
    ),
]


_VALID_CLAIM = {
    "token": "claim-1",
    "worker_id": "worker-a",
    "claimed_at": _DT,
}

TASK_CASES: list[Case] = [
    Case(
        "plan_pending",
        {
            "task_id": "t-1",
            "kind": "plan",
            "state": "pending",
            "payload": {"experiment_id": "exp-1"},
            "created_at": _DT,
            "updated_at": _DT,
        },
        True,
    ),
    Case(
        "implement_claimed",
        {
            "task_id": "t-2",
            "kind": "implement",
            "state": "claimed",
            "payload": {"proposal_id": "p-1"},
            "claim": _VALID_CLAIM,
            "created_at": _DT,
            "updated_at": _DT,
        },
        True,
    ),
    Case(
        "evaluate_submitted",
        {
            "task_id": "t-3",
            "kind": "evaluate",
            "state": "submitted",
            "payload": {"trial_id": "trial-1"},
            "claim": {**_VALID_CLAIM, "expires_at": _DT2},
            "created_at": _DT,
            "updated_at": _DT2,
        },
        True,
    ),
    Case(
        "plan_completed_no_claim",
        {
            "task_id": "t-4",
            "kind": "plan",
            "state": "completed",
            "payload": {"experiment_id": "exp-1"},
            "created_at": _DT,
            "updated_at": _DT2,
        },
        True,
    ),
    Case(
        "plan_failed_no_claim",
        {
            "task_id": "t-5",
            "kind": "plan",
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
            "kind": "plan",
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
            "kind": "evaluate",
            "state": "submitted",
            "payload": {"trial_id": "trial-1"},
            "created_at": _DT,
            "updated_at": _DT,
        },
        False,
    ),
    Case(
        "pending_with_claim",
        {
            "task_id": "t-8",
            "kind": "plan",
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
            "kind": "plan",
            "state": "completed",
            "payload": {"experiment_id": "exp-1"},
            "claim": _VALID_CLAIM,
            "created_at": _DT,
            "updated_at": _DT,
        },
        False,
    ),
    Case(
        "plan_task_missing_experiment_id",
        {
            "task_id": "t-10",
            "kind": "plan",
            "state": "pending",
            "payload": {},
            "created_at": _DT,
            "updated_at": _DT,
        },
        False,
    ),
    Case(
        "implement_task_missing_proposal_id",
        {
            "task_id": "t-11",
            "kind": "implement",
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
            "kind": "plan",
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
            "kind": "plan",
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
            "kind": "plan",
            "state": "claimed",
            "payload": {"experiment_id": "exp-1"},
            "claim": {"token": "c", "worker_id": "w", "claimed_at": "2026-04-23 12:00:00"},
            "created_at": _DT,
            "updated_at": _DT,
        },
        False,
    ),
    Case(
        "impossible_datetime",
        {
            "task_id": "t-16",
            "kind": "plan",
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
            "kind": "plan",
            "state": "pending",
            "payload": {"experiment_id": "exp-1"},
            "claim": None,
            "created_at": _DT,
            "updated_at": _DT,
        },
        False,
    ),
]


EVENT_CASES: list[Case] = [
    Case(
        "task_claimed",
        {
            "event_id": "evt-1",
            "type": "task.claimed",
            "occurred_at": _DT,
            "experiment_id": "exp-1",
            "data": {"task_id": "t-1", "worker_id": "w-1"},
        },
        True,
    ),
    Case(
        "nested_type",
        {
            "event_id": "evt-2",
            "type": "trial.evaluated.success",
            "occurred_at": _DT2,
            "experiment_id": "exp-1",
            "data": {},
        },
        True,
    ),
    Case(
        "type_without_dot",
        {
            "event_id": "evt-3",
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
            "event_id": "evt-4",
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
            "event_id": "evt-5",
            "type": "task.claimed",
            "occurred_at": "2026-04-23T12:00:00+00:00",
            "experiment_id": "exp-1",
            "data": {},
        },
        False,
    ),
    Case(
        "missing_experiment_id",
        {
            "event_id": "evt-6",
            "type": "task.claimed",
            "occurred_at": _DT,
            "data": {},
        },
        False,
    ),
    Case(
        "data_not_object",
        {
            "event_id": "evt-7",
            "type": "task.claimed",
            "occurred_at": _DT,
            "experiment_id": "exp-1",
            "data": "hello",
        },
        False,
    ),
]


PROPOSAL_CASES: list[Case] = [
    Case(
        "drafting",
        {
            "proposal_id": "p-1",
            "experiment_id": "exp-1",
            "slug": "improve-tokenizer",
            "priority": 0.5,
            "parent_commits": [_SHA1],
            "artifacts_uri": "s3://bucket/proposals/p-1/",
            "state": "drafting",
            "created_at": _DT,
        },
        True,
    ),
    Case(
        "completed_sha256",
        {
            "proposal_id": "p-2",
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
            "proposal_id": "p-3",
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
            "proposal_id": "p-4",
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
            "proposal_id": "p-5",
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
            "proposal_id": "p-6",
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
            "proposal_id": "p-7",
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
            "proposal_id": "p-8",
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
            "proposal_id": "p-9",
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
            "proposal_id": "p-10",
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
            "proposal_id": "p-11",
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
            "proposal_id": "p-13",
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
            "proposal_id": "p-14",
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
            "proposal_id": "p-12",
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
]


TRIAL_CASES: list[Case] = [
    Case(
        "starting_minimal",
        {
            "trial_id": "trial-1",
            "experiment_id": "exp-1",
            "proposal_id": "p-1",
            "status": "starting",
            "parent_commits": [_SHA1],
            "started_at": _DT,
        },
        True,
    ),
    Case(
        "success_full",
        {
            "trial_id": "trial-2",
            "experiment_id": "exp-1",
            "proposal_id": "p-1",
            "status": "success",
            "parent_commits": [_SHA1],
            "branch": "work/trial-2",
            "commit_sha": "b" * 40,
            "trial_commit_sha": "c" * 40,
            "artifacts_uri": "s3://bucket/trial-2",
            "description": "improves accuracy",
            "metrics": {"accuracy": 0.91, "tokens": 12345},
            "started_at": _DT,
            "completed_at": _DT2,
        },
        True,
    ),
    Case(
        "eval_error",
        {
            "trial_id": "trial-3",
            "experiment_id": "exp-1",
            "proposal_id": "p-1",
            "status": "eval_error",
            "parent_commits": [_SHA1],
            "started_at": _DT,
        },
        True,
    ),
    Case(
        "branch_not_work",
        {
            "trial_id": "trial-4",
            "experiment_id": "exp-1",
            "proposal_id": "p-1",
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
            "trial_id": "trial-5",
            "experiment_id": "exp-1",
            "proposal_id": "p-1",
            "status": "running",
            "parent_commits": [_SHA1],
            "started_at": _DT,
        },
        False,
    ),
    Case(
        "bad_commit_sha",
        {
            "trial_id": "trial-6",
            "experiment_id": "exp-1",
            "proposal_id": "p-1",
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
            "trial_id": "trial-7",
            "experiment_id": "exp-1",
            "proposal_id": "p-1",
            "status": "starting",
            "parent_commits": [],
            "started_at": _DT,
        },
        False,
    ),
    Case(
        "metrics_explicit_null",
        {
            "trial_id": "trial-8",
            "experiment_id": "exp-1",
            "proposal_id": "p-1",
            "status": "starting",
            "parent_commits": [_SHA1],
            "metrics": None,
            "started_at": _DT,
        },
        False,
    ),
    Case(
        "artifacts_uri_no_scheme",
        {
            "trial_id": "trial-9",
            "experiment_id": "exp-1",
            "proposal_id": "p-1",
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
            "trial_id": "trial-11",
            "experiment_id": "exp-1",
            "proposal_id": "p-1",
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
            "trial_id": "trial-10",
            "experiment_id": "exp-1",
            "proposal_id": "p-1",
            "status": "starting",
            "parent_commits": [_SHA1],
            "started_at": _DT,
            "completed_at": None,
        },
        False,
    ),
]


METRICS_SCHEMA_CASES: list[Case] = [
    Case("one_real", {"accuracy": "real"}, True),
    Case("mixed", {"accuracy": "real", "tokens": "integer", "note": "text"}, True),
    Case("leading_underscore", {"_internal": "integer"}, True),
    Case("empty", {}, False),
    Case("reserved_trial_id", {"trial_id": "text"}, False),
    Case("reserved_completed_at", {"completed_at": "text"}, False),
    Case("invalid_type", {"x": "float"}, False),
    Case("numeric_first_char", {"1accuracy": "real"}, False),
    Case("hyphen_in_name", {"accu-racy": "real"}, False),
]


ALL_CASES: dict[str, list[Case]] = {
    "experiment-config": EXPERIMENT_CONFIG_CASES,
    "task": TASK_CASES,
    "event": EVENT_CASES,
    "proposal": PROPOSAL_CASES,
    "trial": TRIAL_CASES,
    "metrics-schema": METRICS_SCHEMA_CASES,
}
