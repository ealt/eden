"""Docker-backed integration tests for ``container_exec``.

Skipped when no docker daemon is reachable so the suite stays green
on dev laptops without docker installed. CI's
``compose-smoke-subprocess-docker`` job runs these in addition to
the unit tests in ``test_container_exec.py``.

Coverage targets the high-risk failure modes flagged by
``/codex-review``:

- mount shape works for git (the round-0 high finding),
- stale-cidfile rejection,
- SIGKILL-escalation actually kills the sibling container,
- ``reap_orphaned_containers`` actually reaps.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path

import pytest
from eden_service_common import (
    BindMount,
    cleanup_cidfile,
    kill_via_cidfile,
    make_cidfile_path,
    reap_orphaned_containers,
    wrap_command,
)

pytestmark = pytest.mark.docker


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            timeout=5,
        )
    except Exception:  # noqa: BLE001
        return False
    return result.returncode == 0


if not _docker_available():  # pragma: no cover — gated on docker
    pytest.skip("docker daemon not reachable", allow_module_level=True)


_TEST_IMAGE = "debian:12-slim"
"""Pinned bash-equipped image. The wrap uses ``bash -lc`` so an
``alpine``-only image (which has ``sh`` but not ``bash``) trips the
``--init`` exec. ``debian:12-slim`` is ~75 MB and ships bash + git,
which both this test suite and the production ``eden-runtime:dev``
image rely on.
"""


@pytest.fixture(scope="module", autouse=True)
def _ensure_image() -> None:
    """Pre-pull the test image so tests don't time out on a cold pull."""
    subprocess.run(
        ["docker", "pull", _TEST_IMAGE],
        check=True,
        capture_output=True,
        timeout=120,
    )


def _run_wrap_sync(command_str: str, *, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command_str,
        shell=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_wrap_runs_a_simple_command(tmp_path: Path) -> None:
    """End-to-end: wrap_command shell string actually executes."""
    cidfile = tmp_path / "cid"
    cmd = wrap_command(
        original_command="echo hello-from-container",
        image=_TEST_IMAGE,
        cwd_target="/work",
        cidfile=cidfile,
        role="test",
        task_id="t1",
        host_id=socket.gethostname(),
        volumes=[],
        binds=[BindMount(source=str(tmp_path), target="/work")],
        env_keys=[],
    )
    result = _run_wrap_sync(cmd)
    assert result.returncode == 0, result.stderr
    assert "hello-from-container" in result.stdout
    cleanup_cidfile(cidfile)


def test_wrap_runs_git_in_a_worktree_with_external_gitlink(tmp_path: Path) -> None:
    """Mount shape works for the production worktree-with-gitlink case.

    Models the implementer's deployment exactly: a bare repo lives at
    one mounted path, a worktree's `.git` is a *gitlink file* pointing
    into the bare repo's `worktrees/<name>/` subdir, and the spawned
    child container must see BOTH paths to make `git status` work.
    Closes the round-0 codex review finding more rigorously than a
    standalone bind-mount of a normal repo.
    """
    bare = tmp_path / "bare"
    wt = tmp_path / "wt"
    subprocess.run(
        ["git", "init", "--bare", "-q", str(bare)],
        check=True,
    )
    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    subprocess.run(["git", "init", "-q", str(seed_dir)], check=True)
    (seed_dir / "f").write_text("contents\n")
    subprocess.run(
        ["git", "-C", str(seed_dir), "add", "."], check=True
    )
    subprocess.run(
        [
            "git", "-C", str(seed_dir),
            "-c", "user.name=t",
            "-c", "user.email=t@t",
            "commit", "-qm", "init",
        ],
        check=True,
    )
    subprocess.run(
        [
            "git", "-C", str(seed_dir),
            "push", "-q", str(bare), "HEAD:refs/heads/main",
        ],
        check=True,
    )
    subprocess.run(
        [
            "git", "-C", str(bare),
            "worktree", "add", "-q", "--detach", str(wt), "main",
        ],
        check=True,
    )
    # Sanity: confirm `.git` in the worktree is a gitlink file
    # pointing into the bare repo, not a directory. This mirrors the
    # production implementer flow and is the case that broke when
    # only the worktree was mounted into the spawned child.
    gitlink = wt / ".git"
    assert gitlink.is_file(), f".git should be a gitlink file, got {gitlink!r}"
    assert "gitdir:" in gitlink.read_text()

    cidfile = tmp_path / "cid"
    # The wrap mounts the worktree at `/wt` and the bare repo at the
    # SAME absolute path the gitlink encodes. Reading the gitlink
    # gives us that path as a side-effect of the test setup.
    bare_path_inside = str(bare)  # .git gitlink encodes the host path
    cmd = wrap_command(
        # debian:12-slim has no git by default; install on the fly.
        # Production uses eden-runtime:dev which bakes git in.
        original_command=(
            "apt-get update -qq >/dev/null 2>&1 && "
            "apt-get install -y -qq --no-install-recommends git ca-certificates "
            ">/dev/null 2>&1 && "
            "git -C /wt status --porcelain && "
            "git -C /wt log --oneline"
        ),
        image=_TEST_IMAGE,
        cwd_target="/wt",
        cidfile=cidfile,
        role="test",
        task_id="git-probe",
        host_id=socket.gethostname(),
        volumes=[],
        binds=[
            BindMount(source=str(wt), target="/wt"),
            BindMount(source=str(bare), target=bare_path_inside),
        ],
        env_keys=[],
    )
    result = _run_wrap_sync(cmd, timeout=180)
    assert result.returncode == 0, result.stderr
    assert "init" in result.stdout
    cleanup_cidfile(cidfile)


def test_stale_cidfile_blocks_reuse(tmp_path: Path) -> None:
    """``docker run --cidfile <path>`` errors if the path already exists."""
    cidfile = tmp_path / "stale.cid"
    cidfile.write_text("not-a-real-cid\n")
    cmd = wrap_command(
        original_command="echo nope",
        image=_TEST_IMAGE,
        cwd_target="/work",
        cidfile=cidfile,
        role="test",
        task_id="stale-probe",
        host_id=socket.gethostname(),
        volumes=[],
        binds=[BindMount(source=str(tmp_path), target="/work")],
        env_keys=[],
    )
    result = _run_wrap_sync(cmd)
    assert result.returncode != 0
    # Now clean up and retry — should succeed.
    cleanup_cidfile(cidfile)
    cmd2 = wrap_command(
        original_command="echo recovered",
        image=_TEST_IMAGE,
        cwd_target="/work",
        cidfile=cidfile,
        role="test",
        task_id="stale-probe-2",
        host_id=socket.gethostname(),
        volumes=[],
        binds=[BindMount(source=str(tmp_path), target="/work")],
        env_keys=[],
    )
    result2 = _run_wrap_sync(cmd2)
    assert result2.returncode == 0, result2.stderr
    assert "recovered" in result2.stdout
    cleanup_cidfile(cidfile)


def test_terminate_sigkill_path_invokes_post_kill_callback(tmp_path: Path) -> None:
    """End-to-end: SIGKILL escalation in ``Subprocess.terminate`` runs the
    cidfile-driven `docker kill && docker rm -f` callback.

    This drives the *actual* `eden_service_common.spawn` →
    `Subprocess.terminate` codepath, not just the helper in
    isolation. The user command traps SIGTERM and sleeps, so the
    in-container PID 1 ignores the SIGTERM that ``--init`` forwards;
    the local ``docker run`` client then waits, the host's SIGTERM
    ladder hits its deadline, and SIGKILL escalation is forced.
    Without the post-kill callback the spawned container would
    survive that — this asserts it doesn't.
    """
    from eden_service_common import (
        make_cidfile_callbacks,
        make_cidfile_path,
        spawn,
    )

    cidfile_dir = tmp_path / "cidfiles"
    cidfile = make_cidfile_path(cidfile_dir=cidfile_dir, role="kill-test")
    post_kill, cleanup = make_cidfile_callbacks(cidfile)
    cmd = wrap_command(
        # `trap '' TERM` makes the container ignore SIGTERM; the host
        # then SIGKILLs the local `docker run` client after the
        # shutdown deadline, leaving the spawned container alive
        # unless `post_kill_callback` cleans it up.
        original_command="trap '' TERM; sleep 600",
        image=_TEST_IMAGE,
        cwd_target="/work",
        cidfile=cidfile,
        role="kill-test",
        task_id="kill-probe",
        host_id=socket.gethostname(),
        volumes=[],
        binds=[BindMount(source=str(tmp_path), target="/work")],
        env_keys=[],
    )
    sub = spawn(
        command=cmd,
        cwd=tmp_path,
        env={},
        role="kill-test",
        task_id="kill-probe",
        capture_stdin=False,
        post_kill_callback=post_kill,
        cleanup_callbacks=[cleanup],
    )
    # Wait for the cidfile to land so we have a real container id
    # to chase.
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not cidfile.is_file():
        time.sleep(0.1)
    assert cidfile.is_file(), "cidfile never appeared"
    cid = cidfile.read_text().strip()
    assert cid

    # Force SIGKILL escalation by setting an immediate shutdown
    # deadline. SIGTERM will fire, the trap-empty container ignores
    # it, terminate() escalates to SIGKILL, then runs the
    # post_kill_callback.
    sub.terminate(shutdown_deadline=0.5)

    # The spawned sibling container must be gone (not just the local
    # docker-run client). Wait briefly for docker to finalize the
    # remove.
    deadline = time.monotonic() + 10
    last_stdout = "<not yet polled>"
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["docker", "ps", "-aq", "--filter", f"id={cid}"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        last_stdout = result.stdout.strip()
        if not last_stdout:
            break
        time.sleep(0.5)
    assert not last_stdout, (
        f"spawned container {cid} survived SIGKILL escalation; "
        f"post_kill_callback did not run or failed"
    )
    # And the cidfile cleanup ran on the SIGKILL escalation branch.
    assert not cidfile.is_file(), (
        "cleanup callback did not unlink cidfile after SIGKILL escalation"
    )


def test_post_kill_callback_kills_orphaned_container(tmp_path: Path) -> None:
    """Detached sleep container is reachable + killable via cidfile."""
    cidfile = tmp_path / "longrun.cid"
    # Start a long-running detached container outside our wrap helper
    # (the wrap helper streams stdio, so we can't easily background
    # it). The cidfile semantics are identical.
    cid_label = f"eden.test={uuid.uuid4().hex}"
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-d",
            "--cidfile",
            str(cidfile),
            "--label",
            cid_label,
            _TEST_IMAGE,
            "sleep",
            "300",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    cid = result.stdout.strip()
    assert cid, "docker run -d should have printed a container id"
    try:
        # Confirm it's running.
        ps = subprocess.run(
            ["docker", "ps", "-q", "--filter", f"id={cid}"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        assert ps.stdout.strip(), "container not visible in `docker ps`"

        # Now kill via cidfile.
        kill_via_cidfile(cidfile)

        # Wait for it to actually be gone.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            ps = subprocess.run(
                ["docker", "ps", "-aq", "--filter", f"id={cid}"],
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            )
            if not ps.stdout.strip():
                break
            time.sleep(0.5)
        assert not ps.stdout.strip(), f"container {cid} still present after kill"
    finally:
        # Best-effort fallback if the assertion failed.
        subprocess.run(
            ["docker", "rm", "-f", cid],
            capture_output=True,
            timeout=10,
        )
        cleanup_cidfile(cidfile)


def test_reap_orphaned_containers(tmp_path: Path) -> None:
    """A pre-spawned labeled container is removed by ``reap_orphaned_containers``."""
    role = "test-reap"
    host = f"test-host-{uuid.uuid4().hex[:8]}"
    # Pre-spawn a container with the labels reap_orphaned_containers
    # filters by.
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-d",
            "--label",
            f"eden.role={role}",
            "--label",
            f"eden.host={host}",
            _TEST_IMAGE,
            "sleep",
            "300",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    cid = result.stdout.strip()
    try:
        # Verify it's running.
        ps = subprocess.run(
            ["docker", "ps", "-q", "--filter", f"id={cid}"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        assert ps.stdout.strip()

        reap_orphaned_containers(role=role, host=host)

        # Wait for it to be gone.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            ps = subprocess.run(
                ["docker", "ps", "-aq", "--filter", f"id={cid}"],
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            )
            if not ps.stdout.strip():
                break
            time.sleep(0.5)
        assert not ps.stdout.strip(), f"reap should have removed container {cid}"
    finally:
        subprocess.run(
            ["docker", "rm", "-f", cid], capture_output=True, timeout=10
        )


def test_make_cidfile_path_creates_parent_dir(tmp_path: Path) -> None:
    """Path generation creates the cidfile dir if it doesn't exist."""
    deep = tmp_path / "a" / "b" / "c"
    assert not deep.exists()
    cidfile = make_cidfile_path(cidfile_dir=deep, role="probe")
    assert cidfile.parent == deep
    assert deep.is_dir()
    # The cidfile itself is not created — only the dir.
    assert not cidfile.exists()
