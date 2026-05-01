"""Real-subprocess end-to-end test for the Web UI.

Two scenarios:

1. ``test_planner_full_flow_through_ui`` — forks task-store-server
   and web-ui, drives a one-task plan flow through the UI's HTTP
   surface, asserts the resulting task-store state.
2. ``test_stranded_claim_recovered_by_orchestrator_loop`` — adds
   a real orchestrator subprocess to prove that the per-iteration
   ``sweep_expired_claims`` call wired into ``run_orchestrator_loop``
   is what actually reclaims an abandoned UI claim.
"""

from __future__ import annotations

import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx
import pytest
from eden_contracts import MetricsSchema
from eden_storage import SqliteStore

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="subprocess signal handling is POSIX-only",
)

FIXTURE_CONFIG = (
    Path(__file__).resolve().parents[4]
    / "tests"
    / "fixtures"
    / "experiment"
    / ".eden"
    / "config.yaml"
)


def _spawn(args: list[str], log_path: Path) -> subprocess.Popen:
    # See eden#39: undrained subprocess.PIPE handles fill the 64 KiB
    # pipe buffer and wedge the writer's async event loop.
    return subprocess.Popen(
        [sys.executable, "-m", *args],
        stdout=open(log_path, "wb"),  # noqa: SIM115 — handle owned by Popen
        stderr=subprocess.STDOUT,
    )


_TASK_STORE_RE = re.compile(r"^EDEN_TASK_STORE_LISTENING\s+(.*)$")
_WEB_UI_RE = re.compile(r"^EDEN_WEB_UI_LISTENING\s+(.*)$")


def _read_announcement(
    log_path: Path,
    proc: subprocess.Popen,
    pattern: re.Pattern[str],
    timeout: float = 10.0,
) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if log_path.exists():
            try:
                content = log_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                content = ""
            for line in content.splitlines():
                m = pattern.match(line.strip())
                if m is not None:
                    kv = dict(p.split("=", 1) for p in m.group(1).split())
                    return int(kv["port"])
        if proc.poll() is not None:
            raise RuntimeError(
                f"subprocess exited early with code {proc.returncode}"
            )
        time.sleep(0.05)
    raise RuntimeError("subprocess did not announce its port in time")


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
def test_planner_full_flow_through_ui(tmp_path: Path) -> None:
    """Sign in, claim a plan task, draft + submit, verify state in the store."""
    db_path = tmp_path / "eden.sqlite"
    artifacts_dir = tmp_path / "artifacts"
    experiment_id = "exp-web-ui-e2e"
    token = "e2e-test-token"
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
    server_port = _read_announcement(server_log, server, _TASK_STORE_RE)
    task_store_url = f"http://127.0.0.1:{server_port}"

    web_ui_log = logs_dir / "web-ui.log"
    web_ui = _spawn(
        [
            "eden_web_ui",
            "--task-store-url",
            task_store_url,
            "--experiment-id",
            experiment_id,
            "--shared-token",
            token,
            "--experiment-config",
            str(FIXTURE_CONFIG),
            "--session-secret",
            "x" * 32,
            "--worker-id",
            "ui-w",
            "--artifacts-dir",
            str(artifacts_dir),
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--claim-ttl-seconds",
            "60",
        ],
        web_ui_log,
    )
    web_port = _read_announcement(web_ui_log, web_ui, _WEB_UI_RE)
    web_url = f"http://127.0.0.1:{web_port}"

    procs_logs = {
        "task-store-server": (server, server_log),
        "web-ui": (web_ui, web_ui_log),
    }

    try:
        # Seed one plan task via the wire StoreClient.
        from eden_wire import StoreClient

        seed = StoreClient(
            base_url=task_store_url,
            experiment_id=experiment_id,
            token=token,
        )
        try:
            seed.create_plan_task("t-ui-1")
        finally:
            seed.close()

        with httpx.Client(base_url=web_url, timeout=10.0) as ui:
            # Sign in.
            resp = ui.post("/signin", follow_redirects=False)
            assert resp.status_code == 303

            # GET /planner/ to render and parse out the CSRF token.
            resp = ui.get("/planner/")
            assert resp.status_code == 200
            assert "t-ui-1" in resp.text
            m = re.search(
                r'name="csrf_token"\s+value="([^"]+)"', resp.text
            )
            assert m is not None, "csrf token not found in planner list"
            csrf = m.group(1)

            # Claim.
            resp = ui.post(
                "/planner/t-ui-1/claim",
                content=urlencode({"csrf_token": csrf}),
                headers={"content-type": "application/x-www-form-urlencoded"},
                follow_redirects=False,
            )
            assert resp.status_code == 303

            # Draft form.
            resp = ui.get("/planner/t-ui-1/draft")
            assert resp.status_code == 200

            # Submit success with one proposal.
            body = urlencode(
                [
                    ("csrf_token", csrf),
                    ("status", "success"),
                    ("slug", "e2e-feat"),
                    ("priority", "1.0"),
                    ("parent_commits", "a" * 40),
                    ("rationale", "## why\n\nbecause."),
                ]
            )
            resp = ui.post(
                "/planner/t-ui-1/submit",
                content=body,
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
            if resp.status_code != 200:
                pytest.fail(
                    f"submit returned {resp.status_code}: {resp.text}\n"
                    + _dump_logs(procs_logs)
                )
            assert "submitted" in resp.text.lower()

        # Tear down processes before reading SQLite (release file lock).
        for p, _ in procs_logs.values():
            _terminate(p)

        store = SqliteStore(
            experiment_id=experiment_id,
            path=str(db_path),
            metrics_schema=MetricsSchema({"score": "real"}),
        )
        try:
            assert store.read_task("t-ui-1").state == "submitted"
            ready = store.list_proposals(state="ready")
            assert len(ready) == 1
            assert ready[0].slug == "e2e-feat"
            # Artifact file exists at the URI.
            uri = ready[0].artifacts_uri
            assert uri.startswith("file://")
            assert Path(uri.removeprefix("file://")).read_text(
                encoding="utf-8"
            ).startswith("## why")
        finally:
            store.close()
    finally:
        for p, _ in procs_logs.values():
            _terminate(p)


@pytest.mark.e2e
def test_stranded_claim_recovered_by_orchestrator_loop(tmp_path: Path) -> None:
    """End-to-end: UI claims with TTL, orchestrator's loop sweeps + reclaims.

    Spawns three real processes (task-store-server + web-ui +
    orchestrator) and proves the operational path the plan
    promised: the orchestrator's per-iteration sweep is what
    actually reclaims an abandoned claim. The orchestrator is
    launched with no plan tasks so it does the sweep, sees no
    other progress, and quiesces.
    """
    from eden_service_common import seed_bare_repo

    db_path = tmp_path / "eden.sqlite"
    artifacts_dir = tmp_path / "artifacts"
    bare_repo = tmp_path / "bare-repo.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch", "main", str(bare_repo)],
        check=True,
        capture_output=True,
    )
    seed_bare_repo(str(bare_repo))

    experiment_id = "exp-strand"
    token = "strand-token"
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
    server_port = _read_announcement(server_log, server, _TASK_STORE_RE)
    task_store_url = f"http://127.0.0.1:{server_port}"

    web_ui_log = logs_dir / "web-ui.log"
    web_ui = _spawn(
        [
            "eden_web_ui",
            "--task-store-url",
            task_store_url,
            "--experiment-id",
            experiment_id,
            "--shared-token",
            token,
            "--experiment-config",
            str(FIXTURE_CONFIG),
            "--session-secret",
            "x" * 32,
            "--worker-id",
            "ui-w",
            "--artifacts-dir",
            str(artifacts_dir),
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--claim-ttl-seconds",
            "1",
        ],
        web_ui_log,
    )
    web_port = _read_announcement(web_ui_log, web_ui, _WEB_UI_RE)
    web_url = f"http://127.0.0.1:{web_port}"

    procs_logs: dict[str, tuple[subprocess.Popen, Path]] = {
        "task-store-server": (server, server_log),
        "web-ui": (web_ui, web_ui_log),
    }

    try:
        # Seed one plan task via the wire StoreClient.
        from eden_wire import StoreClient

        seed = StoreClient(
            base_url=task_store_url,
            experiment_id=experiment_id,
            token=token,
        )
        try:
            seed.create_plan_task("t-strand")
        finally:
            seed.close()

        # Claim through UI with 1s TTL.
        with httpx.Client(base_url=web_url, timeout=10.0) as ui:
            ui.post("/signin", follow_redirects=False)
            resp = ui.get("/planner/")
            m = re.search(r'name="csrf_token"\s+value="([^"]+)"', resp.text)
            assert m is not None
            csrf = m.group(1)
            resp = ui.post(
                "/planner/t-strand/claim",
                content=urlencode({"csrf_token": csrf}),
                headers={"content-type": "application/x-www-form-urlencoded"},
                follow_redirects=False,
            )
            assert resp.status_code == 303

        # Wait past the TTL so the orchestrator's next iteration's
        # sweep_expired_claims call will reclaim.
        time.sleep(2.0)

        # Now spawn the orchestrator with NO plan tasks; its loop runs
        # sweep_expired_claims once per iteration, sees no other
        # progress, and quiesces.
        orchestrator_log = logs_dir / "orchestrator.log"
        orchestrator = _spawn(
            [
                "eden_orchestrator",
                "--task-store-url",
                task_store_url,
                "--experiment-id",
                experiment_id,
                "--shared-token",
                token,
                "--repo-path",
                str(bare_repo),
                "--plan-tasks",
                "",
                "--max-quiescent-iterations",
                "2",
                "--poll-interval",
                "0.1",
            ],
            orchestrator_log,
        )
        procs_logs["orchestrator"] = (orchestrator, orchestrator_log)
        try:
            rc = orchestrator.wait(timeout=30)
        except subprocess.TimeoutExpired:
            for p, _ in procs_logs.values():
                _terminate(p)
            pytest.fail(
                "orchestrator did not quiesce within 30s. Output:\n"
                + _dump_logs(procs_logs)
            )
        if rc != 0:
            for p, _ in procs_logs.values():
                _terminate(p)
            pytest.fail(
                f"orchestrator exited {rc}. Output:\n" + _dump_logs(procs_logs)
            )

        # Verify state via a separate StoreClient.
        client = StoreClient(
            base_url=task_store_url,
            experiment_id=experiment_id,
            token=token,
        )
        try:
            assert client.read_task("t-strand").state == "pending"
            events = list(client.read_range())
            reclaimed = [
                e
                for e in events
                if e.type == "task.reclaimed"
                and e.data.get("task_id") == "t-strand"
            ]
            assert len(reclaimed) == 1
            assert reclaimed[0].data["cause"] == "expired"
        finally:
            client.close()
    finally:
        for p, _ in procs_logs.values():
            _terminate(p)
