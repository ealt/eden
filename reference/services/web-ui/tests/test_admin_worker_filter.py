"""Tests for the ``?worker=`` filter extension on /admin/tasks/ and /admin/variants/.

Per plan §D.8 the existing admin module gains a worker filter so
the worker-detail page's "see all" links resolve correctly. The
filter is post-fetch client-side Python (no schema change).
"""

from __future__ import annotations

from pathlib import Path

from conftest import (
    WEB_UI_WORKER_NAME,
    seed_evaluate_task,
)
from eden_storage import EvaluationSubmission, InMemoryStore
from fastapi.testclient import TestClient


def _seed_claimed_task(store: InMemoryStore, task_id: str, worker_id: str) -> None:
    store.create_ideation_task(task_id)
    store.claim(task_id, worker_id)


class TestAdminTasksWorkerFilter:
    def test_filter_by_claim_worker(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        worker_ids: dict[str, str],
    ) -> None:
        wid = worker_ids[WEB_UI_WORKER_NAME]
        _seed_claimed_task(store, "task-eric", wid)
        _seed_claimed_task(store, "task-other", worker_ids["ui-w-other"])
        resp = signed_in_client.get(f"/admin/tasks/?worker={wid}")
        assert resp.status_code == 200
        assert "task-eric" in resp.text
        assert "task-other" not in resp.text

    def test_filter_by_submitted_by(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        worker_ids: dict[str, str],
        artifacts_dir: Path,
    ) -> None:
        # seed_evaluate_task drives the executor flow and writes
        # submitted_by on the execution task.
        seed_evaluate_task(
            store, slug="filter-test", artifacts_dir=artifacts_dir
        )
        resp = signed_in_client.get(
            f"/admin/tasks/?worker={worker_ids['executor-w']}"
        )
        assert resp.status_code == 200
        assert "execute-filter-test" in resp.text

    def test_invalid_worker_filter_renders_empty(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        worker_ids: dict[str, str],
    ) -> None:
        _seed_claimed_task(store, "task-x", worker_ids[WEB_UI_WORKER_NAME])
        # Kebab/uppercase fails the opaque ``wkr_*`` grammar → invalid.
        resp = signed_in_client.get("/admin/tasks/?worker=UPPER")
        assert resp.status_code == 200
        # Empty rowset (chunk-9e _INVALID_FILTER discipline)
        assert "task-x" not in resp.text

    def test_filter_composes_with_kind(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        worker_ids: dict[str, str],
    ) -> None:
        wid = worker_ids[WEB_UI_WORKER_NAME]
        _seed_claimed_task(store, "ideation-w", wid)
        # Filter by both: only kind=ideation matches, and our task
        # is the claimant.
        resp = signed_in_client.get(
            f"/admin/tasks/?worker={wid}&kind=ideation"
        )
        assert "ideation-w" in resp.text
        # Filter by kind that doesn't match
        resp = signed_in_client.get(
            f"/admin/tasks/?worker={wid}&kind=execution"
        )
        assert "ideation-w" not in resp.text

    def test_clear_filter_link_present(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        worker_ids: dict[str, str],
    ) -> None:
        wid = worker_ids[WEB_UI_WORKER_NAME]
        _seed_claimed_task(store, "task-clear", wid)
        resp = signed_in_client.get(f"/admin/tasks/?worker={wid}")
        assert "clear worker filter" in resp.text


class TestAdminVariantsWorkerFilter:
    def test_filter_by_executor(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        worker_ids: dict[str, str],
        artifacts_dir: Path,
    ) -> None:
        seed_evaluate_task(
            store, slug="var-filter", artifacts_dir=artifacts_dir
        )
        # seed_evaluate_task uses "executor-w" as the executor worker.
        resp = signed_in_client.get(
            f"/admin/variants/?worker={worker_ids['executor-w']}"
        )
        assert resp.status_code == 200
        assert "variant-eval" in resp.text

    def test_filter_by_evaluator(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        worker_ids: dict[str, str],
        artifacts_dir: Path,
    ) -> None:
        eval_task_id, variant_id, _ = seed_evaluate_task(
            store, slug="var-eval-filter", artifacts_dir=artifacts_dir,
            variant_id="variant-evaled",
        )
        # Drive the evaluation flow to write evaluated_by.
        eval_claim = store.claim(eval_task_id, worker_ids["evaluator-w"])
        store.submit(
            eval_task_id,
            eval_claim.worker_id,
            EvaluationSubmission(
                status="success",
                variant_id=variant_id,
                evaluation={"score": 0.9},
            ),
        )
        store.accept(eval_task_id)
        resp = signed_in_client.get(
            f"/admin/variants/?worker={worker_ids['evaluator-w']}"
        )
        assert resp.status_code == 200
        assert "variant-evaled" in resp.text

    def test_invalid_worker_renders_empty(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        artifacts_dir: Path,
    ) -> None:
        seed_evaluate_task(
            store, slug="var-bad", artifacts_dir=artifacts_dir
        )
        resp = signed_in_client.get("/admin/variants/?worker=UPPER")
        assert resp.status_code == 200
        assert "variant-eval" not in resp.text


class TestAdminLandingPage:
    def test_landing_shows_worker_group_section(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        store.register_group("team-landing")
        resp = signed_in_client.get("/admin/")
        assert resp.status_code == 200
        # Section header — chunk 12a-1c renamed to include the ideas module.
        assert "workers" in resp.text
        assert "groups" in resp.text
        # The link targets
        assert 'href="/admin/workers/"' in resp.text
        assert 'href="/admin/groups/"' in resp.text

    def test_landing_admin_disabled_banner(
        self, signed_in_client_no_admin: TestClient
    ) -> None:
        resp = signed_in_client_no_admin.get("/admin/")
        assert resp.status_code == 200
        assert "admin token not configured" in resp.text
