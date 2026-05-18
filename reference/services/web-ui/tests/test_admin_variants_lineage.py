"""Lineage rendering tests for admin variant detail (phase 12a-1c)."""

from __future__ import annotations

from conftest import seed_evaluate_task
from eden_storage import InMemoryStore
from fastapi.testclient import TestClient


def _signed_in(client: TestClient) -> TestClient:
    resp = client.post("/signin", follow_redirects=False)
    assert resp.status_code == 303
    return client


class TestVariantLineageSection:
    def test_variant_detail_renders_lineage_section(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        _signed_in(client)
        _, variant_id, _ = seed_evaluate_task(store)
        resp = client.get(f"/admin/variants/{variant_id}/")
        assert resp.status_code == 200
        body = resp.text
        assert "<h2>lineage</h2>" in body

    def test_variant_lineage_links_to_parent_idea(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        _signed_in(client)
        _, variant_id, idea_id = seed_evaluate_task(store)
        resp = client.get(f"/admin/variants/{variant_id}/")
        assert resp.status_code == 200
        assert f'/admin/ideas/{idea_id}/' in resp.text

    def test_variant_lineage_links_to_producing_execution_task(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        _signed_in(client)
        _, variant_id, _ = seed_evaluate_task(store)
        # seed_evaluate_task seeds the execution task as `execute-demo`
        resp = client.get(f"/admin/variants/{variant_id}/")
        assert resp.status_code == 200
        assert "/admin/tasks/execute-demo/" in resp.text

    def test_variant_lineage_lists_evaluation_tasks(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        _signed_in(client)
        eval_task_id, variant_id, _ = seed_evaluate_task(store)
        resp = client.get(f"/admin/variants/{variant_id}/")
        assert resp.status_code == 200
        assert f"/admin/tasks/{eval_task_id}/" in resp.text

    def test_variant_lineage_transport_error_banner(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        _signed_in(client)
        _, variant_id, _ = seed_evaluate_task(store)
        # Cause a transport-shaped failure in the lineage helper
        # without breaking the outer route (which calls read_variant /
        # read_idea / list_tasks). ``read_submission`` is only called
        # from the lineage helper's _producing_execution_task path.
        orig_read_submission = store.read_submission

        def _bad_read_submission(task_id: str) -> object:
            raise RuntimeError("transport blip")

        store.read_submission = _bad_read_submission  # type: ignore[method-assign]
        try:
            resp = client.get(f"/admin/variants/{variant_id}/")
            assert resp.status_code == 200
            assert "lineage may be incomplete" in resp.text
        finally:
            store.read_submission = orig_read_submission
