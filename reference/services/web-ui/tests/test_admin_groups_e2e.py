"""Real-subprocess end-to-end test for the groups admin module.

Spawns task-store-server + web-ui with admin auth enabled. Registers
a group through the UI, adds a worker as a member, then verifies via
a separate ``StoreClient`` that a task with ``target={kind:"group",
id:G}`` is claimable by the member and rejected for a non-member.
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
def test_admin_groups_register_and_target_claim(tmp_path: Path) -> None:
    """Real-process groups admin: register group through UI → group-targeted claim works."""
    db_path = tmp_path / "eden.sqlite"
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    credentials_dir = tmp_path / "credentials"
    credentials_dir.mkdir()
    experiment_id = "exp-admin-groups-e2e"
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
            "ui-admin-groups",
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
        # Seed two workers via an admin StoreClient — we need their
        # registration tokens to exercise the target-claim assertion.
        from eden_wire import StoreClient

        admin = StoreClient(
            base_url=task_store_url,
            experiment_id=experiment_id,
            bearer=f"admin:{admin_token}",
        )
        try:
            member_worker, member_token = admin.register_worker("member-w")
            assert member_token is not None
            other_worker, other_token = admin.register_worker("non-member-w")
            assert other_token is not None
            # 12a-2 wave 3 §3.7: create_task(kind=ideation) requires the
            # caller to be a transitive member of `admins` or
            # `orchestrators`. The test creates the seed task as
            # ``member-w``, so register the `admins` group (idempotent on
            # existing) and add ``member-w`` to it. Doesn't affect the
            # downstream group-target claim assertion — `e2e-group`
            # (created below) is the target gate, not `admins`.
            import contextlib

            from eden_storage.errors import AlreadyExists

            with contextlib.suppress(AlreadyExists):
                admin.register_group("admins")
            admin.add_to_group("admins", "member-w")
            # Issue #144: also add the web-ui worker to admins so its
            # /admin/* page loads pass the route-layer gate.
            admin.add_to_group("admins", "ui-admin-groups")
        finally:
            admin.close()

        group_id = "e2e-group"
        with httpx.Client(base_url=web_url, timeout=10.0) as ui:
            resp = ui.post("/signin", follow_redirects=False)
            assert resp.status_code == 303

            # Register the group through the UI.
            resp = ui.get("/admin/groups/")
            assert resp.status_code == 200
            m = re.search(r'name="csrf_token"\s+value="([^"]+)"', resp.text)
            assert m is not None
            csrf = m.group(1)
            resp = ui.post(
                "/admin/groups/",
                content=urlencode({"csrf_token": csrf, "group_id": group_id}),
                headers={"content-type": "application/x-www-form-urlencoded"},
                follow_redirects=False,
            )
            if resp.status_code != 303:
                pytest.fail(
                    f"register-group returned {resp.status_code}: {resp.text}\n"
                    + _dump_logs(procs_logs)
                )
            assert f"/admin/groups/{group_id}/?ok=registered" in resp.headers["location"]

            # Add the member through the UI.
            resp = ui.get(f"/admin/groups/{group_id}/")
            assert resp.status_code == 200
            m = re.search(r'name="csrf_token"\s+value="([^"]+)"', resp.text)
            assert m is not None
            csrf = m.group(1)
            resp = ui.post(
                f"/admin/groups/{group_id}/members",
                content=urlencode(
                    {"csrf_token": csrf, "member_id": "member-w"}
                ),
                headers={"content-type": "application/x-www-form-urlencoded"},
                follow_redirects=False,
            )
            assert resp.status_code == 303, resp.text
            assert "ok=added" in resp.headers["location"]

        # Now seed a task targeting the group. ``create_task`` is
        # worker-gated (admin bearer would 403 per chapter 07 §13.3),
        # so we use the member's worker bearer. ``create_ideation_task``
        # doesn't take a ``target`` kw, so we build the task model
        # directly.
        from datetime import UTC, datetime

        from eden_contracts import IdeationTask

        # Spec format requires trailing 'Z' (no offset).
        now_iso = (
            datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        )
        creator = StoreClient(
            base_url=task_store_url,
            experiment_id=experiment_id,
            bearer=f"member-w:{member_token}",
        )
        try:
            creator.create_task(
                IdeationTask.model_validate(
                    {
                        "task_id": "task-group-target",
                        "kind": "ideation",
                        "state": "pending",
                        "created_at": now_iso,
                        "updated_at": now_iso,
                        "payload": {"experiment_id": experiment_id},
                        "target": {"kind": "group", "id": group_id},
                    }
                )
            )
        finally:
            creator.close()

        # Non-member cannot claim the task.
        from eden_storage.errors import WorkerNotEligible

        non_member = StoreClient(
            base_url=task_store_url,
            experiment_id=experiment_id,
            bearer=f"non-member-w:{other_token}",
        )
        try:
            with pytest.raises(WorkerNotEligible):
                non_member.claim("task-group-target", "non-member-w")
        finally:
            non_member.close()

        # Member CAN claim.
        member = StoreClient(
            base_url=task_store_url,
            experiment_id=experiment_id,
            bearer=f"member-w:{member_token}",
        )
        try:
            claim = member.claim("task-group-target", "member-w")
            assert claim.worker_id == "member-w"
        finally:
            member.close()
    finally:
        for p, _ in procs_logs.values():
            _terminate(p)
