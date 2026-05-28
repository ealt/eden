"""Multi-file executor submission flow (issue #212; follow-up to #120)."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote, urlparse

import pytest
from conftest import (
    get_csrf,
    make_child_commit,
    seed_implement_task,
)
from eden_git import GitRepo
from eden_storage import InMemoryStore, VariantSubmission
from eden_web_ui.routes import executor as executor_routes
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _clear_claims():
    executor_routes._CLAIMS.clear()
    yield
    executor_routes._CLAIMS.clear()


def _claim(
    client: TestClient,
    store: InMemoryStore,
    base_sha: str,
    *,
    slug: str,
) -> str:
    task_id, _ = seed_implement_task(store, base_sha=base_sha, slug=slug)
    csrf = get_csrf(client)
    resp = client.post(
        f"/executor/{task_id}/claim",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    return task_id


def _variant_submission(store: InMemoryStore, task_id: str) -> VariantSubmission:
    sub = store.read_submission(task_id)
    assert isinstance(sub, VariantSubmission)
    return sub


class TestArtifactBundle:
    def test_text_plus_files_writes_bundle(
        self,
        signed_in_impl_client: TestClient,
        store: InMemoryStore,
        bare_repo: GitRepo,
        base_sha: str,
    ) -> None:
        task_id = _claim(signed_in_impl_client, store, base_sha, slug="alpha")
        csrf = get_csrf(signed_in_impl_client)
        child_sha = make_child_commit(bare_repo, base_sha, "alpha-tip")

        resp = signed_in_impl_client.post(
            f"/executor/{task_id}/submit",
            data={
                "csrf_token": csrf,
                "status": "success",
                "commit_sha": child_sha,
                "description": "alpha variant",
                "artifact_text": "## build log\n\nbuilt cleanly",
                "artifacts_uri": "",
            },
            files={"artifact_files": ("build.svg", b"<svg/>", "image/svg+xml")},
        )
        assert resp.status_code == 200, resp.text
        recorded = _variant_submission(store, task_id)
        assert recorded.artifacts_uri is not None
        assert recorded.artifacts_uri.endswith(".tar.gz")

        # The serving route streams entries by name out of the bundle.
        uri = recorded.artifacts_uri
        m = signed_in_impl_client.get(
            f"/artifacts?uri={quote(uri, safe='')}&entry=variant.md"
        )
        assert m.status_code == 200
        assert "build log" in m.text

        s = signed_in_impl_client.get(
            f"/artifacts?uri={quote(uri, safe='')}&entry=build.svg"
        )
        assert s.status_code == 200
        assert s.content == b"<svg/>"

    def test_only_text_writes_single_md(
        self,
        signed_in_impl_client: TestClient,
        store: InMemoryStore,
        bare_repo: GitRepo,
        base_sha: str,
    ) -> None:
        task_id = _claim(signed_in_impl_client, store, base_sha, slug="bravo")
        csrf = get_csrf(signed_in_impl_client)
        child_sha = make_child_commit(bare_repo, base_sha, "bravo-tip")

        resp = signed_in_impl_client.post(
            f"/executor/{task_id}/submit",
            data={
                "csrf_token": csrf,
                "status": "success",
                "commit_sha": child_sha,
                "description": "",
                "artifact_text": "## build log",
                "artifacts_uri": "",
            },
        )
        assert resp.status_code == 200, resp.text
        recorded = _variant_submission(store, task_id)
        assert recorded.artifacts_uri is not None
        parsed = urlparse(recorded.artifacts_uri)
        assert parsed.path.endswith(".md")
        assert "build log" in Path(parsed.path).read_text()

    def test_only_one_file_writes_single_artifact(
        self,
        signed_in_impl_client: TestClient,
        store: InMemoryStore,
        bare_repo: GitRepo,
        base_sha: str,
    ) -> None:
        task_id = _claim(signed_in_impl_client, store, base_sha, slug="charlie")
        csrf = get_csrf(signed_in_impl_client)
        child_sha = make_child_commit(bare_repo, base_sha, "charlie-tip")

        resp = signed_in_impl_client.post(
            f"/executor/{task_id}/submit",
            data={
                "csrf_token": csrf,
                "status": "success",
                "commit_sha": child_sha,
                "description": "",
                "artifact_text": "",
                "artifacts_uri": "",
            },
            files={"artifact_files": ("out.bin", b"\x00\x01\x02", "application/octet-stream")},
        )
        assert resp.status_code == 200, resp.text
        recorded = _variant_submission(store, task_id)
        assert recorded.artifacts_uri is not None
        parsed = urlparse(recorded.artifacts_uri)
        assert parsed.path.endswith(".bin")
        assert Path(parsed.path).read_bytes() == b"\x00\x01\x02"

    def test_explicit_uri_overrides_bundling(
        self,
        signed_in_impl_client: TestClient,
        store: InMemoryStore,
        bare_repo: GitRepo,
        base_sha: str,
    ) -> None:
        task_id = _claim(signed_in_impl_client, store, base_sha, slug="delta")
        csrf = get_csrf(signed_in_impl_client)
        child_sha = make_child_commit(bare_repo, base_sha, "delta-tip")

        explicit = "https://example.invalid/build-output.html"
        resp = signed_in_impl_client.post(
            f"/executor/{task_id}/submit",
            data={
                "csrf_token": csrf,
                "status": "success",
                "commit_sha": child_sha,
                "description": "",
                # Both an explicit URI AND text+files — explicit wins.
                "artifact_text": "ignored",
                "artifacts_uri": explicit,
            },
            files={"artifact_files": ("ignored.txt", b"x", "text/plain")},
        )
        assert resp.status_code == 200, resp.text
        recorded = _variant_submission(store, task_id)
        assert recorded.artifacts_uri == explicit

    def test_no_text_no_files_no_uri_skips_artifact(
        self,
        signed_in_impl_client: TestClient,
        store: InMemoryStore,
        bare_repo: GitRepo,
        base_sha: str,
    ) -> None:
        """Existing 'no artifact' path keeps working."""
        task_id = _claim(signed_in_impl_client, store, base_sha, slug="echo")
        csrf = get_csrf(signed_in_impl_client)
        child_sha = make_child_commit(bare_repo, base_sha, "echo-tip")

        resp = signed_in_impl_client.post(
            f"/executor/{task_id}/submit",
            data={
                "csrf_token": csrf,
                "status": "success",
                "commit_sha": child_sha,
                "description": "",
                "artifact_text": "",
                "artifacts_uri": "",
            },
        )
        assert resp.status_code == 200, resp.text
        recorded = _variant_submission(store, task_id)
        assert recorded.artifacts_uri is None
