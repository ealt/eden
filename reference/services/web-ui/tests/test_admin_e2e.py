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


def _spawn(args: list[str]) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _read_port(
    proc: subprocess.Popen, pattern: re.Pattern[str], timeout: float = 10.0
) -> int:
    deadline = time.monotonic() + timeout
    assert proc.stdout is not None
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                raise RuntimeError(f"subprocess exited early rc={proc.returncode}")
            continue
        m = pattern.match(line.strip())
        if m is not None:
            kv = dict(p.split("=", 1) for p in m.group(1).split())
            return int(kv["port"])
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


def _dump_stderr(procs: dict[str, subprocess.Popen]) -> str:
    parts = []
    for name, p in procs.items():
        stderr = ""
        if p.stderr is not None:
            try:
                stderr = p.stderr.read() or ""
            except Exception as exc:  # noqa: BLE001
                stderr = f"<failed to read stderr: {exc!r}>"
        parts.append(f"--- {name} (pid={p.pid}, rc={p.returncode}) ---\n{stderr}\n")
    return "\n".join(parts)


@pytest.mark.e2e
def test_admin_reclaim_round_trip(tmp_path: Path) -> None:
    """Real-process admin reclaim: claimed → pending via /admin POST."""
    db_path = tmp_path / "eden.sqlite"
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    experiment_id = "exp-admin-e2e"
    token = "admin-e2e-token"

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
    server_port = _read_port(server, _TASK_STORE_RE)
    task_store_url = f"http://127.0.0.1:{server_port}"

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
        ]
    )
    web_port = _read_port(web_ui, _WEB_UI_RE)
    web_url = f"http://127.0.0.1:{web_port}"

    procs = {"task-store-server": server, "web-ui": web_ui}

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
                    + _dump_stderr(procs)
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
        _terminate(web_ui)
        _terminate(server)
