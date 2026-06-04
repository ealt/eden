"""Tests for the 12a-1f reference-only artifact-serving route.

Covers spec/v0/reference-bindings/worker-host-subprocess.md §9 and
docs/plans/eden-phase-12a-1f-substrate-access.md §6.1 / §6.2 /
§6.7.

The route lives at
``/_reference/experiments/{experiment_id}/artifacts/{path:path}``
and is mounted unconditionally; without ``--artifacts-dir`` (i.e.
``make_app(artifacts_dir=None)``) it returns 503 with a
closed-vocabulary reference-error type. With a configured
artifacts root, it does a descriptor-relative component walk
under that root, enforces a 1 MiB cap, and serves the bytes with
safe-delivery headers.

Trust-boundary tests (auth-first, traversal, symlinks, TOCTOU)
are the load-bearing surface; see §6.1 of the plan.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest
from eden_contracts import EvaluationSchema
from eden_storage import InMemoryStore
from eden_wire import make_app
from eden_wire.errors import (
    ArtifactServingDisabled,
    ArtifactTooLarge,
    InvalidPath,
)
from fastapi.testclient import TestClient

EXPERIMENT_ID = "exp_6x1cwsbncertng98jdcg5qwmg1"
ADMIN_TOKEN = "test-admin-token-1f"


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore(
        experiment_id=EXPERIMENT_ID,
        evaluation_schema=EvaluationSchema({"loss": "real"}),
    )


@pytest.fixture
def artifacts(tmp_path: Path) -> Path:
    """Pre-populated artifacts directory with a few fixtures."""
    root = tmp_path / "artifacts"
    root.mkdir()
    (root / "hello.md").write_bytes(b"hello world\n")
    sub = root / "ideas" / "idea-abc"
    sub.mkdir(parents=True)
    (sub / "content.md").write_bytes(b"## Idea\n\nDetails.\n")
    return root


def _route_url(path: str = "") -> str:
    return f"/_reference/experiments/{EXPERIMENT_ID}/artifacts/{path}"


def _register_worker(client: TestClient, name: str = "alice") -> tuple[str, str]:
    """Register a worker by name; return its minted ``wkr_*`` id + token."""
    resp = client.post(
        f"/v0/experiments/{EXPERIMENT_ID}/workers",
        headers={
            "X-Eden-Experiment-Id": EXPERIMENT_ID,
            "Authorization": f"Bearer admin:{ADMIN_TOKEN}",
        },
        json={"name": name},
    )
    assert resp.status_code == 200
    body = resp.json()
    return body["worker_id"], body["registration_token"]


# ----------------------------------------------------------------------
# Auth-first (§6.1 #1) — sentinel real FS entry points
# ----------------------------------------------------------------------


def test_unauth_request_never_touches_filesystem(
    store: InMemoryStore, artifacts: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unauth request → 401 BEFORE any os.open / os.fstat / walk call.

    Patches the actual FS entry points the handler uses (the
    module-level ``_open_artifact_fd`` helper plus os.open / os.fstat
    in the eden_wire.server module). Asserts none are called on an
    unauthenticated request — the post-round-2 component-walk
    implementation no longer calls ``Path.resolve``, so a
    ``Path.resolve``-only sentinel would silently pass even if the
    handler regressed to a pre-auth ``os.open``.
    """
    from eden_wire import _artifact_fd as artifact_fd

    calls: list[str] = []

    def _fail_open(*a, **kw):
        calls.append("_open_artifact_fd")
        raise AssertionError("filesystem touched before auth!")

    def _fail_fstat(*a, **kw):
        calls.append("os.fstat")
        raise AssertionError("os.fstat touched before auth!")

    monkeypatch.setattr(artifact_fd, "_open_artifact_fd", _fail_open)
    monkeypatch.setattr(artifact_fd.os, "fstat", _fail_fstat)

    app = make_app(store, admin_token=ADMIN_TOKEN, artifacts_dir=artifacts)
    client = TestClient(app)
    resp = client.get(_route_url("hello.md"))
    assert resp.status_code == 401
    body = resp.json()
    assert body["type"] == "eden://error/unauthorized"
    assert calls == [], f"filesystem touched on unauth path: {calls!r}"


# ----------------------------------------------------------------------
# Bearer-auth (§6.1 #2-3) — admin OR worker accepted; bad bearer 401
# ----------------------------------------------------------------------


def test_admin_bearer_succeeds(store: InMemoryStore, artifacts: Path) -> None:
    app = make_app(store, admin_token=ADMIN_TOKEN, artifacts_dir=artifacts)
    client = TestClient(app)
    resp = client.get(
        _route_url("hello.md"),
        headers={"Authorization": f"Bearer admin:{ADMIN_TOKEN}"},
    )
    assert resp.status_code == 200
    assert resp.content == b"hello world\n"


def test_worker_bearer_succeeds(store: InMemoryStore, artifacts: Path) -> None:
    app = make_app(store, admin_token=ADMIN_TOKEN, artifacts_dir=artifacts)
    client = TestClient(app)
    worker_id, token = _register_worker(client, "alice")
    resp = client.get(
        _route_url("hello.md"),
        headers={"Authorization": f"Bearer {worker_id}:{token}"},
    )
    assert resp.status_code == 200
    assert resp.content == b"hello world\n"


@pytest.mark.parametrize(
    "header",
    [
        "Basic abc",  # wrong scheme
        "Bearer no-colon",  # missing :
        "Bearer admin:wrong-token",  # bad secret
        "Bearer ghost:nonexistent",  # unknown worker
        "Bearer :empty-principal",  # empty principal
        "Bearer admin:",  # empty secret
    ],
)
def test_bad_bearer_401(
    store: InMemoryStore, artifacts: Path, header: str
) -> None:
    app = make_app(store, admin_token=ADMIN_TOKEN, artifacts_dir=artifacts)
    client = TestClient(app)
    resp = client.get(_route_url("hello.md"), headers={"Authorization": header})
    assert resp.status_code == 401


# ----------------------------------------------------------------------
# Traversal taxonomy (§6.1 #4) — malformed → 400, symlink → 403
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw_path",
    [
        # `..` URL-encoded so httpx doesn't normalize the URL before
        # send. The handler sees `path="../etc/passwd"` after URL
        # decoding and the _REJECT_PATH_COMPONENTS guard rejects.
        "%2E%2E/etc/passwd",
        "foo/",  # empty trailing
        "foo//bar",  # empty middle component
        "foo%00bar",  # NUL byte
    ],
)
def test_malformed_path_400_invalid_path(
    store: InMemoryStore, artifacts: Path, raw_path: str
) -> None:
    app = make_app(store, admin_token=ADMIN_TOKEN, artifacts_dir=artifacts)
    client = TestClient(app)
    resp = client.get(
        _route_url(raw_path),
        headers={"Authorization": f"Bearer admin:{ADMIN_TOKEN}"},
    )
    assert resp.status_code == 400, (
        f"got {resp.status_code}: {resp.json()}"
    )
    assert resp.json()["type"] == "eden://reference-error/invalid-path"


def test_symlink_out_of_root_403(
    store: InMemoryStore, artifacts: Path
) -> None:
    """A symlink inside the root pointing outside → 403 (ELOOP)."""
    bad = artifacts / "escape"
    bad.symlink_to(Path("/etc/passwd"))
    app = make_app(store, admin_token=ADMIN_TOKEN, artifacts_dir=artifacts)
    client = TestClient(app)
    resp = client.get(
        _route_url("escape"),
        headers={"Authorization": f"Bearer admin:{ADMIN_TOKEN}"},
    )
    assert resp.status_code == 403
    # MUST NOT include any bytes from /etc/passwd.
    assert b"root:" not in resp.content


# ----------------------------------------------------------------------
# Missing + non-file (§6.1 #5-6)
# ----------------------------------------------------------------------


def test_missing_404(store: InMemoryStore, artifacts: Path) -> None:
    app = make_app(store, admin_token=ADMIN_TOKEN, artifacts_dir=artifacts)
    client = TestClient(app)
    resp = client.get(
        _route_url("does-not-exist.md"),
        headers={"Authorization": f"Bearer admin:{ADMIN_TOKEN}"},
    )
    assert resp.status_code == 404
    # Codex round-0: 404 MUST emit the wire problem+json envelope,
    # not FastAPI's default {"detail": ...} shape.
    assert (
        resp.headers["content-type"]
        == "application/problem+json"
    )
    assert resp.json()["type"] == "eden://error/not-found"


def test_regular_file_as_intermediate_404_not_403(
    store: InMemoryStore, tmp_path: Path
) -> None:
    """Codex round-0: an intermediate-component regular file MUST
    map to 404 (not 403). The handler distinguishes ENOTDIR-from-
    symlink (→ 403 via lstat) from ENOTDIR-from-regular-file (→
    404). The bug would mis-classify the latter as a symlink hit.
    """
    root = tmp_path / "art"
    root.mkdir()
    # Make a plain file (not a directory) and try to walk THROUGH it.
    (root / "afile").write_bytes(b"not a directory\n")
    app = make_app(store, admin_token=ADMIN_TOKEN, artifacts_dir=root)
    client = TestClient(app)
    resp = client.get(
        _route_url("afile/inside.md"),
        headers={"Authorization": f"Bearer admin:{ADMIN_TOKEN}"},
    )
    assert resp.status_code == 404, (
        f"got {resp.status_code} {resp.json()!r}; "
        "regular-file intermediates must be 404, not 403"
    )
    assert resp.json()["type"] == "eden://error/not-found"


def test_operational_oserror_propagates_to_5xx(
    store: InMemoryStore, artifacts: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex round-3: an operational OSError (EIO, EMFILE, ENOSPC,
    etc.) MUST propagate as a 5xx server fault — NOT silently
    masquerade as a 404 ``eden://error/not-found`` (which would
    mask infrastructure failures from operators).

    Monkeypatches the descriptor-walk's terminal-open to raise
    ``OSError(EIO)`` and asserts the response is 5xx without the
    not-found envelope. The TestClient is configured with
    ``raise_server_exceptions=False`` so we observe Starlette's
    ServerErrorMiddleware-emitted 500 (matching uvicorn's
    production behavior on an uncaught exception) rather than
    having the exception propagate out of the test.
    """
    import errno as _errno

    from eden_wire import _artifact_fd as artifact_fd

    original_open = artifact_fd.os.open

    def _open_with_eio(*args, **kwargs):
        # Only fail on the terminal `O_RDONLY` open so the walk
        # gets far enough for our error to be the one that
        # propagates. Intermediate `O_PATH|O_DIRECTORY` opens
        # succeed.
        flags = args[1] if len(args) > 1 else kwargs.get("flags")
        if flags == artifact_fd._FILE_FLAGS:
            raise OSError(_errno.EIO, "simulated infrastructure failure")
        return original_open(*args, **kwargs)

    monkeypatch.setattr(artifact_fd.os, "open", _open_with_eio)

    app = make_app(store, admin_token=ADMIN_TOKEN, artifacts_dir=artifacts)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get(
        _route_url("hello.md"),
        headers={"Authorization": f"Bearer admin:{ADMIN_TOKEN}"},
    )
    assert resp.status_code >= 500, (
        f"EIO must surface as 5xx, got {resp.status_code}: "
        f"{resp.content[:200]!r}"
    )
    # MUST NOT be the not-found envelope (the round-3 fix narrowed
    # OSError → 404 to ENOENT/ENOTDIR/ENAMETOOLONG only).
    ct = resp.headers.get("content-type", "")
    if ct.startswith("application/"):
        try:
            body = resp.json()
            assert body.get("type") != "eden://error/not-found"
        except ValueError:
            pass  # non-JSON 500 body is fine, just not a 404 envelope


def test_directory_request_404(
    store: InMemoryStore, artifacts: Path
) -> None:
    """``GET <root>/ideas/idea-abc`` → 404 (directory, not a file)."""
    app = make_app(store, admin_token=ADMIN_TOKEN, artifacts_dir=artifacts)
    client = TestClient(app)
    resp = client.get(
        _route_url("ideas/idea-abc"),
        headers={"Authorization": f"Bearer admin:{ADMIN_TOKEN}"},
    )
    # Terminal opens with _FILE_FLAGS (O_RDONLY|O_NOFOLLOW); for a
    # real directory the open succeeds but fstat is_dir → 404.
    assert resp.status_code == 404


# ----------------------------------------------------------------------
# 1 MiB cap (§6.1 #7-8) + body delivery at the boundary
# ----------------------------------------------------------------------


def test_at_cap_boundary_200(
    store: InMemoryStore, tmp_path: Path
) -> None:
    """A file of EXACTLY 1 MiB serves successfully."""
    root = tmp_path / "art"
    root.mkdir()
    big = root / "big.bin"
    big.write_bytes(b"x" * (1024 * 1024))
    app = make_app(store, admin_token=ADMIN_TOKEN, artifacts_dir=root)
    client = TestClient(app)
    resp = client.get(
        _route_url("big.bin"),
        headers={"Authorization": f"Bearer admin:{ADMIN_TOKEN}"},
    )
    assert resp.status_code == 200
    assert len(resp.content) == 1024 * 1024
    assert resp.content == b"x" * (1024 * 1024)


def test_over_cap_413_no_bytes_leaked(
    store: InMemoryStore, tmp_path: Path
) -> None:
    """A file of 1 MiB + 1 byte → 413; problem+json only, no bytes."""
    root = tmp_path / "art"
    root.mkdir()
    too_big = root / "too-big.bin"
    too_big.write_bytes(b"x" * (1024 * 1024 + 1))
    app = make_app(store, admin_token=ADMIN_TOKEN, artifacts_dir=root)
    client = TestClient(app)
    resp = client.get(
        _route_url("too-big.bin"),
        headers={"Authorization": f"Bearer admin:{ADMIN_TOKEN}"},
    )
    assert resp.status_code == 413
    body = resp.json()
    assert body["type"] == "eden://reference-error/artifact-too-large"
    # MUST NOT carry any of the file's bytes.
    assert b"x" * 1024 not in resp.content


# ----------------------------------------------------------------------
# Auth-disabled posture (§6.1 #9) + 503 (§6.1 #10) + mismatch (§6.1 #11)
# ----------------------------------------------------------------------


def test_auth_disabled_serves_without_auth(
    store: InMemoryStore, artifacts: Path
) -> None:
    """admin_token=None (test posture) → route serves without auth."""
    app = make_app(store, admin_token=None, artifacts_dir=artifacts)
    client = TestClient(app)
    resp = client.get(_route_url("hello.md"))
    assert resp.status_code == 200
    assert resp.content == b"hello world\n"


def test_503_when_artifacts_dir_none(store: InMemoryStore) -> None:
    """artifacts_dir=None → route mounted but every request 503s."""
    app = make_app(store, admin_token=ADMIN_TOKEN, artifacts_dir=None)
    client = TestClient(app)
    resp = client.get(
        _route_url("anything.md"),
        headers={"Authorization": f"Bearer admin:{ADMIN_TOKEN}"},
    )
    assert resp.status_code == 503
    assert (
        resp.json()["type"]
        == "eden://reference-error/artifact-serving-disabled"
    )


def test_experiment_id_mismatch_400(
    store: InMemoryStore, artifacts: Path
) -> None:
    """URL experiment_id ≠ store.experiment_id → 400."""
    app = make_app(store, admin_token=ADMIN_TOKEN, artifacts_dir=artifacts)
    client = TestClient(app)
    resp = client.get(
        "/_reference/experiments/wrong-exp/artifacts/hello.md",
        headers={"Authorization": f"Bearer admin:{ADMIN_TOKEN}"},
    )
    assert resp.status_code == 400
    assert resp.json()["type"] == "eden://error/experiment-id-mismatch"


# ----------------------------------------------------------------------
# Edge cases (§6.2)
# ----------------------------------------------------------------------


def test_nested_path_walks_through_subdir(
    store: InMemoryStore, artifacts: Path
) -> None:
    """``ideas/idea-abc/content.md`` — descriptor walk through subdir."""
    app = make_app(store, admin_token=ADMIN_TOKEN, artifacts_dir=artifacts)
    client = TestClient(app)
    resp = client.get(
        _route_url("ideas/idea-abc/content.md"),
        headers={"Authorization": f"Bearer admin:{ADMIN_TOKEN}"},
    )
    assert resp.status_code == 200
    assert resp.content == b"## Idea\n\nDetails.\n"


def test_symlink_loop_403(
    store: InMemoryStore, artifacts: Path
) -> None:
    """``a → b``, ``b → a`` → terminal symlink → 403 via ELOOP."""
    (artifacts / "a").symlink_to("b")
    (artifacts / "b").symlink_to("a")
    app = make_app(store, admin_token=ADMIN_TOKEN, artifacts_dir=artifacts)
    client = TestClient(app)
    resp = client.get(
        _route_url("a"),
        headers={"Authorization": f"Bearer admin:{ADMIN_TOKEN}"},
    )
    assert resp.status_code == 403


def test_intermediate_symlink_403(
    store: InMemoryStore, artifacts: Path
) -> None:
    """A symlinked INTERMEDIATE dir → 403 via O_NOFOLLOW on the walk."""
    real_dir = artifacts / "real"
    real_dir.mkdir()
    (real_dir / "file.md").write_bytes(b"contents\n")
    # Replace the directory with a symlink to itself's parent's name.
    link = artifacts / "via-link"
    link.symlink_to(real_dir)
    app = make_app(store, admin_token=ADMIN_TOKEN, artifacts_dir=artifacts)
    client = TestClient(app)
    resp = client.get(
        _route_url("via-link/file.md"),
        headers={"Authorization": f"Bearer admin:{ADMIN_TOKEN}"},
    )
    assert resp.status_code == 403


# ----------------------------------------------------------------------
# Safe-delivery headers (§6.1 #18)
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("hello.md", b"## hello\n"),
        ("danger.html", b"<script>alert(1)</script>"),
        ("danger.svg", b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'),
        ("payload.bin", b"\x00\x01\x02\x03"),
    ],
)
def test_safe_delivery_headers_on_200(
    store: InMemoryStore,
    tmp_path: Path,
    filename: str,
    content: bytes,
) -> None:
    """Every 200 carries Content-Disposition: attachment,
    X-Content-Type-Options: nosniff, Content-Type:
    application/octet-stream — defeats stored-XSS via attacker-
    controlled .html / .svg artifacts.
    """
    root = tmp_path / "art"
    root.mkdir()
    (root / filename).write_bytes(content)
    app = make_app(store, admin_token=ADMIN_TOKEN, artifacts_dir=root)
    client = TestClient(app)
    resp = client.get(
        _route_url(filename),
        headers={"Authorization": f"Bearer admin:{ADMIN_TOKEN}"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/octet-stream"
    # Codex round-2: the route emits an RFC-6266 header with BOTH
    # legacy `filename="..."` and modern `filename*=UTF-8''...`.
    # For ASCII-safe filenames the legacy form mirrors the raw
    # basename; both forms reference it.
    cd = resp.headers["content-disposition"]
    assert cd.startswith("attachment; ")
    assert f'filename="{filename}"' in cd
    assert f"filename*=UTF-8''{filename}" in cd
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_hostile_filename_does_not_inject_header(
    store: InMemoryStore, tmp_path: Path
) -> None:
    """Codex round-2: an attacker-controlled filename containing
    quotes / backslashes / control chars (or CR/LF) MUST NOT break
    Content-Disposition syntax or inject extra headers. The route
    sanitizes the ASCII-form to a quoted-pair-escaped value AND
    emits the unambiguous percent-encoded filename*= form.
    """
    root = tmp_path / "art"
    root.mkdir()
    # Use a filename that exercises the escape paths: embedded
    # quote, backslash, and a NUL byte (which the path-walk would
    # reject earlier, but a control char like a tab is enough).
    hostile = 'evil";nasty=1\t.html'
    target = root / hostile
    target.write_bytes(b"<script>x</script>")
    # URL-quote since httpx wouldn't transmit special chars literally.
    from urllib.parse import quote as urlquote

    app = make_app(store, admin_token=ADMIN_TOKEN, artifacts_dir=root)
    client = TestClient(app)
    resp = client.get(
        _route_url(urlquote(hostile)),
        headers={"Authorization": f"Bearer admin:{ADMIN_TOKEN}"},
    )
    assert resp.status_code == 200
    cd = resp.headers["content-disposition"]
    # Header MUST be syntactically well-formed and MUST NOT
    # contain the bare attacker payload.
    assert cd.startswith("attachment; ")
    assert 'filename="evil\\";nasty=1.html"' in cd  # quote escaped
    # Tab (control char) is stripped from the ASCII form.
    assert "\t" not in cd
    # No CR/LF — would be a header-injection vector.
    assert "\r" not in cd
    assert "\n" not in cd
    # The filename*= form percent-encodes the original.
    assert "filename*=UTF-8''" in cd


def test_safe_delivery_headers_at_cap_boundary(
    store: InMemoryStore, tmp_path: Path
) -> None:
    """The 1 MiB boundary file also gets the safe-delivery headers."""
    root = tmp_path / "art"
    root.mkdir()
    (root / "big.bin").write_bytes(b"y" * (1024 * 1024))
    app = make_app(store, admin_token=ADMIN_TOKEN, artifacts_dir=root)
    client = TestClient(app)
    resp = client.get(
        _route_url("big.bin"),
        headers={"Authorization": f"Bearer admin:{ADMIN_TOKEN}"},
    )
    assert resp.status_code == 200
    cd = resp.headers["content-disposition"]
    assert cd.startswith("attachment; ")
    assert 'filename="big.bin"' in cd
    assert "filename*=UTF-8''big.bin" in cd
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["content-type"] == "application/octet-stream"


# ----------------------------------------------------------------------
# TOCTOU terminal-component swap (§6.1 #15)
# ----------------------------------------------------------------------


def test_toctou_swap_never_serves_oversized_file(
    store: InMemoryStore, tmp_path: Path
) -> None:
    """Concurrent rename swapping 100B file ↔ 2 MiB decoy under
    the SAME path → responses are either 200-with-100-bytes,
    404, or 413; NEVER 200-with-2-MiB-bytes, NEVER 403 (which
    is reserved for symlinks).
    """
    root = tmp_path / "art"
    root.mkdir()
    target = root / "target.bin"
    decoy = root / "decoy.bin"
    target.write_bytes(b"x" * 100)
    decoy.write_bytes(b"D" * (2 * 1024 * 1024))

    stop = threading.Event()

    def swap_loop() -> None:
        # Pre-populate BOTH staging files (the prior diff only
        # wrote `small_a` and referenced `small_b`, which would
        # FileNotFoundError on the first swap and never alternate
        # — Codex round-2 finding). Each iteration: replace target
        # in-place, then re-stage the just-consumed source so the
        # next swap has fresh bytes to move.
        small_src = root / ".tmp-small.bin"
        big_src = root / ".tmp-big.bin"
        small_src.write_bytes(b"x" * 100)
        big_src.write_bytes(b"D" * (2 * 1024 * 1024))
        flag = False
        while not stop.is_set():
            try:
                if flag:
                    os.replace(small_src, target)
                    small_src.write_bytes(b"x" * 100)
                else:
                    os.replace(big_src, target)
                    big_src.write_bytes(b"D" * (2 * 1024 * 1024))
            except FileNotFoundError:
                # Inode disappeared mid-swap; recreate both staging
                # files and continue.
                small_src.write_bytes(b"x" * 100)
                big_src.write_bytes(b"D" * (2 * 1024 * 1024))
                continue
            flag = not flag

    swapper = threading.Thread(target=swap_loop, daemon=True)

    app = make_app(store, admin_token=ADMIN_TOKEN, artifacts_dir=root)
    client = TestClient(app)
    swapper.start()
    try:
        # Fire many concurrent GETs.
        for _ in range(20):
            resp = client.get(
                _route_url("target.bin"),
                headers={"Authorization": f"Bearer admin:{ADMIN_TOKEN}"},
            )
            assert resp.status_code in (200, 404, 413), (
                f"unexpected status {resp.status_code}; "
                f"body[:100]={resp.content[:100]!r}"
            )
            # Critical invariant: a 200 NEVER carries 2 MiB.
            if resp.status_code == 200:
                assert len(resp.content) <= 1024 * 1024
                # And it MUST NOT contain the decoy marker pattern.
                # (Rename atomically swaps inodes; a 200 with the
                # decoy inode would be > 1 MiB, but we cap at 1 MiB —
                # so the read could in principle return 1 MiB of "D"s.
                # That's still a privilege violation: it would mean
                # we served 1 MiB from an oversized file by skipping
                # the size check. Assert the body is the small file's
                # contents exactly.)
                assert resp.content == b"x" * 100, (
                    "200 response served wrong content; "
                    f"got len={len(resp.content)} starting with "
                    f"{resp.content[:20]!r}"
                )
            # 403 is reserved for symlinks; benign renames must not
            # produce one.
            assert resp.status_code != 403
    finally:
        stop.set()
        swapper.join(timeout=1.0)


def test_toctou_intermediate_swap_to_symlink_race(
    store: InMemoryStore, tmp_path: Path
) -> None:
    """Codex round-2: a concurrent rename swapping an intermediate
    component between a real directory and a symlink-to-/etc MUST
    NOT serve bytes from outside the artifacts root. The
    descriptor-walk locks each step by the prior fd, so even a
    perfectly-timed swap between our pre-open `lstat` and the
    `os.open` call hits ELOOP on the open (or harmlessly fails
    later).
    """
    root = tmp_path / "art"
    root.mkdir()
    realdir = root / "dir"
    realdir.mkdir()
    (realdir / "file.md").write_bytes(b"safe contents\n")

    # Staging: a symlink to /etc and a backup of the real dir name.
    decoy = root / ".tmp-symlink"
    decoy.symlink_to("/etc")

    stop = threading.Event()

    def swap_loop() -> None:
        # Alternate `<root>/dir` between the real dir and a symlink
        # to /etc. We rename the existing `dir` away, drop the
        # symlink in its place, then swap back. The descriptor
        # walk MUST never resolve a request beneath the symlinked
        # form.
        renamed = root / ".tmp-dir-stash"
        flag = False
        while not stop.is_set():
            try:
                if flag:
                    # Restore real dir.
                    if not (root / "dir").exists():
                        os.replace(renamed, root / "dir")
                else:
                    # Move real dir aside, drop symlink in place.
                    if (root / "dir").is_dir() and not (root / "dir").is_symlink():
                        os.rename(root / "dir", renamed)
                        # Symlinks can't be overwritten by os.replace,
                        # so create the new symlink directly.
                        (root / "dir").symlink_to("/etc")
                    elif (root / "dir").is_symlink():
                        # Remove the symlink and restore.
                        (root / "dir").unlink()
                        os.replace(renamed, root / "dir")
            except (FileNotFoundError, FileExistsError, OSError):
                continue
            flag = not flag

    swapper = threading.Thread(target=swap_loop, daemon=True)
    app = make_app(store, admin_token=ADMIN_TOKEN, artifacts_dir=root)
    client = TestClient(app)
    swapper.start()
    try:
        for _ in range(40):
            resp = client.get(
                _route_url("dir/file.md"),
                headers={"Authorization": f"Bearer admin:{ADMIN_TOKEN}"},
            )
            # Allowed: 200-with-safe-contents (real dir wins), 403
            # (symlink hit), 404 (race during swap).
            assert resp.status_code in (200, 403, 404), (
                f"unexpected {resp.status_code}; body[:80]={resp.content[:80]!r}"
            )
            if resp.status_code == 200:
                # MUST be the safe content, never bytes from /etc.
                assert resp.content == b"safe contents\n", (
                    f"served unexpected bytes: {resp.content[:80]!r}"
                )
    finally:
        stop.set()
        swapper.join(timeout=1.0)


# ----------------------------------------------------------------------
# Error class shape sanity
# ----------------------------------------------------------------------


def test_reference_error_classes_exposed() -> None:
    """The three reference-error subclasses live under eden_wire.errors."""
    assert issubclass(InvalidPath, Exception)
    assert issubclass(ArtifactTooLarge, Exception)
    assert issubclass(ArtifactServingDisabled, Exception)


# ----------------------------------------------------------------------
# Binding-doc §1.1 lock (§6.7)
# ----------------------------------------------------------------------


def test_binding_doc_mentions_substrate_env_vars() -> None:
    """The four new env vars are documented in the binding's §1.1
    table. Catches the "added env var, forgot to document it" footgun.
    """
    binding = (
        Path(__file__).resolve().parents[4]
        / "spec/v0/reference-bindings/worker-host-subprocess.md"
    )
    assert binding.is_file(), f"binding doc not found at {binding}"
    text = binding.read_text(encoding="utf-8")
    for var in (
        "EDEN_REPO_DIR",
        "EDEN_ARTIFACT_URL",
        "EDEN_ARTIFACT_PATH_ROOT",
        "EDEN_READONLY_STORE_URL",
    ):
        assert (
            f"| `{var}` |" in text or f"`{var}`" in text
        ), f"binding doc missing {var}"
    # §9 (substrate-access) was added in 12a-1f.
    assert "## 9. Substrate read-access" in text
