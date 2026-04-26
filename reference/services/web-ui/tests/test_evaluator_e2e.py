"""Real-subprocess end-to-end test for the evaluator module.

Forks task-store-server + web-ui (no ``--repo-path`` needed; the
evaluator never touches a repo through the UI), seeds a ready
proposal + implement task and drives it through to ``starting``
with a ``commit_sha`` set so an evaluate task can be created. Then
drives the full claim → draft → submit flow over real HTTP.
Asserts the resulting task / submission state via a separate
``StoreClient`` against the same SQLite database.
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
from eden_contracts import MetricsSchema, Proposal
from eden_storage import EvaluateSubmission, ImplementSubmission, SqliteStore

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


def _read_port(proc: subprocess.Popen, pattern: re.Pattern[str], timeout: float = 10.0) -> int:
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
def test_evaluator_full_flow_through_ui(tmp_path: Path) -> None:
    """Real-process evaluator flow: claim → draft → submit, verify state."""
    db_path = tmp_path / "eden.sqlite"
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    experiment_id = "exp-eval-e2e"
    token = "eval-e2e-token"

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
            "y" * 32,
            "--worker-id",
            "ui-eval",
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
        # Seed: ready proposal + implement task; drive it to a
        # starting trial with commit_sha so the evaluate task can be
        # created.
        from eden_wire import StoreClient

        seed = StoreClient(
            base_url=task_store_url,
            experiment_id=experiment_id,
            token=token,
        )
        try:
            artifact_path = artifacts_dir / "p-eval.md"
            artifact_path.write_text("rationale")
            proposal = Proposal(
                proposal_id="p-eval",
                experiment_id=experiment_id,
                slug="eval-e2e",
                priority=1.0,
                parent_commits=["a" * 40],
                artifacts_uri=f"file://{artifact_path.resolve()}",
                state="drafting",
                created_at="2026-04-24T11:00:00Z",
            )
            seed.create_proposal(proposal)
            seed.mark_proposal_ready("p-eval")
            seed.create_implement_task("t-impl-1", "p-eval")
            # Drive to starting trial with commit_sha.
            from eden_contracts import Trial

            trial = Trial(
                trial_id="tr-1",
                experiment_id=experiment_id,
                proposal_id="p-eval",
                status="starting",
                parent_commits=["a" * 40],
                branch="work/eval-e2e-tr-1",
                started_at="2026-04-24T12:00:00Z",
            )
            seed.create_trial(trial)
            claim = seed.claim("t-impl-1", "impl-w")
            seed.submit(
                "t-impl-1",
                claim.token,
                ImplementSubmission(
                    status="success", trial_id="tr-1", commit_sha="b" * 40
                ),
            )
            seed.accept("t-impl-1")
            seed.create_evaluate_task("t-eval-1", "tr-1")
        finally:
            seed.close()

        with httpx.Client(base_url=web_url, timeout=10.0) as ui:
            resp = ui.post("/signin", follow_redirects=False)
            assert resp.status_code == 303

            resp = ui.get("/evaluator/")
            assert resp.status_code == 200, resp.text
            assert "t-eval-1" in resp.text
            m = re.search(r'name="csrf_token"\s+value="([^"]+)"', resp.text)
            assert m is not None, "csrf token not found in evaluator list"
            csrf = m.group(1)

            resp = ui.post(
                "/evaluator/t-eval-1/claim",
                content=urlencode({"csrf_token": csrf}),
                headers={"content-type": "application/x-www-form-urlencoded"},
                follow_redirects=False,
            )
            assert resp.status_code == 303

            resp = ui.get("/evaluator/t-eval-1/draft")
            assert resp.status_code == 200, resp.text
            assert "tr-1" in resp.text

            body = urlencode(
                [
                    ("csrf_token", csrf),
                    ("status", "success"),
                    ("metric.score", "0.875"),
                    ("artifacts_uri", "https://logs.example/run-1"),
                ]
            )
            resp = ui.post(
                "/evaluator/t-eval-1/submit",
                content=body,
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
            if resp.status_code != 200:
                pytest.fail(
                    f"submit returned {resp.status_code}: {resp.text}\n"
                    + _dump_stderr(procs)
                )
            assert "0.875" in resp.text

        for p in procs.values():
            _terminate(p)

        store = SqliteStore(
            experiment_id=experiment_id,
            path=str(db_path),
            metrics_schema=MetricsSchema({"score": "real"}),
        )
        try:
            task = store.read_task("t-eval-1")
            assert task.state == "submitted"
            recorded = store.read_submission("t-eval-1")
            assert isinstance(recorded, EvaluateSubmission)
            assert recorded.status == "success"
            assert recorded.trial_id == "tr-1"
            assert recorded.metrics == {"score": 0.875}
            assert recorded.artifacts_uri == "https://logs.example/run-1"
        finally:
            store.close()
    finally:
        for p in procs.values():
            _terminate(p)
