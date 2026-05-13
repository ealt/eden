"""Round-trip tests for ``Task.target`` (12a-1 wave 2).

The ``target`` field's claim-time RBAC enforcement lands in a later
wave; wave 2 only requires the field to round-trip cleanly through
storage so that wave-3 wire endpoints and wave-4 services see the
recorded value when they later add enforcement.

The persistence path for tasks is JSON-blob through ``data`` columns
(see ``_schema.py`` / ``_postgres_schema.py``); ``target`` should
travel transparently via Pydantic's ``model_dump`` /
``model_validate`` round-trip without any schema change. These tests
exercise that round-trip on every backend.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from eden_contracts import (
    IdeationPayload,
    IdeationTask,
    TaskTarget,
)
from eden_storage import Store


def _ideation_task_with_target(
    experiment_id: str,
    *,
    task_id: str = "t-target",
    target: TaskTarget | None = None,
) -> IdeationTask:
    # Build via model_validate so an absent target is omitted entirely;
    # passing target=None directly trips the NotNone validator (which
    # mirrors the JSON-Schema absent-vs-null discipline).
    data: dict[str, object] = {
        "task_id": task_id,
        "kind": "ideation",
        "state": "pending",
        "payload": IdeationPayload(experiment_id=experiment_id).model_dump(
            mode="json", exclude_none=True
        ),
        "created_at": "2026-04-23T00:00:00.000Z",
        "updated_at": "2026-04-23T00:00:00.000Z",
    }
    if target is not None:
        data["target"] = target.model_dump(mode="json", exclude_none=True)
    return IdeationTask.model_validate(data)


def test_target_worker_round_trips(make_store: Callable[..., Store]) -> None:
    store = make_store()
    target = TaskTarget(kind="worker", id="eric")
    store.create_task(_ideation_task_with_target(store.experiment_id, target=target))
    fresh = store.read_task("t-target")
    assert fresh.target is not None
    assert fresh.target.kind == "worker"
    assert fresh.target.id == "eric"


def test_target_group_round_trips(make_store: Callable[..., Store]) -> None:
    store = make_store()
    target = TaskTarget(kind="group", id="humans")
    store.create_task(_ideation_task_with_target(store.experiment_id, target=target))
    fresh = store.read_task("t-target")
    assert fresh.target is not None
    assert fresh.target.kind == "group"
    assert fresh.target.id == "humans"


def test_target_absent_round_trips(make_store: Callable[..., Store]) -> None:
    """A task with no target reads back with target=None."""
    store = make_store()
    store.create_task(_ideation_task_with_target(store.experiment_id, target=None))
    fresh = store.read_task("t-target")
    assert fresh.target is None


@pytest.mark.parametrize(
    ("kind", "id_"),
    [
        ("worker", "eric"),
        ("group", "humans"),
    ],
)
def test_target_appears_in_list_tasks(
    make_store: Callable[..., Store], kind: str, id_: str
) -> None:
    """``list_tasks`` returns tasks with their full target intact."""
    store = make_store()
    target = TaskTarget(kind=kind, id=id_)  # type: ignore[arg-type]
    store.create_task(_ideation_task_with_target(store.experiment_id, target=target))
    tasks = store.list_tasks(kind="ideation")
    assert len(tasks) == 1
    [task] = tasks
    assert task.target is not None
    assert task.target.kind == kind
    assert task.target.id == id_
