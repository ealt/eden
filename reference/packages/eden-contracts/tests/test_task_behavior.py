"""Targeted tests for Task: discriminator dispatch and payload typing."""

from __future__ import annotations

import pytest
from eden_contracts import (
    EvaluatePayload,
    EvaluateTask,
    ImplementPayload,
    ImplementTask,
    PlanPayload,
    PlanTask,
    TaskAdapter,
)
from pydantic import ValidationError


def _claim() -> dict[str, str]:
    return {
        "token": "c",
        "worker_id": "w",
        "claimed_at": "2026-04-23T12:00:00Z",
    }


def test_discriminator_dispatches_plan() -> None:
    task = TaskAdapter.validate_python(
        {
            "task_id": "t",
            "kind": "plan",
            "state": "pending",
            "payload": {"experiment_id": "exp-1"},
            "created_at": "2026-04-23T12:00:00Z",
            "updated_at": "2026-04-23T12:00:00Z",
        }
    )
    assert isinstance(task, PlanTask)
    assert isinstance(task.payload, PlanPayload)
    assert task.payload.experiment_id == "exp-1"


def test_discriminator_dispatches_implement() -> None:
    task = TaskAdapter.validate_python(
        {
            "task_id": "t",
            "kind": "implement",
            "state": "claimed",
            "payload": {"proposal_id": "p-1"},
            "claim": _claim(),
            "created_at": "2026-04-23T12:00:00Z",
            "updated_at": "2026-04-23T12:00:00Z",
        }
    )
    assert isinstance(task, ImplementTask)
    assert isinstance(task.payload, ImplementPayload)


def test_discriminator_dispatches_evaluate() -> None:
    task = TaskAdapter.validate_python(
        {
            "task_id": "t",
            "kind": "evaluate",
            "state": "submitted",
            "payload": {"trial_id": "trial-1"},
            "claim": _claim(),
            "created_at": "2026-04-23T12:00:00Z",
            "updated_at": "2026-04-23T12:00:00Z",
        }
    )
    assert isinstance(task, EvaluateTask)
    assert isinstance(task.payload, EvaluatePayload)


def test_claim_required_when_claimed() -> None:
    with pytest.raises(ValidationError, match="claim is required"):
        TaskAdapter.validate_python(
            {
                "task_id": "t",
                "kind": "plan",
                "state": "claimed",
                "payload": {"experiment_id": "exp-1"},
                "created_at": "2026-04-23T12:00:00Z",
                "updated_at": "2026-04-23T12:00:00Z",
            }
        )


def test_claim_forbidden_when_completed() -> None:
    with pytest.raises(ValidationError, match="claim is forbidden"):
        TaskAdapter.validate_python(
            {
                "task_id": "t",
                "kind": "plan",
                "state": "completed",
                "payload": {"experiment_id": "exp-1"},
                "claim": _claim(),
                "created_at": "2026-04-23T12:00:00Z",
                "updated_at": "2026-04-23T12:00:00Z",
            }
        )
