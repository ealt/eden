"""Unit tests for the new ``GitRepo`` remote ops (Phase 10d follow-up B).

Driven against a local ``file://`` URL "remote" — sufficient because
git's transport abstraction makes file:// share the same ref-update
code path as http://. HTTP transport is exercised end-to-end by the
compose smokes against a real Gitea container.

The credential-helper roundtrip test spins up a tiny in-process
``http.server`` so the auth flow goes through real Basic-auth
challenge/response. That's the one path file:// can't model (file://
has no auth layer), and where argv-only inspection would silently
skip a wiring bug.
"""

from __future__ import annotations

import base64
import http.server
import socket
import threading
from pathlib import Path

import pytest
from eden_git import (
    GitError,
    GitRepo,
    GitTransportError,
    Identity,
    RefRefused,
    TreeEntry,
)

TEST_AUTHOR = Identity(name="EDEN Test", email="test@eden.example")
FIXED_DATE = "2026-04-23T00:00:00+00:00"


def _seeded_bare(tmp_path: Path, name: str = "remote.git") -> tuple[GitRepo, str]:
    """A bare repo with a single seed commit on `refs/heads/main`."""
    repo = GitRepo.init_bare(tmp_path / name)
    blob = repo.write_blob(b"seed\n")
    tree = repo.write_tree_from_entries(
        [TreeEntry(mode="100644", type="blob", sha=blob, path="README")]
    )
    seed = repo.commit_tree(
        tree,
        parents=[],
        message="seed",
        author=TEST_AUTHOR,
        author_date=FIXED_DATE,
        committer_date=FIXED_DATE,
    )
    repo.create_ref("refs/heads/main", seed)
    return repo, seed


# ---------------------------------------------------------------- clone


def test_clone_from_bare_succeeds(tmp_path: Path) -> None:
    remote, seed = _seeded_bare(tmp_path)
    cloned = GitRepo.clone_from(
        url=f"file://{remote.path}",
        dest=tmp_path / "clone.git",
        bare=True,
    )
    assert cloned.is_bare()
    assert cloned.resolve_ref("refs/heads/main") == seed


def test_clone_from_records_credential_helper(tmp_path: Path) -> None:
    remote, _ = _seeded_bare(tmp_path)
    helper = "/etc/eden/credential-helper.sh"
    cloned = GitRepo.clone_from(
        url=f"file://{remote.path}",
        dest=tmp_path / "clone.git",
        bare=True,
        credential_helper=helper,
    )
    # Repo-local config carries the helper for subsequent ops.
    result = cloned._run(["config", "--get", "credential.helper"])
    assert result.stdout.strip() == helper


def test_clone_from_unreachable_raises_transport_error(tmp_path: Path) -> None:
    # 127.0.0.1:1 is reserved + always refused.
    with pytest.raises(GitTransportError):
        GitRepo.clone_from(
            url="http://127.0.0.1:1/missing.git",
            dest=tmp_path / "clone.git",
            bare=True,
        )


# ---------------------------------------------------------------- push


def test_push_ref_succeeds(tmp_path: Path) -> None:
    remote, seed = _seeded_bare(tmp_path)
    cloned = GitRepo.clone_from(
        url=f"file://{remote.path}",
        dest=tmp_path / "clone.git",
        bare=True,
    )
    blob = cloned.write_blob(b"work\n")
    tree = cloned.write_tree_from_entries(
        [TreeEntry(mode="100644", type="blob", sha=blob, path="WORK")]
    )
    work = cloned.commit_tree(
        tree,
        parents=[seed],
        message="work",
        author=TEST_AUTHOR,
        author_date=FIXED_DATE,
        committer_date=FIXED_DATE,
    )
    cloned.create_ref("refs/heads/work/p0-trial-x", work)
    cloned.push_ref("refs/heads/work/p0-trial-x")
    assert remote.resolve_ref("refs/heads/work/p0-trial-x") == work


def test_push_ref_with_cas_rejects_on_divergence(tmp_path: Path) -> None:
    remote, seed = _seeded_bare(tmp_path)
    cloned = GitRepo.clone_from(
        url=f"file://{remote.path}",
        dest=tmp_path / "clone.git",
        bare=True,
    )
    # Diverge: someone else pushed a different commit to the remote
    # between our local create and our push attempt.
    blob_a = remote.write_blob(b"intruder\n")
    tree_a = remote.write_tree_from_entries(
        [TreeEntry(mode="100644", type="blob", sha=blob_a, path="THEIRS")]
    )
    intruder = remote.commit_tree(
        tree_a,
        parents=[seed],
        message="intruder",
        author=TEST_AUTHOR,
        author_date=FIXED_DATE,
        committer_date=FIXED_DATE,
    )
    remote.create_ref("refs/heads/work/p0-trial-x", intruder)

    # Our push, with CAS expecting the ref to be absent, must fail.
    blob = cloned.write_blob(b"ours\n")
    tree = cloned.write_tree_from_entries(
        [TreeEntry(mode="100644", type="blob", sha=blob, path="OURS")]
    )
    ours = cloned.commit_tree(
        tree,
        parents=[seed],
        message="ours",
        author=TEST_AUTHOR,
        author_date=FIXED_DATE,
        committer_date=FIXED_DATE,
    )
    cloned.create_ref("refs/heads/work/p0-trial-x", ours)
    with pytest.raises(RefRefused):
        cloned.push_ref(
            "refs/heads/work/p0-trial-x",
            expected_old_sha=cloned.zero_oid(),
        )


# ---------------------------------------------------------------- fetch


def test_fetch_ref_pulls_remote_state(tmp_path: Path) -> None:
    remote, seed = _seeded_bare(tmp_path)
    cloned = GitRepo.clone_from(
        url=f"file://{remote.path}",
        dest=tmp_path / "clone.git",
        bare=True,
    )
    # Remote evolves; local fetches.
    blob = remote.write_blob(b"new\n")
    tree = remote.write_tree_from_entries(
        [TreeEntry(mode="100644", type="blob", sha=blob, path="NEW")]
    )
    new_commit = remote.commit_tree(
        tree,
        parents=[seed],
        message="new",
        author=TEST_AUTHOR,
        author_date=FIXED_DATE,
        committer_date=FIXED_DATE,
    )
    remote.create_ref("refs/heads/work/x-trial-y", new_commit)
    fetched = cloned.fetch_ref("refs/heads/work/x-trial-y")
    assert fetched == new_commit
    assert cloned.resolve_ref("refs/heads/work/x-trial-y") == new_commit


def test_fetch_ref_returns_none_for_missing_ref(tmp_path: Path) -> None:
    remote, _ = _seeded_bare(tmp_path)
    cloned = GitRepo.clone_from(
        url=f"file://{remote.path}",
        dest=tmp_path / "clone.git",
        bare=True,
    )
    assert cloned.fetch_ref("refs/heads/nope") is None


def test_fetch_all_heads_prunes_local_orphans(tmp_path: Path) -> None:
    remote, seed = _seeded_bare(tmp_path)
    cloned = GitRepo.clone_from(
        url=f"file://{remote.path}",
        dest=tmp_path / "clone.git",
        bare=True,
    )
    # Fabricate a local-only branch that doesn't exist on the remote.
    cloned.create_ref("refs/heads/work/orphan", seed)
    assert cloned.resolve_ref("refs/heads/work/orphan") == seed
    cloned.fetch_all_heads()
    assert cloned.resolve_ref("refs/heads/work/orphan") is None


# ---------------------------------------------------------------- delete-remote


def test_delete_remote_ref_succeeds(tmp_path: Path) -> None:
    remote, seed = _seeded_bare(tmp_path)
    remote.create_ref("refs/heads/work/p0-trial-x", seed)
    cloned = GitRepo.clone_from(
        url=f"file://{remote.path}",
        dest=tmp_path / "clone.git",
        bare=True,
    )
    cloned.delete_remote_ref(
        "refs/heads/work/p0-trial-x", expected_sha=seed
    )
    assert remote.resolve_ref("refs/heads/work/p0-trial-x") is None


def test_delete_remote_ref_cas_rejects_on_divergence(tmp_path: Path) -> None:
    remote, seed = _seeded_bare(tmp_path)
    remote.create_ref("refs/heads/work/p0-trial-x", seed)
    cloned = GitRepo.clone_from(
        url=f"file://{remote.path}",
        dest=tmp_path / "clone.git",
        bare=True,
    )
    # The remote then advances under the integrator's feet.
    blob = remote.write_blob(b"after\n")
    tree = remote.write_tree_from_entries(
        [TreeEntry(mode="100644", type="blob", sha=blob, path="AFTER")]
    )
    later = remote.commit_tree(
        tree,
        parents=[seed],
        message="after",
        author=TEST_AUTHOR,
        author_date=FIXED_DATE,
        committer_date=FIXED_DATE,
    )
    remote.update_ref("refs/heads/work/p0-trial-x", later)
    # Local thinks the ref is at `seed` and tries to compensating-delete.
    # CAS guards against blowing away the new value.
    with pytest.raises((RefRefused, GitError)):
        cloned.delete_remote_ref(
            "refs/heads/work/p0-trial-x", expected_sha=seed
        )


# ---------------------------------------------------------------- ls-remote


def test_ls_remote_lists_all_heads(tmp_path: Path) -> None:
    remote, seed = _seeded_bare(tmp_path)
    remote.create_ref("refs/heads/trial/abc-p0", seed)
    remote.create_ref("refs/heads/work/p0-trial-y", seed)
    cloned = GitRepo.clone_from(
        url=f"file://{remote.path}",
        dest=tmp_path / "clone.git",
        bare=True,
    )
    refs = cloned.ls_remote("refs/heads/*")
    names = {name for name, _ in refs}
    assert "refs/heads/main" in names
    assert "refs/heads/trial/abc-p0" in names
    assert "refs/heads/work/p0-trial-y" in names


def test_ls_remote_pattern_filters(tmp_path: Path) -> None:
    remote, seed = _seeded_bare(tmp_path)
    remote.create_ref("refs/heads/trial/abc-p0", seed)
    remote.create_ref("refs/heads/work/p0-trial-y", seed)
    cloned = GitRepo.clone_from(
        url=f"file://{remote.path}",
        dest=tmp_path / "clone.git",
        bare=True,
    )
    only_trials = cloned.ls_remote("refs/heads/trial/*")
    names = [name for name, _ in only_trials]
    assert names == ["refs/heads/trial/abc-p0"]


# ---------------------------------------------------------------- credential helper


class _BasicAuthHandler(http.server.BaseHTTPRequestHandler):
    """Minimal HTTP server that demands Basic auth for any request."""

    expected: tuple[str, str] = ("eden", "secret")

    def _check_auth(self) -> bool:
        header = self.headers.get("Authorization")
        if header is None or not header.startswith("Basic "):
            return False
        decoded = base64.b64decode(header[len("Basic ") :]).decode()
        user, _, pw = decoded.partition(":")
        return (user, pw) == self.expected

    def do_GET(self) -> None:  # noqa: N802 — http.server convention
        if not self._check_auth():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="git"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        # Anything past the auth check returns 404 — we're proving the
        # credential helper feeds the auth header, NOT serving git
        # smart http (that would be huge to mock; the file:// + basic-
        # auth combination would fall over). The test asserts the
        # 401-vs-404 distinction below.
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        # Quiet the test output.
        pass


@pytest.fixture
def http_basic_server(tmp_path: Path):
    """In-process HTTP server demanding Basic auth for every request."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = http.server.HTTPServer(("127.0.0.1", port), _BasicAuthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/probe.git"
    finally:
        server.shutdown()
        server.server_close()


def test_credential_helper_provides_basic_auth(
    tmp_path: Path, http_basic_server: str
) -> None:
    """A clone WITHOUT the helper hits 401; WITH the helper, it auths past
    401 (then fails 404 because we didn't mock smart-http)."""
    helper_path = tmp_path / "helper.sh"
    helper_path.write_text(
        '#!/bin/sh\n'
        'case "$1" in get) echo username=eden; echo password=secret;; esac\n',
        encoding="utf-8",
    )
    helper_path.chmod(0o755)

    # Without the helper: git has no credentials AND
    # GIT_TERMINAL_PROMPT=0 prevents the prompt fallback, so it
    # fails reading the username before it can attempt auth.
    with pytest.raises(GitError) as no_auth:
        GitRepo.clone_from(
            url=http_basic_server,
            dest=tmp_path / "noauth.git",
            bare=True,
        )
    no_auth_stderr = no_auth.value.stderr
    assert (
        "could not read Username" in no_auth_stderr
        or "Authentication" in no_auth_stderr
        or "401" in no_auth_stderr
    ), (
        "expected no-auth failure marker; got stderr: "
        f"{no_auth_stderr!r}"
    )

    # With the helper: credentials are supplied; auth passes; the
    # server returns 404 because we didn't mock smart-http. The
    # "could not read Username" marker MUST be absent — that proves
    # the helper actually fed git a username.
    with pytest.raises(GitError) as auth_ok:
        GitRepo.clone_from(
            url=http_basic_server,
            dest=tmp_path / "auth.git",
            bare=True,
            credential_helper=str(helper_path),
        )
    auth_stderr = auth_ok.value.stderr
    assert "could not read Username" not in auth_stderr
    assert "401" not in auth_stderr
