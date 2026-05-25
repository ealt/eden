"""Multi-file upload flow tests for the ideator (issue #120).

Covers:

- text + uploads produce a ``.tar.gz`` bundle reachable via the
  artifact-serving route, with the manifest table rendered on the
  evaluator/executor draft views.
- single upload (no text) produces a direct file under the original
  extension; ``content`` is no longer required when a file is
  attached.
- text-only continues to write ``<id>.md`` (back-compat).
- per-row uploads land on the correct idea (row-0 vs row-1 isolation).
- duplicate-filename uploads surface as a per-row form error rather
  than a 500.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote, urlparse

from conftest import get_csrf
from eden_storage import InMemoryStore
from fastapi.testclient import TestClient


def _claim(client: TestClient, task_id: str, token: str) -> None:
    resp = client.post(
        f"/ideator/{task_id}/claim",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303


class TestSingleFileOnly:
    def test_single_upload_no_text_writes_direct_file(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        artifacts_dir: Path,
    ) -> None:
        store.create_ideation_task("t-single")
        token = get_csrf(signed_in_client)
        _claim(signed_in_client, "t-single", token)

        resp = signed_in_client.post(
            "/ideator/t-single/submit",
            data={
                "csrf_token": token,
                "status": "success",
                "slug": "single-pdf",
                "priority": "1.0",
                "parent_commits": "a" * 40,
                "content": "",
                "intended_executor_kind": "none",
                "intended_executor_id": "",
            },
            files={"files_0": ("design.pdf", b"%PDF-1.4 fake", "application/pdf")},
        )
        assert resp.status_code == 200, resp.text
        ideas = store.list_ideas(state="ready")
        assert len(ideas) == 1
        parsed = urlparse(ideas[0].artifacts_uri)
        assert parsed.path.endswith(".pdf")
        assert Path(parsed.path).read_bytes() == b"%PDF-1.4 fake"


class TestTextPlusUploadsBundle:
    def test_text_plus_upload_writes_bundle(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        artifacts_dir: Path,
    ) -> None:
        store.create_ideation_task("t-bundle")
        token = get_csrf(signed_in_client)
        _claim(signed_in_client, "t-bundle", token)

        resp = signed_in_client.post(
            "/ideator/t-bundle/submit",
            data={
                "csrf_token": token,
                "status": "success",
                "slug": "design-with-diagram",
                "priority": "1.0",
                "parent_commits": "a" * 40,
                "content": "## design\n\nuse SVG for diagrams",
                "intended_executor_kind": "none",
                "intended_executor_id": "",
            },
            files={"files_0": ("arch.svg", b"<svg/>", "image/svg+xml")},
        )
        assert resp.status_code == 200, resp.text
        ideas = store.list_ideas(state="ready")
        assert len(ideas) == 1
        assert ideas[0].artifacts_uri.endswith(".tar.gz")

        # Bundle is reachable via the serving route, and the
        # manifest's idea.md headline + svg entry can be streamed.
        uri = ideas[0].artifacts_uri
        manifest_resp = signed_in_client.get(
            f"/artifacts?uri={quote(uri, safe='')}&entry=manifest.json"
        )
        assert manifest_resp.status_code == 200
        assert "idea.md" in manifest_resp.text
        assert "arch.svg" in manifest_resp.text

        headline = signed_in_client.get(
            f"/artifacts?uri={quote(uri, safe='')}&entry=idea.md"
        )
        assert headline.status_code == 200
        assert "design" in headline.text

        svg = signed_in_client.get(
            f"/artifacts?uri={quote(uri, safe='')}&entry=arch.svg"
        )
        assert svg.status_code == 200
        assert svg.content == b"<svg/>"


class TestMultiUploadNoText:
    def test_two_uploads_no_text_writes_bundle(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        store.create_ideation_task("t-2up")
        token = get_csrf(signed_in_client)
        _claim(signed_in_client, "t-2up", token)

        files = [
            ("files_0", ("a.txt", b"alpha", "text/plain")),
            ("files_0", ("b.txt", b"beta", "text/plain")),
        ]
        resp = signed_in_client.post(
            "/ideator/t-2up/submit",
            data={
                "csrf_token": token,
                "status": "success",
                "slug": "two-files",
                "priority": "1.0",
                "parent_commits": "a" * 40,
                "content": "",
                "intended_executor_kind": "none",
                "intended_executor_id": "",
            },
            files=files,
        )
        assert resp.status_code == 200, resp.text
        ideas = store.list_ideas(state="ready")
        assert len(ideas) == 1
        assert ideas[0].artifacts_uri.endswith(".tar.gz")


class TestPerRowIsolation:
    def test_row0_uploads_dont_leak_to_row1(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        store.create_ideation_task("t-multi-row")
        token = get_csrf(signed_in_client)
        _claim(signed_in_client, "t-multi-row", token)

        resp = signed_in_client.post(
            "/ideator/t-multi-row/submit",
            data={
                "csrf_token": token,
                "status": "success",
                "slug": ["row-zero", "row-one"],
                "priority": ["1.0", "1.0"],
                "parent_commits": ["a" * 40, "a" * 40],
                "content": ["", "## row 1 plain text"],
                "intended_executor_kind": ["none", "none"],
                "intended_executor_id": ["", ""],
            },
            files={"files_0": ("only-row0.txt", b"row0", "text/plain")},
        )
        assert resp.status_code == 200, resp.text
        ideas = {i.slug: i for i in store.list_ideas(state="ready")}
        assert set(ideas) == {"row-zero", "row-one"}
        # Row 0 → single .txt direct file (no text, one upload).
        assert ideas["row-zero"].artifacts_uri.endswith(".txt")
        # Row 1 → text-only `.md`.
        assert ideas["row-one"].artifacts_uri.endswith(".md")


class TestErrorPaths:
    def test_no_content_no_files_re_renders_with_error(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        store.create_ideation_task("t-empty")
        token = get_csrf(signed_in_client)
        _claim(signed_in_client, "t-empty", token)

        resp = signed_in_client.post(
            "/ideator/t-empty/submit",
            data={
                "csrf_token": token,
                "status": "success",
                "slug": "needs-content",
                "priority": "1.0",
                "parent_commits": "a" * 40,
                "content": "",
                "intended_executor_kind": "none",
                "intended_executor_id": "",
            },
        )
        assert resp.status_code == 400
        assert "content markdown is required" in resp.text
        # Nothing was created.
        assert store.list_ideas() == []

    def test_duplicate_upload_filenames_renders_artifact_error(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        store.create_ideation_task("t-dup")
        token = get_csrf(signed_in_client)
        _claim(signed_in_client, "t-dup", token)

        files = [
            ("files_0", ("collide.txt", b"first", "text/plain")),
            ("files_0", ("collide.txt", b"second", "text/plain")),
        ]
        resp = signed_in_client.post(
            "/ideator/t-dup/submit",
            data={
                "csrf_token": token,
                "status": "success",
                "slug": "dup-files",
                "priority": "1.0",
                "parent_commits": "a" * 40,
                "content": "x",
                "intended_executor_kind": "none",
                "intended_executor_id": "",
            },
            files=files,
        )
        # Bundling rejected → no store mutation, form re-renders with
        # validation_errors path (rendered as 400 by the route).
        # The persist_error path uses 502 only for transport-shaped
        # failures.
        assert resp.status_code in (400, 502)
        assert "duplicate" in resp.text.lower()
        assert store.list_ideas() == []
