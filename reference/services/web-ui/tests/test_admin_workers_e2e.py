"""Real-subprocess end-to-end test for the workers admin module.

Spawns task-store-server + web-ui with admin auth ENABLED, drives
register/reissue through the UI, extracts the one-shot token from
the rendered HTML, and verifies the resulting credential
authenticates against ``GET /whoami`` via a separate ``StoreClient``.
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
    return subprocess.Popen(
        [sys.executable, "-m", *args],
        stdout=open(log_path, "wb"),  # noqa: SIM115 — handle owned by Popen
        stderr=subprocess.STDOUT,
    )


def _read_port(
    log_path: Path,
    proc: subprocess.Popen,
    pattern: re.Pattern[str],
    timeout: float = 15.0,
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
def test_admin_workers_register_and_authenticate(tmp_path: Path) -> None:
    """Real-process workers admin: register through UI → credential authenticates."""
    db_path = tmp_path / "eden.sqlite"
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    credentials_dir = tmp_path / "credentials"
    credentials_dir.mkdir()
    experiment_id = "exp-admin-workers-e2e"
    admin_token = "z" * 64
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
            "--admin-token",
            admin_token,
            "--host",
            "127.0.0.1",
            "--port",
            "0",
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
            "--experiment-config",
            str(FIXTURE_CONFIG),
            "--session-secret",
            "z" * 32,
            "--worker-id",
            "ui-admin-workers",
            "--admin-token",
            admin_token,
            "--credentials-dir",
            str(credentials_dir),
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
        with httpx.Client(base_url=web_url, timeout=10.0) as ui:
            # Sign in to get a session + CSRF.
            resp = ui.post("/signin", follow_redirects=False)
            assert resp.status_code == 303

            resp = ui.get("/admin/workers/")
            assert resp.status_code == 200
            m = re.search(r'name="csrf_token"\s+value="([^"]+)"', resp.text)
            assert m is not None
            csrf = m.group(1)

            # Register a fresh worker through the UI.
            new_worker_id = "e2e-new-worker"
            resp = ui.post(
                "/admin/workers/",
                content=urlencode(
                    {
                        "csrf_token": csrf,
                        "worker_id": new_worker_id,
                        "labels": "role=ideator",
                    }
                ),
                headers={"content-type": "application/x-www-form-urlencoded"},
                follow_redirects=False,
            )
            if resp.status_code != 200:
                pytest.fail(
                    f"register returned {resp.status_code}: {resp.text}\n"
                    + _dump_logs(procs_logs)
                )
            # Extract the one-shot token from the response HTML.
            tok_m = re.search(
                r'<code class="token">([^<]+)</code>', resp.text
            )
            assert tok_m is not None, "token page did not render the token"
            token = tok_m.group(1)
            assert resp.headers.get("cache-control") == "no-store"

        # Verify the new credential authenticates via a separate
        # StoreClient using the new worker's bearer.
        from eden_wire import StoreClient

        verify = StoreClient(
            base_url=task_store_url,
            experiment_id=experiment_id,
            bearer=f"{new_worker_id}:{token}",
        )
        try:
            assert verify.whoami() == new_worker_id
        finally:
            verify.close()

        # Reissue via the UI; old token should now fail; new one works.
        with httpx.Client(base_url=web_url, timeout=10.0) as ui2:
            resp = ui2.post("/signin", follow_redirects=False)
            assert resp.status_code == 303
            resp = ui2.get(f"/admin/workers/{new_worker_id}/")
            assert resp.status_code == 200
            m = re.search(r'name="csrf_token"\s+value="([^"]+)"', resp.text)
            assert m is not None
            csrf = m.group(1)

            resp = ui2.post(
                f"/admin/workers/{new_worker_id}/reissue-credential",
                content=urlencode({"csrf_token": csrf}),
                headers={"content-type": "application/x-www-form-urlencoded"},
                follow_redirects=False,
            )
            assert resp.status_code == 200
            tok_m = re.search(
                r'<code class="token">([^<]+)</code>', resp.text
            )
            assert tok_m is not None
            new_token = tok_m.group(1)
            assert new_token != token

        # Old credential MUST now fail.
        from eden_wire.errors import Unauthorized

        old_client = StoreClient(
            base_url=task_store_url,
            experiment_id=experiment_id,
            bearer=f"{new_worker_id}:{token}",
        )
        try:
            with pytest.raises(Unauthorized):
                old_client.whoami()
        finally:
            old_client.close()

        # New credential MUST succeed.
        new_client = StoreClient(
            base_url=task_store_url,
            experiment_id=experiment_id,
            bearer=f"{new_worker_id}:{new_token}",
        )
        try:
            assert new_client.whoami() == new_worker_id
        finally:
            new_client.close()
    finally:
        for p, _ in procs_logs.values():
            _terminate(p)
