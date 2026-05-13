"""Unit tests for the ideator subprocess mode."""

from __future__ import annotations

import textwrap
import time
from pathlib import Path

import pytest
from eden_contracts import EvaluationSchema, ExperimentConfig, IdeationTask, ObjectiveSpec
from eden_ideator_host import build_subprocess_config, run_ideator_subprocess_loop
from eden_ideator_host.subprocess_mode import (
    IdeatorSubprocessConfig,
    ProtocolViolation,
    handle_ideation_task,
    start_ideator_subprocess,
)
from eden_service_common import StopFlag, seed_bare_repo
from eden_storage import IdeaSubmission, InMemoryStore

EXPERIMENT_ID = "exp-1"


def _seed_store_and_repo(tmp_path: Path) -> tuple[InMemoryStore, str]:
    repo_path = tmp_path / "bare.git"
    from eden_git import GitRepo

    GitRepo.init_bare(repo_path)
    seed_sha = seed_bare_repo(str(repo_path))
    store = InMemoryStore(
        experiment_id=EXPERIMENT_ID,
        evaluation_schema=EvaluationSchema.model_validate({"score": "real"}),
    )
    # 12a-1 wave 5: Store.claim's §3.5 step-2 registration check
    # rejects unregistered worker_ids. Pre-register the ideator-1
    # worker the subprocess loop uses.
    store.register_worker("ideator-1")
    return store, seed_sha


def _experiment_config() -> ExperimentConfig:
    return ExperimentConfig(
        parallel_variants=1,
        max_variants=10,
        max_wall_time="1h",
        evaluation_schema=EvaluationSchema.model_validate({"score": "real"}),
        objective=ObjectiveSpec(expr="score", direction="maximize"),
    )


def _write_worker(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "worker.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _config(
    *, command: str, cwd: Path, startup: float = 5, task: float = 5
) -> IdeatorSubprocessConfig:
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
    sub = start_ideator_subprocess(config)
    assert sub.is_alive
    sub.stop()


def test_dispatch_collects_ideas(tmp_path: Path) -> None:
    worker = _write_worker(
        tmp_path,
        """
        import json, sys
        print(json.dumps({"event": "ready"}), flush=True)
        line = sys.stdin.readline()
        dispatch = json.loads(line)
        task_id = dispatch["task_id"]
        for i in range(2):
            print(json.dumps({"event": "idea", "task_id": task_id,
                              "slug": f"{task_id}-p{i}",
                              "priority": float(2 - i),
                              "parent_commits": ["a" * 40],
                              "rationale": f"# rationale {i}\\n"}),
                  flush=True)
        print(json.dumps({"event": "ideation-done", "task_id": task_id}), flush=True)
        """,
    )
    store, _ = _seed_store_and_repo(tmp_path)
    store.create_ideation_task("ideation-1")
    artifacts = tmp_path / "artifacts"
    config = _config(command=f"python3 {worker}", cwd=tmp_path)
    sub = start_ideator_subprocess(config)
    ideation_task = store.list_tasks(kind="ideation", state="pending")[0]
    assert isinstance(ideation_task, IdeationTask)
    handle_ideation_task(
        store=store,
        task=ideation_task,
        worker_id="ideator-1",
        ideator=sub,
        experiment_id=EXPERIMENT_ID,
        objective={"expr": "score", "direction": "maximize"},
        evaluation_schema={"score": "real"},
        artifacts_dir=artifacts,
    )
    sub.stop()
    submitted = store.read_task("ideation-1")
    assert submitted.state == "submitted"
    submission = store.read_submission("ideation-1")
    assert isinstance(submission, IdeaSubmission)
    assert submission.status == "success"
    assert len(submission.idea_ids) == 2
    ideas = [store.read_idea(pid) for pid in submission.idea_ids]
    assert all(p.state == "ready" for p in ideas)


def test_ideation_error_terminator(tmp_path: Path) -> None:
    worker = _write_worker(
        tmp_path,
        """
        import json, sys
        print(json.dumps({"event": "ready"}), flush=True)
        line = sys.stdin.readline()
        dispatch = json.loads(line)
        print(json.dumps({
            "event": "ideation-error",
            "task_id": dispatch["task_id"],
            "reason": "no",
        }), flush=True)
        """,
    )
    store, _ = _seed_store_and_repo(tmp_path)
    store.create_ideation_task("ideation-1")
    artifacts = tmp_path / "artifacts"
    config = _config(command=f"python3 {worker}", cwd=tmp_path)
    sub = start_ideator_subprocess(config)
    ideation_task = store.list_tasks(kind="ideation", state="pending")[0]
    assert isinstance(ideation_task, IdeationTask)
    handle_ideation_task(
        store=store,
        task=ideation_task,
        worker_id="ideator-1",
        ideator=sub,
        experiment_id=EXPERIMENT_ID,
        objective={"expr": "score", "direction": "maximize"},
        evaluation_schema={"score": "real"},
        artifacts_dir=artifacts,
    )
    sub.stop()
    submission = store.read_submission("ideation-1")
    assert isinstance(submission, IdeaSubmission)
    assert submission.status == "error"


def test_protocol_violation_wrong_task_id(tmp_path: Path) -> None:
    worker = _write_worker(
        tmp_path,
        """
        import json, sys
        print(json.dumps({"event": "ready"}), flush=True)
        line = sys.stdin.readline()
        # Reply with a different task_id (protocol violation).
        print(json.dumps({"event": "idea", "task_id": "nope",
                          "slug": "x", "priority": 1.0,
                          "parent_commits": ["a" * 40]}),
              flush=True)
        """,
    )
    store, _ = _seed_store_and_repo(tmp_path)
    store.create_ideation_task("ideation-1")
    config = _config(command=f"python3 {worker}", cwd=tmp_path)
    sub = start_ideator_subprocess(config)
    ideation_task = store.list_tasks(kind="ideation", state="pending")[0]
    assert isinstance(ideation_task, IdeationTask)
    with pytest.raises(ProtocolViolation):
        handle_ideation_task(
            store=store,
            task=ideation_task,
            worker_id="ideator-1",
            ideator=sub,
            experiment_id=EXPERIMENT_ID,
            objective={"expr": "score", "direction": "maximize"},
            evaluation_schema={"score": "real"},
            artifacts_dir=tmp_path,
        )
    sub.stop()
    submission = store.read_submission("ideation-1")
    assert isinstance(submission, IdeaSubmission)
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
        print(json.dumps({"event": "idea", "task_id": task_id,
                          "slug": "p0", "priority": 1.0,
                          "parent_commits": ["a" * 40],
                          "rationale": "# r"}),
              flush=True)
        print(json.dumps({"event": "ideation-done", "task_id": task_id}), flush=True)
        """,
    )
    store, _ = _seed_store_and_repo(tmp_path)
    store.create_ideation_task("ideation-1")
    store.create_ideation_task("ideation-2")
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
        run_ideator_subprocess_loop(
            store=store,
            worker_id="ideator-1",
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
        t1 = store.read_task("ideation-1")
        t2 = store.read_task("ideation-2")
        if t1.state == "submitted" and t2.state == "submitted":
            break
        time.sleep(0.1)
    stop.set()
    t.join(timeout=10)
    assert not t.is_alive()
    assert marker.is_file()
    # First task got submit-error from the crash; second succeeds after respawn.
    s1 = store.read_submission("ideation-1")
    s2 = store.read_submission("ideation-2")
    assert isinstance(s1, IdeaSubmission)
    assert isinstance(s2, IdeaSubmission)
    assert s1.status == "error"
    assert s2.status == "success"
