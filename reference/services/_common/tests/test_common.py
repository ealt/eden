"""Smoke tests for the shared service scaffolding."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest
from eden_contracts import EvaluationSchema
from eden_git import GitRepo
from eden_service_common import (
    StopFlag,
    install_stop_handlers,
    make_evaluate_fn,
    make_implement_fn,
    make_plan_fn,
    seed_bare_repo,
)
from eden_service_common.cli import add_common_arguments


def test_seed_bare_repo_produces_resolvable_head(tmp_path: Path) -> None:
    import subprocess

    subprocess.run(
        ["git", "init", "--bare", "--initial-branch", "main", str(tmp_path)],
        check=True,
        capture_output=True,
    )
    seed_sha = seed_bare_repo(str(tmp_path))
    assert len(seed_sha) == 40
    repo = GitRepo(str(tmp_path))
    assert repo.resolve_ref("refs/heads/main") == seed_sha


_TS = "2026-04-01T00:00:00Z"


def _make_plan_task(task_id: str = "ideation-1") -> Any:
    from eden_contracts import IdeationPayload, IdeationTask

    return IdeationTask(
        task_id=task_id,
        kind="ideation",
        state="pending",
        created_at=_TS,
        updated_at=_TS,
        payload=IdeationPayload(experiment_id="exp_0123456789abcdefghjkmnpqrs"),
    )


def _make_impl_task(task_id: str = "execution-1") -> Any:
    from eden_contracts import ExecutionPayload, ExecutionTask

    return ExecutionTask(
        task_id=task_id,
        kind="execution",
        state="pending",
        created_at=_TS,
        updated_at=_TS,
        payload=ExecutionPayload(idea_id="idea-1"),
    )


def _make_eval_task(task_id: str = "evaluate-1") -> Any:
    from eden_contracts import EvaluationPayload, EvaluationTask

    return EvaluationTask(
        task_id=task_id,
        kind="evaluation",
        state="pending",
        created_at=_TS,
        updated_at=_TS,
        payload=EvaluationPayload(variant_id="variant-1"),
    )


def test_make_ideation_fn_emits_ideas_with_base_commit_parent() -> None:
    base = "a" * 40
    ideation_fn = make_plan_fn(base_commit_sha=base, ideas_per_ideation=2)
    out = ideation_fn(_make_plan_task())
    assert len(out) == 2
    for tpl in out:
        assert tpl.parent_commits == (base,)
        assert tpl.slug.startswith("ideation-1-p")


def test_make_execution_fn_writes_real_commit(tmp_path: Path) -> None:
    import subprocess

    from eden_contracts import Idea

    subprocess.run(
        ["git", "init", "--bare", "--initial-branch", "main", str(tmp_path)],
        check=True,
        capture_output=True,
    )
    base = seed_bare_repo(str(tmp_path))
    exec_fn = make_implement_fn(repo_path=str(tmp_path))
    task = _make_impl_task()
    idea = Idea(
        idea_id="idea-1",
        experiment_id="exp_0123456789abcdefghjkmnpqrs",
        slug="feat-x",
        priority=1.0,
        parent_commits=[base],
        artifacts_uri="file:///tmp/artifacts",
        state="ready",
        created_at=_TS,
    )
    outcome = exec_fn(task, idea)
    assert outcome.status == "success"
    assert outcome.commit_sha is not None
    assert len(outcome.commit_sha) == 40
    repo = GitRepo(str(tmp_path))
    assert repo.commit_parents(outcome.commit_sha) == [base]


def test_make_evaluate_fn_emits_schema_matching_evaluation() -> None:
    from eden_contracts import Variant

    schema = EvaluationSchema({"loss": "real", "steps": "integer", "note": "text"})
    eval_fn = make_evaluate_fn(evaluation_schema=schema)
    task = _make_eval_task()
    variant = Variant(
        variant_id="variant-1",
        experiment_id="exp_0123456789abcdefghjkmnpqrs",
        idea_id="idea-1",
        status="starting",
        parent_commits=["a" * 40],
        branch="work/x",
        commit_sha="b" * 40,
        started_at=_TS,
    )
    outcome = eval_fn(task, variant)
    assert outcome.status == "success"
    assert outcome.evaluation is not None
    assert set(outcome.evaluation.keys()) == {"loss", "steps", "note"}
    assert isinstance(outcome.evaluation["loss"], float)
    assert isinstance(outcome.evaluation["steps"], int)
    assert isinstance(outcome.evaluation["note"], str)


def test_stop_flag_wait_returns_true_on_set() -> None:
    flag = StopFlag()
    assert flag.is_set() is False
    flag.set()
    assert flag.is_set() is True
    assert flag.wait(0.01) is True


def test_install_stop_handlers_is_noop_off_main_thread() -> None:
    import threading

    flag = StopFlag()
    errors: list[BaseException] = []

    def _run() -> None:
        try:
            install_stop_handlers(flag)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=_run)
    t.start()
    t.join()
    assert errors == []


def test_experiment_id_rejects_empty_string() -> None:
    """``--experiment-id ""`` is rejected at parse time so downstream
    Pydantic constructions with ``Field(min_length=1)`` can't be
    reached with an empty value (codex-review round-2 finding on
    issue #134)."""
    parser = argparse.ArgumentParser()
    add_common_arguments(parser, require_task_store_url=False)
    with pytest.raises(SystemExit):
        parser.parse_args(["--experiment-id", ""])


def test_experiment_id_rejects_whitespace_only() -> None:
    parser = argparse.ArgumentParser()
    add_common_arguments(parser, require_task_store_url=False)
    with pytest.raises(SystemExit):
        parser.parse_args(["--experiment-id", "   "])


def test_experiment_id_accepts_normal_value() -> None:
    parser = argparse.ArgumentParser()
    add_common_arguments(parser, require_task_store_url=False)
    args = parser.parse_args(["--experiment-id", "exp-1"])
    assert args.experiment_id == "exp-1"
