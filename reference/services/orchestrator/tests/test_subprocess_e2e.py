"""Real-subprocess end-to-end test for Phase 10d subprocess mode.

Mirrors ``test_e2e.py`` but runs each worker host with
``--mode subprocess`` and the fixture's ``plan.py`` /
``implement.py`` / ``eval.py`` scripts. Asserts the same final
3-trial-success shape against the bare repo.

# TODO(eden#39): this test flakes ~30% locally because the
# orchestrator's default ``--max-quiescent-iterations 3 ×
# --poll-interval 0.1 = 0.3s`` quiescence tolerance is shorter
# than the subprocess-mode worker startup window. Bumping the
# tolerance triggers a deeper bug (task-store-server stalls on
# ``list_trials`` after ~8s of orchestrator runtime). Investigation
# in progress; if this test flakes on CI, leave a comment on issue
# #39 with the run URL.
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
    reason="subprocess signal handling is POSIX-only",
)

FIXTURE_DIR = (
    Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "experiment"
)
FIXTURE_CONFIG = FIXTURE_DIR / ".eden" / "config.yaml"


def _spawn(args: list[str]) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _read_port_announcement(proc: subprocess.Popen, timeout: float = 10.0) -> int:
    deadline = time.monotonic() + timeout
    assert proc.stdout is not None
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
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
def test_three_trial_experiment_subprocess_mode(tmp_path: Path) -> None:
    """Spawn 5 processes in subprocess mode against fixture scripts."""
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
    experiment_id = "exp-e2e-sub"
    token = "test-token"
    artifacts_dir = tmp_path / "artifacts"
    worktrees_dir = tmp_path / "worktrees"

    server = _spawn(
        [
            "eden_task_store_server",
            "--store-url",
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

    planner_env_file = tmp_path / "planner.env"
    planner_env_file.write_text(
        f"EDEN_BASE_COMMIT_SHA={base_sha}\nEDEN_PROPOSALS_PER_PLAN=1\n",
        encoding="utf-8",
    )

    planner = _spawn(
        [
            "eden_planner_host",
            "--mode",
            "subprocess",
            "--task-store-url",
            base_url,
            "--experiment-id",
            experiment_id,
            "--shared-token",
            token,
            "--worker-id",
            "planner-1",
            "--experiment-config",
            str(FIXTURE_CONFIG),
            "--experiment-dir",
            str(FIXTURE_DIR),
            "--artifacts-dir",
            str(artifacts_dir),
            "--plan-env-file",
            str(planner_env_file),
        ]
    )
    implementer = _spawn(
        [
            "eden_implementer_host",
            "--mode",
            "subprocess",
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
            "--experiment-config",
            str(FIXTURE_CONFIG),
            "--experiment-dir",
            str(FIXTURE_DIR),
            "--worktrees-dir",
            str(worktrees_dir),
        ]
    )
    evaluator = _spawn(
        [
            "eden_evaluator_host",
            "--mode",
            "subprocess",
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
            "--experiment-dir",
            str(FIXTURE_DIR),
            "--repo-path",
            str(bare_repo),
            "--worktrees-dir",
            str(worktrees_dir),
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
            rc = orchestrator.wait(timeout=120)
        except subprocess.TimeoutExpired:
            for p in procs.values():
                _terminate(p)
            pytest.fail(
                "orchestrator did not exit within 120s. Subprocess stderr:\n"
                + _dump_stderr(procs)
            )
        if rc != 0:
            for p in procs.values():
                _terminate(p)
            pytest.fail(
                f"orchestrator exited {rc}. Subprocess stderr:\n"
                + _dump_stderr(procs)
            )

        for name in ("planner", "implementer", "evaluator", "task-store-server"):
            _terminate(procs[name])

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
                assert parents, "trial commit must have at least one parent"
        finally:
            store.close()
    finally:
        for p in procs.values():
            _terminate(p)
