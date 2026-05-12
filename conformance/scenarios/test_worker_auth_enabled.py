"""Auth-enabled worker authentication scenarios — chapter 07 §13.

12a-1 wave 5 made the §13 bearer scheme normative. The default
``ReferenceAdapter`` runs the task-store-server with auth disabled
(the chunk-9c / wave-5 posture used by every other conformance
scenario in this suite); this file spawns its own auth-enabled
server and asserts the §13 MUSTs that are observable only when the
middleware is installed:

- §13 — missing bearer → 401 ``eden://error/unauthorized``.
- §13 — malformed bearer → 401 ``eden://error/unauthorized``.
- §13.3 — admin bearer hitting a worker-gated endpoint → 403
  ``eden://error/forbidden``.
- §13.3 — worker bearer hitting an admin-gated endpoint → 403
  ``eden://error/forbidden``.
- §6.4 — ``/whoami`` returns the authenticated worker_id.
- §13.4 — ``reissue_credential`` invalidates the prior credential
  (401 on the stale bearer; 200 on the fresh one).
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

CONFORMANCE_GROUP = "Worker auth"

_FIXTURES_DIR = Path(__file__).resolve().parent.parent / "src" / "conformance" / "fixtures"
_EXPERIMENT_CONFIG = _FIXTURES_DIR / "minimal-experiment.yaml"
_PORT_ANNOUNCE_TIMEOUT = 15.0


def _spawn_auth_server(
    experiment_config: Path, experiment_id: str, admin_token: str
) -> tuple[subprocess.Popen[str], int]:
    """Spawn a task-store-server with --admin-token; return (proc, port)."""
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
    """Drain stdout from a daemon thread; return the announced port."""
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
    """Per-test auth-enabled task-store-server.

    Yields a dict with ``base_url``, ``experiment_id``, ``admin_token``.
    """
    experiment_id = f"auth-{uuid.uuid4().hex[:8]}"
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


def _client(
    server: dict[str, str], bearer: str | None = None
) -> httpx.Client:
    headers = {
        "X-Eden-Experiment-Id": server["experiment_id"],
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json, application/problem+json",
    }
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"
    return httpx.Client(
        base_url=server["base_url"], headers=headers, timeout=15.0
    )


def _admin_register(server: dict[str, str], worker_id: str) -> str:
    """Register ``worker_id`` via admin bearer; return the registration_token."""
    with _client(server, bearer=f"admin:{server['admin_token']}") as c:
        resp = c.post(
            f"/v0/experiments/{server['experiment_id']}/workers",
            json={"worker_id": worker_id},
        )
        resp.raise_for_status()
        body = resp.json()
        token = body.get("registration_token")
        assert isinstance(token, str) and token, body
        return token


def test_missing_bearer_returns_401(auth_server: dict[str, str]) -> None:
    """spec/v0/07-wire-protocol.md §13 — missing bearer returns 401 unauthorized."""
    with _client(auth_server) as c:
        resp = c.get(f"/v0/experiments/{auth_server['experiment_id']}/events")
    assert resp.status_code == 401, resp.text
    assert resp.json().get("type") == "eden://error/unauthorized"


def test_malformed_bearer_returns_401(auth_server: dict[str, str]) -> None:
    """spec/v0/07-wire-protocol.md §13 — malformed bearer returns 401 unauthorized."""
    # No ``:`` separator → malformed per §13.1's <principal>:<secret> grammar.
    with _client(auth_server, bearer="not-a-valid-bearer") as c:
        resp = c.get(f"/v0/experiments/{auth_server['experiment_id']}/events")
    assert resp.status_code == 401, resp.text
    assert resp.json().get("type") == "eden://error/unauthorized"


def test_admin_bearer_on_worker_gated_endpoint_returns_403(
    auth_server: dict[str, str],
) -> None:
    """spec/v0/07-wire-protocol.md §13.3 — admin bearer on worker-gated route → 403."""
    # POST /tasks/{T}/claim is worker-gated per §13.3.
    with _client(auth_server, bearer=f"admin:{auth_server['admin_token']}") as c:
        # We don't even need a real task — auth-class enforcement runs
        # before the route handler reaches the store.
        resp = c.post(
            f"/v0/experiments/{auth_server['experiment_id']}/tasks/some-tid/claim",
            json={},
        )
    assert resp.status_code == 403, resp.text
    assert resp.json().get("type") == "eden://error/forbidden"


def test_worker_bearer_on_admin_gated_endpoint_returns_403(
    auth_server: dict[str, str],
) -> None:
    """spec/v0/07-wire-protocol.md §13.3 — worker bearer on admin-gated route → 403."""
    wid = "test-worker"
    token = _admin_register(auth_server, wid)
    # POST /workers (register_worker) is admin-gated per §13.3.
    with _client(auth_server, bearer=f"{wid}:{token}") as c:
        resp = c.post(
            f"/v0/experiments/{auth_server['experiment_id']}/workers",
            json={"worker_id": "another-worker"},
        )
    assert resp.status_code == 403, resp.text
    assert resp.json().get("type") == "eden://error/forbidden"


def test_whoami_returns_authenticated_worker_id(
    auth_server: dict[str, str],
) -> None:
    """spec/v0/07-wire-protocol.md §6.4 — /whoami returns the bearer's worker_id."""
    wid = "eric"
    token = _admin_register(auth_server, wid)
    with _client(auth_server, bearer=f"{wid}:{token}") as c:
        resp = c.get(f"/v0/experiments/{auth_server['experiment_id']}/whoami")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"worker_id": wid}


def test_reissue_credential_invalidates_prior(
    auth_server: dict[str, str],
) -> None:
    """spec/v0/07-wire-protocol.md §13.4 — reissue invalidates the prior credential.

    Stale bearer → 401; fresh bearer → 200 on the same probe.
    """
    wid = "rotated"
    stale_token = _admin_register(auth_server, wid)

    # Reissue via admin bearer; capture the new token.
    with _client(auth_server, bearer=f"admin:{auth_server['admin_token']}") as c:
        rotate = c.post(
            f"/v0/experiments/{auth_server['experiment_id']}/workers/{wid}/reissue-credential"
        )
        rotate.raise_for_status()
        fresh_token = rotate.json()["registration_token"]
    assert fresh_token != stale_token

    # Stale bearer → 401.
    with _client(auth_server, bearer=f"{wid}:{stale_token}") as c:
        stale = c.get(f"/v0/experiments/{auth_server['experiment_id']}/whoami")
    assert stale.status_code == 401, stale.text
    assert stale.json().get("type") == "eden://error/unauthorized"

    # Fresh bearer → 200.
    with _client(auth_server, bearer=f"{wid}:{fresh_token}") as c:
        fresh = c.get(f"/v0/experiments/{auth_server['experiment_id']}/whoami")
    assert fresh.status_code == 200, fresh.text
    assert fresh.json() == {"worker_id": wid}
