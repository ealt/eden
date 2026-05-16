"""Evaluator list preview tests (phase 12a-1c, wave 3)."""

from __future__ import annotations

from pathlib import Path

from conftest import seed_evaluate_task
from eden_storage import InMemoryStore
from fastapi.testclient import TestClient


def _signed_in(client: TestClient) -> TestClient:
    resp = client.post("/signin", follow_redirects=False)
    assert resp.status_code == 303
    return client


class TestEvaluatorListPreview:
    def test_row_count_matches_pending(
        self,
        client: TestClient,
        store: InMemoryStore,
    ) -> None:
        _signed_in(client)
        seed_evaluate_task(store, slug="alpha", variant_id="va")
        seed_evaluate_task(store, slug="beta", variant_id="vb")
        resp = client.get("/evaluator/")
        assert resp.status_code == 200
        assert "evaluate-alpha" in resp.text
        assert "evaluate-beta" in resp.text

    def test_preview_renders_variant_branch_and_commit_sha(
        self,
        client: TestClient,
        store: InMemoryStore,
    ) -> None:
        _signed_in(client)
        seed_evaluate_task(store, slug="alpha", variant_id="va")
        resp = client.get("/evaluator/")
        assert resp.status_code == 200
        assert "work/alpha-va" in resp.text
        # commit_sha is the default 40-byte "b" string; first 8 chars shown
        assert "bbbbbbbb" in resp.text

    def test_preview_renders_executed_by(
        self,
        client: TestClient,
        store: InMemoryStore,
    ) -> None:
        _signed_in(client)
        # seed_evaluate_task drives via worker_id="executor-w"
        seed_evaluate_task(store, slug="alpha", variant_id="va")
        resp = client.get("/evaluator/")
        assert resp.status_code == 200
        # The variant's evaluated_by is None at the evaluator-list
        # stage (the variant is in `starting`), but executed_by IS
        # set by the executor submit/accept.
        assert "executor-w" in resp.text

    def test_preview_inline_variant_artifact_when_file_uri(
        self,
        client: TestClient,
        store: InMemoryStore,
        artifacts_dir: Path,
    ) -> None:
        _signed_in(client)
        path = artifacts_dir / "variant-artifact.md"
        path.write_text("variant artifact body")
        seed_evaluate_task(
            store,
            slug="alpha",
            variant_id="va",
            variant_artifact_path=path,
        )
        resp = client.get("/evaluator/")
        assert resp.status_code == 200
        assert "variant artifact body" in resp.text

    def test_preview_variant_unavailable_when_read_variant_404s(
        self,
        client: TestClient,
        store: InMemoryStore,
    ) -> None:
        _signed_in(client)
        seed_evaluate_task(store, slug="alpha", variant_id="va")
        from eden_storage.errors import NotFound

        def _missing(_: str) -> object:
            raise NotFound("no such variant")

        store.read_variant = _missing  # type: ignore[method-assign]
        resp = client.get("/evaluator/")
        assert resp.status_code == 200
        assert "(variant unavailable)" in resp.text

    def test_preview_transport_error_renders_banner(
        self,
        client: TestClient,
        store: InMemoryStore,
    ) -> None:
        _signed_in(client)
        seed_evaluate_task(store, slug="alpha", variant_id="va")

        def _flaky(_: str) -> object:
            raise RuntimeError("transport blip")

        store.read_variant = _flaky  # type: ignore[method-assign]
        resp = client.get("/evaluator/")
        assert resp.status_code == 200
        assert "variant/idea read(s) failed" in resp.text
        assert "(read failed)" in resp.text

    def test_preview_renders_target_anyone_by_default(
        self,
        client: TestClient,
        store: InMemoryStore,
    ) -> None:
        _signed_in(client)
        seed_evaluate_task(store, slug="alpha", variant_id="va")
        resp = client.get("/evaluator/")
        assert resp.status_code == 200
        assert "anyone" in resp.text

    def test_admin_lineage_link_is_present(
        self,
        client: TestClient,
        store: InMemoryStore,
    ) -> None:
        _signed_in(client)
        eval_task_id, _, _ = seed_evaluate_task(
            store, slug="alpha", variant_id="va"
        )
        resp = client.get("/evaluator/")
        assert resp.status_code == 200
        assert f"/admin/tasks/{eval_task_id}/" in resp.text
