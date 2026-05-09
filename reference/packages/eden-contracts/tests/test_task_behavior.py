"""Targeted tests for Task: discriminator dispatch and payload typing."""

from __future__ import annotations

import pytest
from eden_contracts import (
    EvaluationPayload,
    EvaluationTask,
    ExecutionPayload,
    ExecutionTask,
    IdeationPayload,
    IdeationTask,
    TaskAdapter,
)
from pydantic import ValidationError


def _claim() -> dict[str, str]:
    return {
        "worker_id": "w",
        "claimed_at": "2026-04-23T12:00:00Z",
    }


def test_discriminator_dispatches_ideation() -> None:
    task = TaskAdapter.validate_python(
        {
            "task_id": "t",
            "kind": "ideation",
            "state": "pending",
            "payload": {"experiment_id": "exp-1"},
            "created_at": "2026-04-23T12:00:00Z",
            "updated_at": "2026-04-23T12:00:00Z",
        }
    )
    assert isinstance(task, IdeationTask)
    assert isinstance(task.payload, IdeationPayload)
    assert task.payload.experiment_id == "exp-1"


def test_discriminator_dispatches_execution() -> None:
    task = TaskAdapter.validate_python(
        {
            "task_id": "t",
            "kind": "execution",
            "state": "claimed",
            "payload": {"idea_id": "idea-1"},
            "claim": _claim(),
            "created_at": "2026-04-23T12:00:00Z",
            "updated_at": "2026-04-23T12:00:00Z",
        }
    )
    assert isinstance(task, ExecutionTask)
    assert isinstance(task.payload, ExecutionPayload)


def test_discriminator_dispatches_evaluate() -> None:
    task = TaskAdapter.validate_python(
        {
            "task_id": "t",
            "kind": "evaluation",
            "state": "submitted",
            "payload": {"variant_id": "variant-1"},
            "claim": _claim(),
            "created_at": "2026-04-23T12:00:00Z",
            "updated_at": "2026-04-23T12:00:00Z",
        }
    )
    assert isinstance(task, EvaluationTask)
    assert isinstance(task.payload, EvaluationPayload)


def test_claim_required_when_claimed() -> None:
    with pytest.raises(ValidationError, match="claim is required"):
        TaskAdapter.validate_python(
            {
                "task_id": "t",
                "kind": "ideation",
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
                "kind": "ideation",
                "state": "completed",
                "payload": {"experiment_id": "exp-1"},
                "claim": _claim(),
                "created_at": "2026-04-23T12:00:00Z",
                "updated_at": "2026-04-23T12:00:00Z",
            }
        )
