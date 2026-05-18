"""Checkpoint authority conformance scenarios — chapter 07 §14, §13.3.

Per chapter 9 §5 "Checkpoint authority": the three §14 checkpoint
endpoints require the deployment-admin bearer (literal ``admin``
principal per §13.1); worker bearers receive 403
``eden://error/forbidden``. The wave-4 spec amendment makes this
uniform across export / import / read_experiment.

The default ReferenceAdapter runs auth-disabled, so this file spawns
its own auth-enabled task-store-server (same pattern as
``test_worker_auth_enabled``) to exercise the §13 middleware.
"""

from __future__ import annotations

import queue
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = "Checkpoint authority"

_FIXTURES_DIR = Path(__file__).resolve().parents[1] / "src" / "conformance" / "fixtures"
_EXPERIMENT_CONFIG = _FIXTURES_DIR / "minimal-experiment.yaml"
_PORT_ANNOUNCE_TIMEOUT = 15.0


def _spawn_auth_server(
    experiment_config: Path, experiment_id: str, admin_token: str
) -> tuple[subprocess.Popen[str], int]:
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "eden_task_store_server",
            "--store-url",
            ":memory:",
            "--experiment-id",
            experiment_id,
            "--experiment-config",
            str(experiment_config),
            "--admin-token",
            admin_token,
            "--port",
            "0",
            "--host",
            "127.0.0.1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    port = _read_port_announcement(proc)
    return proc, port


def _read_port_announcement(proc: subprocess.Popen[str]) -> int:
    assert proc.stdout is not None
    q: queue.Queue[str] = queue.Queue()

    def _reader() -> None:
        assert proc.stdout is not None
        try:
            for line in iter(proc.stdout.readline, ""):
                q.put(line)
        finally:
            q.put("")

    threading.Thread(target=_reader, daemon=True).start()
    deadline = time.monotonic() + _PORT_ANNOUNCE_TIMEOUT
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("task-store-server did not announce port in time")
        try:
            line = q.get(timeout=min(remaining, 0.5))
        except queue.Empty:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"task-store-server exited early rc={proc.returncode}"
                ) from None
            continue
        if line == "":
            raise RuntimeError("task-store-server closed stdout before announcing port")
        if line.startswith("EDEN_TASK_STORE_LISTENING"):
            parts = dict(p.split("=", 1) for p in line.strip().split()[1:])
            return int(parts["port"])


@pytest.fixture
def auth_server(tmp_path: Path) -> Iterator[dict[str, str]]:
    experiment_id = f"auth-cp-{uuid.uuid4().hex[:8]}"
    admin_token = secrets.token_hex(24)
    cfg_copy = tmp_path / "experiment-config.yaml"
    shutil.copyfile(_EXPERIMENT_CONFIG, cfg_copy)
    proc, port = _spawn_auth_server(cfg_copy, experiment_id, admin_token)
    try:
        yield {
            "base_url": f"http://127.0.0.1:{port}",
            "experiment_id": experiment_id,
            "admin_token": admin_token,
        }
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2.0)


def _client(server: dict[str, str], *, bearer: str | None) -> httpx.Client:
    headers = {
        "Accept": "application/json, application/problem+json",
    }
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"
    return httpx.Client(
        base_url=server["base_url"], headers=headers, timeout=15.0
    )


def test_export_admin_succeeds(auth_server: dict[str, str]) -> None:
    """spec/v0/07-wire-protocol.md §14.1 — admin bearer can export.

    Per §14.1 the admin principal authorizes the export endpoint;
    chapter 9 §5 "Checkpoint authority" group covers this MUST.
    """
    with _client(auth_server, bearer=f"admin:{auth_server['admin_token']}") as c:
        resp = c.post(
            f"/v0/experiments/{auth_server['experiment_id']}/checkpoint",
            headers={"X-Eden-Experiment-Id": auth_server["experiment_id"]},
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"] == "application/x-eden-checkpoint+tar"


def test_export_worker_bearer_rejected(auth_server: dict[str, str]) -> None:
    """spec/v0/07-wire-protocol.md §14.1 — worker bearer is rejected.

    Per §14 the checkpoint endpoints are admin-gated on the literal
    ``admin`` principal; a worker bearer MUST be rejected with 403
    ``eden://error/forbidden``. Registers a worker first, then probes
    the endpoint with that worker's bearer.
    """
    admin_bearer = f"admin:{auth_server['admin_token']}"
    with _client(auth_server, bearer=admin_bearer) as c:
        reg = c.post(
            f"/v0/experiments/{auth_server['experiment_id']}/workers",
            headers={"X-Eden-Experiment-Id": auth_server["experiment_id"]},
            json={"worker_id": "no-export-w"},
        )
        reg.raise_for_status()
        worker_token = reg.json()["registration_token"]

    with _client(auth_server, bearer=f"no-export-w:{worker_token}") as c:
        resp = c.post(
            f"/v0/experiments/{auth_server['experiment_id']}/checkpoint",
            headers={"X-Eden-Experiment-Id": auth_server["experiment_id"]},
        )
        assert resp.status_code == 403
        assert resp.json()["type"] == "eden://error/forbidden"


def test_read_experiment_worker_bearer_rejected(
    auth_server: dict[str, str],
) -> None:
    """spec/v0/07-wire-protocol.md §14.3 — worker bearer is rejected on read_experiment.

    Per §14.3 the full ``read_experiment`` endpoint is admin-gated;
    worker bearers MUST receive 403 ``eden://error/forbidden``.
    """
    admin_bearer = f"admin:{auth_server['admin_token']}"
    with _client(auth_server, bearer=admin_bearer) as c:
        reg = c.post(
            f"/v0/experiments/{auth_server['experiment_id']}/workers",
            headers={"X-Eden-Experiment-Id": auth_server["experiment_id"]},
            json={"worker_id": "no-read-w"},
        )
        reg.raise_for_status()
        worker_token = reg.json()["registration_token"]

    with _client(auth_server, bearer=f"no-read-w:{worker_token}") as c:
        resp = c.get(
            f"/v0/experiments/{auth_server['experiment_id']}",
            headers={"X-Eden-Experiment-Id": auth_server["experiment_id"]},
        )
        assert resp.status_code == 403
        assert resp.json()["type"] == "eden://error/forbidden"


def test_import_unauthenticated_rejected(auth_server: dict[str, str]) -> None:
    """spec/v0/07-wire-protocol.md §13.3 — unauthenticated import is rejected.

    Per §13.3 every ``/v0/`` request MUST carry a valid bearer; the
    import endpoint inherits this from the §13 middleware and the
    §14.2 admin gate. A request without a bearer returns 401
    ``eden://error/unauthorized``.
    """
    with _client(auth_server, bearer=None) as c:
        resp = c.post(
            "/v0/checkpoints/import",
            content=b"x",
            headers={"Content-Type": "application/x-eden-checkpoint+tar"},
        )
        assert resp.status_code == 401
        assert resp.json()["type"] == "eden://error/unauthorized"
