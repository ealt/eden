"""Real-subprocess end-to-end test for the admin ideas module.

Spawns task-store-server + web-ui, pre-seeds an ideation submission
via direct ``StoreClient`` so the lineage walk has something to find,
then drives the ``/admin/ideas/`` list + detail pages via real HTTP
and verifies the lineage links resolve.

Pattern follows ``test_admin_workers_e2e.py``.
"""

from __future__ import annotations

import re
import signal
import subprocess
import sys
import time
from pathlib import Path

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
def test_admin_ideas_list_and_detail_resolve_lineage(tmp_path: Path) -> None:
    db_path = tmp_path / "eden.sqlite"
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    credentials_dir = tmp_path / "credentials"
    credentials_dir.mkdir()
    # Identity rename (#128): opaque experiment id.
    experiment_id = "exp_0123456789abcdefghjkmnpqrs"
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

    # Identity rename (#128): mint all workers + reserved groups BEFORE
    # the web-ui spawn (server-minted opaque ids); persist the web-ui
    # worker credential for its startup bootstrap.
    from eden_contracts import Idea
    from eden_service_common.auth import credential_path
    from eden_storage import IdeaSubmission
    from eden_wire import StoreClient

    admin = StoreClient(
        base_url=task_store_url,
        experiment_id=experiment_id,
        bearer=f"admin:{admin_token}",
    )
    try:
        ui_worker, ui_token = admin.register_worker("ui-admin-ideas")
        ui_worker_id = ui_worker.worker_id
        assert ui_token is not None
        exp_cred_dir = credentials_dir / experiment_id
        exp_cred_dir.mkdir(parents=True, exist_ok=True)
        credential_path(exp_cred_dir, ui_worker_id).write_text(ui_token)

        op_worker, op_token = admin.register_worker("operator-e2e")
        operator_id = op_worker.worker_id
        ideator_worker, ideator_token = admin.register_worker("ideator-e2e")
        ideator_id = ideator_worker.worker_id
        # §13.3: business ops need a worker in admins / orchestrators.
        admin.register_group(
            "admins",
            members=[operator_id, ui_worker_id],
            allow_reserved=True,
        )
        admin.register_group(
            "orchestrators", members=[operator_id], allow_reserved=True
        )
    finally:
        admin.close()

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
            ui_worker_id,
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
        operator = StoreClient(
            base_url=task_store_url,
            experiment_id=experiment_id,
            bearer=f"{operator_id}:{op_token}",
        )
        ideator = StoreClient(
            base_url=task_store_url,
            experiment_id=experiment_id,
            bearer=f"{ideator_id}:{ideator_token}",
        )
        try:
            operator.create_ideation_task("plan-e2e")
            claim = ideator.claim("plan-e2e", ideator_id)
            idea_id = "idea-e2e"
            ideator.create_idea(
                Idea(
                    idea_id=idea_id,
                    experiment_id=experiment_id,
                    slug="e2e",
                    priority=1.0,
                    parent_commits=["a" * 40],
                    artifacts_uri="https://example.invalid/x.md",
                    state="drafting",
                    created_at="2026-04-24T11:00:00Z",
                )
            )
            ideator.mark_idea_ready(idea_id)
            ideator.submit(
                "plan-e2e",
                claim.worker_id,
                IdeaSubmission(status="success", idea_ids=(idea_id,)),
            )
            operator.accept("plan-e2e")
        finally:
            operator.close()
            ideator.close()

        # Drive the UI: sign in, list ideas, click into the seeded
        # idea, verify the lineage link to /admin/tasks/plan-e2e/.
        with httpx.Client(base_url=web_url, timeout=10.0) as ui:
            resp = ui.post("/signin", follow_redirects=False)
            assert resp.status_code == 303

            resp = ui.get("/admin/ideas/")
            if resp.status_code != 200:
                pytest.fail(
                    f"/admin/ideas/ returned {resp.status_code}\n"
                    + _dump_logs(procs_logs)
                )
            assert idea_id in resp.text

            resp = ui.get(f"/admin/ideas/{idea_id}/")
            if resp.status_code != 200:
                pytest.fail(
                    f"/admin/ideas/{idea_id}/ returned {resp.status_code}\n"
                    + _dump_logs(procs_logs)
                )
            assert "/admin/tasks/plan-e2e/" in resp.text

            resp = ui.get("/admin/tasks/plan-e2e/")
            assert resp.status_code == 200
            # The ideation-task page lists the produced idea (forward
            # one-hop lineage).
            assert f"/admin/ideas/{idea_id}/" in resp.text
    finally:
        for p, _ in procs_logs.values():
            _terminate(p)
