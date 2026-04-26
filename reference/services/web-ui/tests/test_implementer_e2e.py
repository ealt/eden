"""Real-subprocess end-to-end test for the implementer module.

Forks task-store-server + web-ui (with ``--repo-path`` set), seeds
a ready proposal + pending implement task via ``StoreClient``,
writes a child commit into the bare repo, and drives the full
claim → draft → submit flow over real HTTP. Asserts the resulting
task / trial / ref state via a separate ``StoreClient`` and
``GitRepo`` against the same bare repo.
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
from eden_git import GitRepo, Identity, TreeEntry
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

_TASK_STORE_RE = re.compile(r"^EDEN_TASK_STORE_LISTENING\s+(.*)$")
_WEB_UI_RE = re.compile(r"^EDEN_WEB_UI_LISTENING\s+(.*)$")

_E2E_IDENTITY = Identity(name="EDEN E2E", email="e2e@eden.invalid")
_E2E_DATE = "2026-04-24T12:00:00+00:00"


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


def _seed_bare_repo(repo_dir: Path) -> tuple[GitRepo, str]:
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(repo_dir)],
        check=True,
        capture_output=True,
    )
    repo = GitRepo(str(repo_dir))
    blob = repo.write_blob(b"seed\n")
    tree = repo.write_tree_from_entries(
        [TreeEntry(mode="100644", type="blob", sha=blob, path="seed.txt")]
    )
    base_sha = repo.commit_tree(
        tree,
        parents=[],
        message="seed\n",
        author=_E2E_IDENTITY,
        committer=_E2E_IDENTITY,
        author_date=_E2E_DATE,
        committer_date=_E2E_DATE,
    )
    repo.create_ref("refs/heads/main", base_sha)
    return repo, base_sha


@pytest.mark.e2e
def test_implementer_full_flow_through_ui(tmp_path: Path) -> None:
    """Real-process implementer flow: claim → draft → submit, verify state."""
    db_path = tmp_path / "eden.sqlite"
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    repo_dir = tmp_path / "bare-repo.git"
    experiment_id = "exp-impl-e2e"
    token = "impl-e2e-token"

    repo, base_sha = _seed_bare_repo(repo_dir)
    # Push a child commit the operator will reference in the form.
    blob = repo.write_blob(b"e2e impl payload\n")
    tree = repo.write_tree_from_entries(
        [TreeEntry(mode="100644", type="blob", sha=blob, path="payload.txt")]
    )
    child_sha = repo.commit_tree(
        tree,
        parents=[base_sha],
        message="impl tip\n",
        author=_E2E_IDENTITY,
        committer=_E2E_IDENTITY,
        author_date=_E2E_DATE,
        committer_date=_E2E_DATE,
    )

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
            "x" * 32,
            "--worker-id",
            "ui-impl",
            "--artifacts-dir",
            str(artifacts_dir),
            "--repo-path",
            str(repo_dir),
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
        # Seed proposal + implement task via wire client.
        from eden_wire import StoreClient

        seed = StoreClient(
            base_url=task_store_url,
            experiment_id=experiment_id,
            token=token,
        )
        try:
            artifact_path = artifacts_dir / "p-impl.md"
            artifact_path.write_text("rationale")
            proposal = Proposal(
                proposal_id="p-impl",
                experiment_id=experiment_id,
                slug="impl-e2e",
                priority=1.0,
                parent_commits=[base_sha],
                artifacts_uri=f"file://{artifact_path.resolve()}",
                state="drafting",
                created_at="2026-04-24T11:00:00Z",
            )
            seed.create_proposal(proposal)
            seed.mark_proposal_ready("p-impl")
            seed.create_implement_task("t-impl-1", "p-impl")
        finally:
            seed.close()

        with httpx.Client(base_url=web_url, timeout=10.0) as ui:
            resp = ui.post("/signin", follow_redirects=False)
            assert resp.status_code == 303

            resp = ui.get("/implementer/")
            assert resp.status_code == 200
            assert "t-impl-1" in resp.text
            m = re.search(r'name="csrf_token"\s+value="([^"]+)"', resp.text)
            assert m is not None, "csrf token not found in implementer list"
            csrf = m.group(1)

            resp = ui.post(
                "/implementer/t-impl-1/claim",
                content=urlencode({"csrf_token": csrf}),
                headers={"content-type": "application/x-www-form-urlencoded"},
                follow_redirects=False,
            )
            assert resp.status_code == 303

            resp = ui.get("/implementer/t-impl-1/draft")
            assert resp.status_code == 200, resp.text
            assert "rationale" in resp.text

            body = urlencode(
                [
                    ("csrf_token", csrf),
                    ("status", "success"),
                    ("commit_sha", child_sha),
                    ("description", "e2e impl"),
                ]
            )
            resp = ui.post(
                "/implementer/t-impl-1/submit",
                content=body,
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
            if resp.status_code != 200:
                pytest.fail(
                    f"submit returned {resp.status_code}: {resp.text}\n"
                    + _dump_stderr(procs)
                )
            assert child_sha in resp.text

        for p in procs.values():
            _terminate(p)

        store = SqliteStore(
            experiment_id=experiment_id,
            path=str(db_path),
            metrics_schema=MetricsSchema({"score": "real"}),
        )
        try:
            assert store.read_task("t-impl-1").state == "submitted"
            trials = store.list_trials()
            assert len(trials) == 1
            trial = trials[0]
            assert trial.status == "starting"
            assert trial.commit_sha is None
            assert trial.branch is not None
            assert trial.branch.startswith("work/impl-e2e-")
            assert trial.parent_commits == [base_sha]
            # work/* ref committed by Phase 2 of the UI flow.
            assert (
                repo.resolve_ref(f"refs/heads/{trial.branch}") == child_sha
            )
        finally:
            store.close()
    finally:
        for p in procs.values():
            _terminate(p)
