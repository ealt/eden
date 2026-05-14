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

EXPERIMENT_ID = "exp-12a-1f"
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


def _register_worker(client: TestClient, worker_id: str = "alice") -> str:
    resp = client.post(
        f"/v0/experiments/{EXPERIMENT_ID}/workers",
        headers={
            "X-Eden-Experiment-Id": EXPERIMENT_ID,
            "Authorization": f"Bearer admin:{ADMIN_TOKEN}",
        },
        json={"worker_id": worker_id},
    )
    assert resp.status_code == 200
    return resp.json()["registration_token"]


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
    from eden_wire import server as srv

    calls: list[str] = []

    def _fail_open(*a, **kw):
        calls.append("_open_artifact_fd")
        raise AssertionError("filesystem touched before auth!")

    def _fail_fstat(*a, **kw):
        calls.append("os.fstat")
        raise AssertionError("os.fstat touched before auth!")

    monkeypatch.setattr(srv, "_open_artifact_fd", _fail_open)
    monkeypatch.setattr(srv.os, "fstat", _fail_fstat)

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
    token = _register_worker(client, "alice")
    resp = client.get(
        _route_url("hello.md"),
        headers={"Authorization": f"Bearer alice:{token}"},
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
    assert (
        resp.headers["content-disposition"]
        == f'attachment; filename="{filename}"'
    )
    assert resp.headers["x-content-type-options"] == "nosniff"


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
    assert resp.headers["content-disposition"] == 'attachment; filename="big.bin"'
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
        small_a = root / ".tmp-small-a.bin"
        small_b = root / ".tmp-small-b.bin"
        small_a.write_bytes(b"x" * 100)
        big = root / ".tmp-big.bin"
        big.write_bytes(b"D" * (2 * 1024 * 1024))
        # Repeatedly rename target between small and big.
        flag = False
        while not stop.is_set():
            try:
                if flag:
                    os.replace(small_b, target)
                    small_a.write_bytes(b"x" * 100)
                else:
                    os.replace(big, target)
                    big = root / ".tmp-big.bin"
                    big.write_bytes(b"D" * (2 * 1024 * 1024))
            except FileNotFoundError:
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
