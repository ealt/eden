"""Real-subprocess end-to-end test for Phase 10d subprocess mode.

Mirrors ``test_e2e.py`` but runs each worker host with
``--mode subprocess`` and the fixture's ``plan.py`` /
``implement.py`` / ``eval.py`` scripts. Asserts the same final
3-trial-success shape against the bare repo.
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


def _spawn(args: list[str], log_path: Path) -> subprocess.Popen:
    # Redirect stdout+stderr to a per-subprocess file. Workers + server
    # log every HTTP request to stdout (eden_service_common.logging
    # routes the root logger to sys.stdout); over a 30+ s test run that
    # quickly exceeds the 64 KiB pipe buffer if nothing drains it, and
    # the server's async event loop wedges on a blocking write inside a
    # logger call. Tests previously used subprocess.PIPE without a
    # drainer and flaked on this; redirecting to a real file removes
    # the back-pressure entirely and preserves the log for diagnostics.
    return subprocess.Popen(
        [sys.executable, "-m", *args],
        stdout=open(log_path, "wb"),  # noqa: SIM115 — handle owned by Popen
        stderr=subprocess.STDOUT,
    )


def _read_port_announcement(
    log_path: Path, proc: subprocess.Popen, timeout: float = 10.0
) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if log_path.exists():
            try:
                content = log_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                content = ""
            for line in content.splitlines():
                if line.startswith("EDEN_TASK_STORE_LISTENING"):
                    parts = dict(p.split("=", 1) for p in line.strip().split()[1:])
                    return int(parts["port"])
        if proc.poll() is not None:
            raise RuntimeError(
                f"task-store-server exited early with code {proc.returncode}"
            )
        time.sleep(0.05)
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


def _dump_logs(procs_logs: dict[str, tuple[subprocess.Popen, Path]]) -> str:
    parts = []
    for name, (p, log_path) in procs_logs.items():
        try:
            content = (
                log_path.read_text(encoding="utf-8", errors="replace")
                if log_path.exists()
                else ""
            )
        except OSError as exc:
            content = f"<failed to read {log_path}: {exc!r}>"
        parts.append(
            f"--- {name} (pid={p.pid}, rc={p.returncode}) ---\n{content}\n"
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
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()

    server_log = logs_dir / "server.log"
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
        ],
        server_log,
    )
    port = _read_port_announcement(server_log, server)
    base_url = f"http://127.0.0.1:{port}"

    planner_env_file = tmp_path / "planner.env"
    planner_env_file.write_text(
        f"EDEN_BASE_COMMIT_SHA={base_sha}\nEDEN_PROPOSALS_PER_PLAN=1\n",
        encoding="utf-8",
    )

    planner_log = logs_dir / "planner.log"
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
        ],
        planner_log,
    )
    implementer_log = logs_dir / "implementer.log"
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
        ],
        implementer_log,
    )
    evaluator_log = logs_dir / "evaluator.log"
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
        ],
        evaluator_log,
    )
    orchestrator_log = logs_dir / "orchestrator.log"
    # Quiescence tolerance must exceed subprocess-mode worker
    # startup-to-first-claim latency; with the default 0.3s the
    # orchestrator declares quiescence before workers come online and
    # every plan task stays pending.
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
            "--poll-interval",
            "0.5",
            "--max-quiescent-iterations",
            "20",
        ],
        orchestrator_log,
    )

    procs_logs = {
        "task-store-server": (server, server_log),
        "planner": (planner, planner_log),
        "implementer": (implementer, implementer_log),
        "evaluator": (evaluator, evaluator_log),
        "orchestrator": (orchestrator, orchestrator_log),
    }

    try:
        try:
            rc = orchestrator.wait(timeout=120)
        except subprocess.TimeoutExpired:
            for p, _ in procs_logs.values():
                _terminate(p)
            pytest.fail(
                "orchestrator did not exit within 120s. Subprocess output:\n"
                + _dump_logs(procs_logs)
            )
        if rc != 0:
            for p, _ in procs_logs.values():
                _terminate(p)
            pytest.fail(
                f"orchestrator exited {rc}. Subprocess output:\n"
                + _dump_logs(procs_logs)
            )

        for name in ("planner", "implementer", "evaluator", "task-store-server"):
            _terminate(procs_logs[name][0])

        store = SqliteStore(
            experiment_id=experiment_id,
            path=str(db_path),
            metrics_schema=MetricsSchema({"score": "real"}),
        )
        try:
            trials = store.list_trials(status="success")
            if len(trials) != 3:
                pytest.fail(
                    f"expected 3 success trials, got {len(trials)}. Output:\n"
                    + _dump_logs(procs_logs)
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
        for p, _ in procs_logs.values():
            _terminate(p)
