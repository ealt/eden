"""Real-subprocess end-to-end test for the admin module.

Spawns task-store-server + web-ui (no orchestrator), drives a planner
submit through real HTTP so a task moves to ``submitted``, then hits
``POST /admin/tasks/<id>/reclaim`` and verifies via a separate
``StoreClient`` that the task is back in ``pending`` and the
``task.reclaimed`` event with ``cause=operator`` is in the log.
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

_TASK_STORE_RE = re.compile(r"^EDEN_TASK_STORE_LISTENING\s+(.*)$")
_WEB_UI_RE = re.compile(r"^EDEN_WEB_UI_LISTENING\s+(.*)$")


def _spawn(args: list[str], log_path: Path) -> subprocess.Popen:
    # See eden#39: undrained subprocess.PIPE handles fill the 64 KiB
    # pipe buffer and wedge the writer's async event loop.
    return subprocess.Popen(
        [sys.executable, "-m", *args],
        stdout=open(log_path, "wb"),  # noqa: SIM115 — handle owned by Popen
        stderr=subprocess.STDOUT,
    )


def _read_port(
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
            raise RuntimeError(f"subprocess exited early rc={proc.returncode}")
        time.sleep(0.05)
    raise RuntimeError("subprocess did not announce its port in time")


def _terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=5.0)
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
def test_admin_reclaim_round_trip(tmp_path: Path) -> None:
    """Real-process admin reclaim: claimed → pending via /admin POST."""
    db_path = tmp_path / "eden.sqlite"
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    experiment_id = "exp-admin-e2e"
    token = "admin-e2e-token"
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
    server_port = _read_port(server_log, server, _TASK_STORE_RE)
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
            "z" * 32,
            "--worker-id",
            "ui-admin",
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
    web_port = _read_port(web_ui_log, web_ui, _WEB_UI_RE)
    web_url = f"http://127.0.0.1:{web_port}"

    procs_logs = {
        "task-store-server": (server, server_log),
        "web-ui": (web_ui, web_ui_log),
    }

    try:
        from eden_wire import StoreClient

        seed = StoreClient(
            base_url=task_store_url,
            experiment_id=experiment_id,
            token=token,
        )
        try:
            seed.create_plan_task("t-plan-1")
        finally:
            seed.close()

        with httpx.Client(base_url=web_url, timeout=10.0) as ui:
            resp = ui.post("/signin", follow_redirects=False)
            assert resp.status_code == 303

            resp = ui.get("/planner/")
            assert resp.status_code == 200
            m = re.search(r'name="csrf_token"\s+value="([^"]+)"', resp.text)
            assert m is not None
            csrf = m.group(1)

            resp = ui.post(
                "/planner/t-plan-1/claim",
                content=urlencode({"csrf_token": csrf}),
                headers={"content-type": "application/x-www-form-urlencoded"},
                follow_redirects=False,
            )
            assert resp.status_code == 303

            resp = ui.get("/admin/tasks/?state=claimed")
            assert resp.status_code == 200, resp.text
            assert "t-plan-1" in resp.text

            resp = ui.post(
                "/admin/tasks/t-plan-1/reclaim",
                content=urlencode({"csrf_token": csrf}),
                headers={"content-type": "application/x-www-form-urlencoded"},
                follow_redirects=False,
            )
            if resp.status_code != 303:
                pytest.fail(
                    f"reclaim returned {resp.status_code}: {resp.text}\n"
                    + _dump_logs(procs_logs)
                )
            assert "?reclaimed=ok" in resp.headers["location"]

        verify = StoreClient(
            base_url=task_store_url,
            experiment_id=experiment_id,
            token=token,
        )
        try:
            task = verify.read_task("t-plan-1")
            assert task.state == "pending"
            events = verify.replay()
            reclaim_events = [e for e in events if e.type == "task.reclaimed"]
            assert len(reclaim_events) == 1
            assert reclaim_events[0].data["cause"] == "operator"
        finally:
            verify.close()
    finally:
        for p, _ in procs_logs.values():
            _terminate(p)
