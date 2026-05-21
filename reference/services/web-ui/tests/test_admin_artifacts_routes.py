"""Per-route tests for the artifacts directory-listing admin module.

Issue #107: ``GET /admin/artifacts/`` lists files under the
configured ``--artifacts-dir`` recursively, each linking to the
existing ``GET /artifacts?uri=file://...`` serving route. Tests
cover auth gate, empty-state banner, populated render, recursive
walk, and path-containment (symlink-escape + ``..``-escape).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import quote

from fastapi.testclient import TestClient


class TestAdminArtifactsAuthGate:
    def test_get_redirects_unauthenticated(self, client: TestClient) -> None:
        resp = client.get("/admin/artifacts/", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/signin"


class TestAdminArtifactsEmptyState:
    def test_empty_dir_renders_banner(self, signed_in_client: TestClient) -> None:
        resp = signed_in_client.get("/admin/artifacts/")
        assert resp.status_code == 200
        assert "no artifact files found" in resp.text
        assert "scripted-mode deployments produce no artifact files" in resp.text


class TestAdminArtifactsPopulated:
    def test_lists_file_with_size_mtime_and_serve_link(
        self,
        signed_in_client: TestClient,
        artifacts_dir: Path,
    ) -> None:
        target = artifacts_dir / "idea-demo.md"
        target.write_text("hello world\n")
        resp = signed_in_client.get("/admin/artifacts/")
        assert resp.status_code == 200
        assert "idea-demo.md" in resp.text
        # File size in bytes appears verbatim.
        assert str(target.stat().st_size) in resp.text
        # The serve link is a real RFC-8089 file URI produced by
        # ``Path.as_uri()`` (percent-encodes reserved chars in the
        # path), then re-encoded by Jinja's ``urlencode`` filter for
        # safe inclusion in the querystring.
        expected_uri = quote(target.resolve().as_uri(), safe="/")
        assert f"/artifacts?uri={expected_uri}" in resp.text

    def test_recursive_walk_at_depth_gt_one(
        self,
        signed_in_client: TestClient,
        artifacts_dir: Path,
    ) -> None:
        nested = artifacts_dir / "a" / "b" / "c"
        nested.mkdir(parents=True)
        (nested / "deep.txt").write_text("payload")
        resp = signed_in_client.get("/admin/artifacts/")
        assert resp.status_code == 200
        assert "a/b/c/deep.txt" in resp.text

    def test_link_back_to_artifact_serves_file(
        self,
        signed_in_client: TestClient,
        artifacts_dir: Path,
    ) -> None:
        target = artifacts_dir / "served.txt"
        target.write_text("served-bytes")
        resp = signed_in_client.get(
            f"/artifacts?uri=file://{target.resolve()}",
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert resp.text == "served-bytes"

    def test_rendered_link_round_trips_for_reserved_chars(
        self,
        signed_in_client: TestClient,
        artifacts_dir: Path,
    ) -> None:
        """Filenames containing URI-reserved chars (``?`` / ``#``)
        must produce links that round-trip to the serving route.

        Codex round-0 finding: ``serve_uri = f"file://{resolved}"``
        wasn't a valid file URI for these chars — the listing
        rendered a clickable link that 404'd when followed because
        ``urlparse`` split the path at the unescaped ``?``. The fix
        is ``Path.as_uri()`` (percent-encodes reserved bytes).
        """
        for name, payload in (
            ("a?b.txt", "with-question"),
            ("c#d.txt", "with-hash"),
            ("e f.txt", "with-space"),
        ):
            target = artifacts_dir / name
            target.write_text(payload)
            resp = signed_in_client.get("/admin/artifacts/")
            assert resp.status_code == 200
            match = re.search(
                r'href="(/artifacts\?uri=[^"]+)">\s*<code>'
                + re.escape(name)
                + r"</code>",
                resp.text,
            )
            assert match is not None, (
                f"no listing link found for {name}; response was: {resp.text}"
            )
            follow = signed_in_client.get(
                match.group(1), follow_redirects=False
            )
            assert follow.status_code == 200, (
                f"following the rendered link for {name} returned "
                f"{follow.status_code}"
            )
            assert follow.text == payload


class TestAdminArtifactsContainment:
    def test_symlink_to_outside_jail_is_skipped(
        self,
        signed_in_client: TestClient,
        artifacts_dir: Path,
        tmp_path: Path,
    ) -> None:
        outside = tmp_path / "outside-secret.txt"
        outside.write_text("do-not-leak")
        link = artifacts_dir / "escape.txt"
        os.symlink(outside, link)
        resp = signed_in_client.get("/admin/artifacts/")
        assert resp.status_code == 200
        # The symlink target lives outside artifacts_dir and must be
        # silently dropped — neither the path nor any payload bytes
        # may leak into the rendered listing.
        assert "escape.txt" not in resp.text
        assert "outside-secret.txt" not in resp.text
        assert "do-not-leak" not in resp.text

    def test_traversal_uri_returns_404_from_serving_route(
        self,
        signed_in_client: TestClient,
        artifacts_dir: Path,
        tmp_path: Path,
    ) -> None:
        # The listing endpoint never accepts a path from the caller,
        # so traversal containment is enforced exclusively at the
        # serving route. Sanity-check the existing posture here so a
        # future refactor that lets the listing accept caller-supplied
        # paths still has explicit test coverage.
        outside = tmp_path / "outside.txt"
        outside.write_text("nope")
        resp = signed_in_client.get(
            f"/artifacts?uri=file://{outside.resolve()}",
            follow_redirects=False,
        )
        assert resp.status_code == 404
