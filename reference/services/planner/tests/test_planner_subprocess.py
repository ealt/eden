"""Unit tests for the planner subprocess mode."""

from __future__ import annotations

import textwrap
import time
from pathlib import Path

import pytest
from eden_contracts import ExperimentConfig, MetricsSchema, ObjectiveSpec, PlanTask
from eden_planner_host import build_subprocess_config, run_planner_subprocess_loop
from eden_planner_host.subprocess_mode import (
    PlannerSubprocessConfig,
    ProtocolViolation,
    handle_plan_task,
    start_planner_subprocess,
)
from eden_service_common import StopFlag, seed_bare_repo
from eden_storage import InMemoryStore, PlanSubmission

EXPERIMENT_ID = "exp-1"


def _seed_store_and_repo(tmp_path: Path) -> tuple[InMemoryStore, str]:
    repo_path = tmp_path / "bare.git"
    from eden_git import GitRepo

    GitRepo.init_bare(repo_path)
    seed_sha = seed_bare_repo(str(repo_path))
    store = InMemoryStore(
        experiment_id=EXPERIMENT_ID,
        metrics_schema=MetricsSchema.model_validate({"score": "real"}),
    )
    return store, seed_sha


def _experiment_config() -> ExperimentConfig:
    return ExperimentConfig(
        parallel_trials=1,
        max_trials=10,
        max_wall_time="1h",
        metrics_schema=MetricsSchema.model_validate({"score": "real"}),
        objective=ObjectiveSpec(expr="score", direction="maximize"),
    )


def _write_worker(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "worker.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _config(
    *, command: str, cwd: Path, startup: float = 5, task: float = 5
) -> PlannerSubprocessConfig:
    return build_subprocess_config(
        command=command,
        cwd=cwd,
        env={},
        startup_deadline=startup,
        task_deadline=task,
        shutdown_deadline=2,
    )


def test_ready_handshake(tmp_path: Path) -> None:
    worker = _write_worker(
        tmp_path,
        """
        import json, sys
        print(json.dumps({"event": "ready"}), flush=True)
        sys.stdin.readline()
        """,
    )
    config = _config(command=f"python3 {worker}", cwd=tmp_path)
    sub = start_planner_subprocess(config)
    assert sub.is_alive
    sub.stop()


def test_dispatch_collects_proposals(tmp_path: Path) -> None:
    worker = _write_worker(
        tmp_path,
        """
        import json, sys
        print(json.dumps({"event": "ready"}), flush=True)
        line = sys.stdin.readline()
        dispatch = json.loads(line)
        task_id = dispatch["task_id"]
        for i in range(2):
            print(json.dumps({"event": "proposal", "task_id": task_id,
                              "slug": f"{task_id}-p{i}",
                              "priority": float(2 - i),
                              "parent_commits": ["a" * 40],
                              "rationale": f"# rationale {i}\\n"}),
                  flush=True)
        print(json.dumps({"event": "plan-done", "task_id": task_id}), flush=True)
        """,
    )
    store, _ = _seed_store_and_repo(tmp_path)
    store.create_plan_task("plan-1")
    artifacts = tmp_path / "artifacts"
    config = _config(command=f"python3 {worker}", cwd=tmp_path)
    sub = start_planner_subprocess(config)
    plan_task = store.list_tasks(kind="plan", state="pending")[0]
    assert isinstance(plan_task, PlanTask)
    handle_plan_task(
        store=store,
        task=plan_task,
        worker_id="planner-1",
        planner=sub,
        experiment_id=EXPERIMENT_ID,
        objective={"expr": "score", "direction": "maximize"},
        metrics_schema={"score": "real"},
        artifacts_dir=artifacts,
    )
    sub.stop()
    submitted = store.read_task("plan-1")
    assert submitted.state == "submitted"
    submission = store.read_submission("plan-1")
    assert isinstance(submission, PlanSubmission)
    assert submission.status == "success"
    assert len(submission.proposal_ids) == 2
    proposals = [store.read_proposal(pid) for pid in submission.proposal_ids]
    assert all(p.state == "ready" for p in proposals)


def test_plan_error_terminator(tmp_path: Path) -> None:
    worker = _write_worker(
        tmp_path,
        """
        import json, sys
        print(json.dumps({"event": "ready"}), flush=True)
        line = sys.stdin.readline()
        dispatch = json.loads(line)
        print(json.dumps({"event": "plan-error", "task_id": dispatch["task_id"], "reason": "no"}),
              flush=True)
        """,
    )
    store, _ = _seed_store_and_repo(tmp_path)
    store.create_plan_task("plan-1")
    artifacts = tmp_path / "artifacts"
    config = _config(command=f"python3 {worker}", cwd=tmp_path)
    sub = start_planner_subprocess(config)
    plan_task = store.list_tasks(kind="plan", state="pending")[0]
    assert isinstance(plan_task, PlanTask)
    handle_plan_task(
        store=store,
        task=plan_task,
        worker_id="planner-1",
        planner=sub,
        experiment_id=EXPERIMENT_ID,
        objective={"expr": "score", "direction": "maximize"},
        metrics_schema={"score": "real"},
        artifacts_dir=artifacts,
    )
    sub.stop()
    submission = store.read_submission("plan-1")
    assert isinstance(submission, PlanSubmission)
    assert submission.status == "error"


def test_protocol_violation_wrong_task_id(tmp_path: Path) -> None:
    worker = _write_worker(
        tmp_path,
        """
        import json, sys
        print(json.dumps({"event": "ready"}), flush=True)
        line = sys.stdin.readline()
        # Reply with a different task_id (protocol violation).
        print(json.dumps({"event": "proposal", "task_id": "nope",
                          "slug": "x", "priority": 1.0,
                          "parent_commits": ["a" * 40]}),
              flush=True)
        """,
    )
    store, _ = _seed_store_and_repo(tmp_path)
    store.create_plan_task("plan-1")
    config = _config(command=f"python3 {worker}", cwd=tmp_path)
    sub = start_planner_subprocess(config)
    plan_task = store.list_tasks(kind="plan", state="pending")[0]
    assert isinstance(plan_task, PlanTask)
    with pytest.raises(ProtocolViolation):
        handle_plan_task(
            store=store,
            task=plan_task,
            worker_id="planner-1",
            planner=sub,
            experiment_id=EXPERIMENT_ID,
            objective={"expr": "score", "direction": "maximize"},
            metrics_schema={"score": "real"},
            artifacts_dir=tmp_path,
        )
    sub.stop()
    submission = store.read_submission("plan-1")
    assert isinstance(submission, PlanSubmission)
    assert submission.status == "error"


def test_loop_respawns_on_subprocess_crash(tmp_path: Path) -> None:
    """When the subprocess exits unexpectedly, the loop respawns it."""
    worker = _write_worker(
        tmp_path,
        """
        import json, os, sys
        print(json.dumps({"event": "ready"}), flush=True)
        marker = os.environ.get("CRASH_MARKER")
        if marker and not os.path.exists(marker):
            open(marker, "w").close()
            sys.exit(1)
        line = sys.stdin.readline()
        dispatch = json.loads(line)
        task_id = dispatch["task_id"]
        print(json.dumps({"event": "proposal", "task_id": task_id,
                          "slug": "p0", "priority": 1.0,
                          "parent_commits": ["a" * 40],
                          "rationale": "# r"}),
              flush=True)
        print(json.dumps({"event": "plan-done", "task_id": task_id}), flush=True)
        """,
    )
    store, _ = _seed_store_and_repo(tmp_path)
    store.create_plan_task("plan-1")
    store.create_plan_task("plan-2")
    marker = tmp_path / "crashed"
    config = build_subprocess_config(
        command=f"python3 {worker}",
        cwd=tmp_path,
        env={"CRASH_MARKER": str(marker)},
        startup_deadline=5,
        task_deadline=5,
        shutdown_deadline=2,
    )
    stop = StopFlag()

    import threading

    def _run() -> None:
        run_planner_subprocess_loop(
            store=store,
            worker_id="planner-1",
            experiment_id=EXPERIMENT_ID,
            experiment_config=_experiment_config(),
            artifacts_dir=tmp_path / "artifacts",
            subprocess_config=config,
            poll_interval=0.05,
            stop=stop,
        )

    t = threading.Thread(target=_run)
    t.start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        t1 = store.read_task("plan-1")
        t2 = store.read_task("plan-2")
        if t1.state == "submitted" and t2.state == "submitted":
            break
        time.sleep(0.1)
    stop.set()
    t.join(timeout=10)
    assert not t.is_alive()
    assert marker.is_file()
    # First task got submit-error from the crash; second succeeds after respawn.
    s1 = store.read_submission("plan-1")
    s2 = store.read_submission("plan-2")
    assert isinstance(s1, PlanSubmission)
    assert isinstance(s2, PlanSubmission)
    assert s1.status == "error"
    assert s2.status == "success"
