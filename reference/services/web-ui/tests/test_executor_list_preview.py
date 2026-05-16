"""Executor list preview tests (phase 12a-1c, wave 3)."""

from __future__ import annotations

from pathlib import Path

from conftest import seed_implement_task
from eden_storage import InMemoryStore
from fastapi.testclient import TestClient

BASE_SHA = "a" * 40


def _signed_in(client: TestClient) -> TestClient:
    resp = client.post("/signin", follow_redirects=False)
    assert resp.status_code == 303
    return client


class TestExecutorListPreview:
    def test_row_count_matches_pending(
        self,
        exec_client: TestClient,
        store: InMemoryStore,
        artifacts_dir: Path,
    ) -> None:
        client = _signed_in(exec_client)
        seed_implement_task(
            store, slug="alpha", artifacts_dir=artifacts_dir, base_sha=BASE_SHA
        )
        seed_implement_task(
            store, slug="beta", artifacts_dir=artifacts_dir, base_sha=BASE_SHA
        )
        resp = client.get("/executor/")
        assert resp.status_code == 200
        body = resp.text
        # Both task ids are surfaced
        assert "execute-alpha" in body
        assert "execute-beta" in body

    def test_preview_renders_idea_slug_priority_parent_commits(
        self,
        exec_client: TestClient,
        store: InMemoryStore,
        artifacts_dir: Path,
    ) -> None:
        client = _signed_in(exec_client)
        seed_implement_task(
            store, slug="alpha", artifacts_dir=artifacts_dir, base_sha=BASE_SHA
        )
        resp = client.get("/executor/")
        assert resp.status_code == 200
        body = resp.text
        # slug + priority + first 8 chars of parent commit
        assert "alpha" in body
        assert "1.0" in body
        assert "aaaaaaaa" in body  # BASE_SHA fixture starts with 40 'a's

    def test_preview_details_carries_content_when_file_artifact(
        self,
        exec_client: TestClient,
        store: InMemoryStore,
        artifacts_dir: Path,
    ) -> None:
        client = _signed_in(exec_client)
        seed_implement_task(
            store,
            slug="alpha",
            artifacts_dir=artifacts_dir,
            artifact_text="hello idea body",
            base_sha=BASE_SHA,
        )
        resp = client.get("/executor/")
        assert resp.status_code == 200
        assert "<details>" in resp.text
        assert "hello idea body" in resp.text

    def test_preview_falls_back_when_non_file_uri(
        self,
        exec_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        client = _signed_in(exec_client)
        # Pass artifacts_dir=None so the seed helper uses an https URI
        seed_implement_task(
            store, slug="alpha", artifacts_dir=None, base_sha=BASE_SHA
        )
        resp = client.get("/executor/")
        assert resp.status_code == 200
        assert "content unavailable" in resp.text

    def test_preview_idea_unavailable_when_read_idea_404s(
        self,
        exec_client: TestClient,
        store: InMemoryStore,
        artifacts_dir: Path,
    ) -> None:
        client = _signed_in(exec_client)
        seed_implement_task(
            store, slug="alpha", artifacts_dir=artifacts_dir, base_sha=BASE_SHA
        )
        from eden_storage.errors import NotFound

        def _missing(_: str) -> object:
            raise NotFound("no such idea")

        store.read_idea = _missing  # type: ignore[method-assign]
        resp = client.get("/executor/")
        assert resp.status_code == 200
        assert "(idea unavailable)" in resp.text

    def test_preview_transport_error_renders_banner(
        self,
        exec_client: TestClient,
        store: InMemoryStore,
        artifacts_dir: Path,
    ) -> None:
        client = _signed_in(exec_client)
        seed_implement_task(
            store, slug="alpha", artifacts_dir=artifacts_dir, base_sha=BASE_SHA
        )

        def _flaky(_: str) -> object:
            raise RuntimeError("transport blip")

        store.read_idea = _flaky  # type: ignore[method-assign]
        resp = client.get("/executor/")
        assert resp.status_code == 200
        assert "idea read(s) failed" in resp.text
        assert "(read failed)" in resp.text

    def test_preview_renders_target_anyone_by_default(
        self,
        exec_client: TestClient,
        store: InMemoryStore,
        artifacts_dir: Path,
    ) -> None:
        client = _signed_in(exec_client)
        seed_implement_task(
            store, slug="alpha", artifacts_dir=artifacts_dir, base_sha=BASE_SHA
        )
        resp = client.get("/executor/")
        assert resp.status_code == 200
        assert "anyone" in resp.text

    def test_admin_lineage_link_is_present(
        self,
        exec_client: TestClient,
        store: InMemoryStore,
        artifacts_dir: Path,
    ) -> None:
        client = _signed_in(exec_client)
        task_id, _ = seed_implement_task(
            store, slug="alpha", artifacts_dir=artifacts_dir, base_sha=BASE_SHA
        )
        resp = client.get("/executor/")
        assert resp.status_code == 200
        assert f"/admin/tasks/{task_id}/" in resp.text
