"""Real-subprocess end-to-end test.

Forks five processes (task-store-server + planner + implementer +
evaluator + orchestrator) on an ephemeral port and drives a 3-trial
experiment to quiescence over real HTTP.

The test passes if the orchestrator exits 0 and the final DB + repo
state match the expected shape. On failure, every subprocess's stderr
is captured into the pytest failure report.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from eden_contracts import MetricsSchema
from eden_git import GitRepo
from eden_service_common import seed_bare_repo
from eden_storage import SqliteStore

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="subprocess signal handling is POSIX-only for 8b",
)

FIXTURE_CONFIG = (
    Path(__file__).resolve().parents[4]
    / "tests"
    / "fixtures"
    / "experiment"
    / ".eden"
    / "config.yaml"
)


def _spawn(args: list[str]) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _read_port_announcement(proc: subprocess.Popen, timeout: float = 10.0) -> int:
    """Read task-store-server's EDEN_TASK_STORE_LISTENING line and return the port."""
    deadline = time.monotonic() + timeout
    assert proc.stdout is not None
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            # Process may have died before listening.
            if proc.poll() is not None:
                raise RuntimeError(
                    f"task-store-server exited early with code {proc.returncode}"
                )
            continue
        if line.startswith("EDEN_TASK_STORE_LISTENING"):
            parts = dict(p.split("=", 1) for p in line.strip().split()[1:])
            return int(parts["port"])
    raise RuntimeError("task-store-server did not announce its port in time")


def _terminate(proc: subprocess.Popen, timeout: float = 5.0) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)


def _dump_stderr(procs: dict[str, subprocess.Popen]) -> str:
    parts = []
    for name, p in procs.items():
        stderr = ""
        if p.stderr is not None:
            try:
                stderr = p.stderr.read() or ""
            except Exception as exc:  # noqa: BLE001
                stderr = f"<failed to read stderr: {exc!r}>"
        parts.append(
            f"--- {name} (pid={p.pid}, rc={p.returncode}) ---\n{stderr}\n"
        )
    return "\n".join(parts)


@pytest.mark.e2e
def test_three_trial_experiment_over_subprocesses(tmp_path: Path) -> None:
    """Spawn 5 processes, run to quiescence, assert final state."""
    bare_repo = tmp_path / "bare-repo.git"
    subprocess.run(
        [
            "git",
            "init",
            "--bare",
            "--initial-branch",
            "main",
            str(bare_repo),
        ],
        check=True,
        capture_output=True,
    )
    base_sha = seed_bare_repo(str(bare_repo))

    db_path = tmp_path / "eden.sqlite"
    experiment_id = "exp-e2e"
    token = "test-token"

    server = _spawn(
        [
            "eden_task_store_server",
            "--db-path",
            str(db_path),
            "--experiment-id",
            experiment_id,
            "--experiment-config",
            str(FIXTURE_CONFIG),
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--shared-token",
            token,
            "--subscribe-timeout",
            "1.0",
        ]
    )
    port = _read_port_announcement(server)
    base_url = f"http://127.0.0.1:{port}"

    planner = _spawn(
        [
            "eden_planner_host",
            "--task-store-url",
            base_url,
            "--experiment-id",
            experiment_id,
            "--shared-token",
            token,
            "--worker-id",
            "planner-1",
            "--base-commit-sha",
            base_sha,
        ]
    )
    implementer = _spawn(
        [
            "eden_implementer_host",
            "--task-store-url",
            base_url,
            "--experiment-id",
            experiment_id,
            "--shared-token",
            token,
            "--worker-id",
            "implementer-1",
            "--repo-path",
            str(bare_repo),
        ]
    )
    evaluator = _spawn(
        [
            "eden_evaluator_host",
            "--task-store-url",
            base_url,
            "--experiment-id",
            experiment_id,
            "--shared-token",
            token,
            "--worker-id",
            "evaluator-1",
            "--experiment-config",
            str(FIXTURE_CONFIG),
        ]
    )
    orchestrator = _spawn(
        [
            "eden_orchestrator",
            "--task-store-url",
            base_url,
            "--experiment-id",
            experiment_id,
            "--shared-token",
            token,
            "--repo-path",
            str(bare_repo),
            "--plan-tasks",
            "plan-1,plan-2,plan-3",
        ]
    )

    procs = {
        "task-store-server": server,
        "planner": planner,
        "implementer": implementer,
        "evaluator": evaluator,
        "orchestrator": orchestrator,
    }

    try:
        try:
            rc = orchestrator.wait(timeout=60)
        except subprocess.TimeoutExpired:
            # Tear workers + server down before reading stderr so any
            # buffered output flushes; then dump everything.
            for p in procs.values():
                _terminate(p)
            pytest.fail(
                "orchestrator did not exit within 60s. Subprocess stderr:\n"
                + _dump_stderr(procs)
            )
        if rc != 0:
            for p in procs.values():
                _terminate(p)
            pytest.fail(
                f"orchestrator exited {rc}. Subprocess stderr:\n"
                + _dump_stderr(procs)
            )

        # Tear down workers + server so the SqliteStore's file is released.
        for name in ("planner", "implementer", "evaluator", "task-store-server"):
            _terminate(procs[name])

        # Inspect final state.
        store = SqliteStore(
            experiment_id=experiment_id,
            path=str(db_path),
            metrics_schema=MetricsSchema({"score": "real"}),
        )
        try:
            trials = store.list_trials(status="success")
            if len(trials) != 3:
                pytest.fail(
                    f"expected 3 success trials, got {len(trials)}. Stderr:\n"
                    + _dump_stderr(procs)
                )
            repo = GitRepo(str(bare_repo))
            for trial in trials:
                assert trial.trial_commit_sha is not None
                assert trial.parent_commits == [base_sha]
                parents = repo.commit_parents(trial.trial_commit_sha)
                assert parents == list(trial.parent_commits)
                # The integrator MUST write a canonical
                # `refs/heads/trial/<id>-<slug>` ref pointing at
                # the same commit (chapter 06 §3.2).
                slug = store.read_proposal(trial.proposal_id).slug
                ref = f"refs/heads/trial/{trial.trial_id}-{slug}"
                assert repo.resolve_ref(ref) == trial.trial_commit_sha

            # Every plan/implement/evaluate task terminal.
            for kind in ("plan", "implement", "evaluate"):
                tasks = store.list_tasks(kind=kind)
                completed = [t for t in tasks if t.state == "completed"]
                assert len(completed) == 3, (
                    f"expected 3 completed {kind} tasks, got {len(completed)}"
                )

            # Event-log assertions (codex-review feedback): a 3-trial
            # experiment must emit one task.completed per role per trial
            # and one trial.integrated per trial. The full transcript is
            # richer (drafting, claimed, submitted, succeeded, …); this
            # test pins the must-have terminal events without freezing
            # the exact count of every intermediate event.
            events = list(store.read_range())
            event_types = [e.type for e in events]
            assert event_types.count("task.completed") == 9, (
                f"expected 9 task.completed events (3 per role × 3 trials), got "
                f"{event_types.count('task.completed')}; full sequence: {event_types}"
            )
            assert event_types.count("trial.integrated") == 3, (
                f"expected 3 trial.integrated events, got "
                f"{event_types.count('trial.integrated')}"
            )
            assert event_types.count("trial.succeeded") == 3, (
                f"expected 3 trial.succeeded events, got "
                f"{event_types.count('trial.succeeded')}"
            )

            # Replay the full event log to a final-state map and assert
            # every entity reaches a terminal status. Replaces the
            # Phase-5 in-process lifecycle-reconstruction test deleted
            # in Phase 8c.
            lifecycle = _reconstruct_lifecycle(events)
            # 3 plan + 3 implement + 3 evaluate = 9 tasks; the
            # `--plan-tasks` flag above seeds three plan IDs, each of
            # which becomes one proposal → one trial.
            assert len(lifecycle["tasks"]) == 9
            assert set(lifecycle["tasks"].values()) == {"completed"}
            assert len(lifecycle["proposals"]) == 3
            assert set(lifecycle["proposals"].values()) == {"completed"}
            assert len(lifecycle["trials"]) == 3
            assert set(lifecycle["trials"].values()) == {"success"}
        finally:
            store.close()
    finally:
        for p in procs.values():
            _terminate(p)


def _reconstruct_lifecycle(events) -> dict:  # noqa: ANN001 - sequence of EventRecord
    """Fold the event log into a final-state map per entity.

    Lifecycle-bearing event types map to a status; integration events
    (`trial.integrated`) are intentionally ignored — the trial's
    lifecycle status is `success`, with integration recorded as a
    separate field on the trial entity.
    """
    tasks: dict[str, str] = {}
    proposals: dict[str, str] = {}
    trials: dict[str, str] = {}
    for event in events:
        data = event.data
        t = event.type
        if t == "task.created":
            tasks[data["task_id"]] = "pending"
        elif t == "task.claimed":
            tasks[data["task_id"]] = "claimed"
        elif t == "task.submitted":
            tasks[data["task_id"]] = "submitted"
        elif t == "task.completed":
            tasks[data["task_id"]] = "completed"
        elif t == "task.failed":
            tasks[data["task_id"]] = "failed"
        elif t == "task.reclaimed":
            tasks[data["task_id"]] = "pending"
        elif t == "proposal.drafted":
            proposals[data["proposal_id"]] = "drafting"
        elif t == "proposal.ready":
            proposals[data["proposal_id"]] = "ready"
        elif t == "proposal.dispatched":
            proposals[data["proposal_id"]] = "dispatched"
        elif t == "proposal.completed":
            proposals[data["proposal_id"]] = "completed"
        elif t == "trial.started":
            trials[data["trial_id"]] = "starting"
        elif t == "trial.succeeded":
            trials[data["trial_id"]] = "success"
        elif t == "trial.errored":
            trials[data["trial_id"]] = "error"
        elif t == "trial.eval_errored":
            trials[data["trial_id"]] = "eval_error"
    return {"tasks": tasks, "proposals": proposals, "trials": trials}
