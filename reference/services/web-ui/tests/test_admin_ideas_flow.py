"""Cross-request flow tests for the admin ideas module (phase 12a-1c, wave 4)."""

from __future__ import annotations

from eden_storage import IdeaSubmission, InMemoryStore, VariantSubmission
from fastapi.testclient import TestClient

BASE_SHA = "a" * 40


def _signed_in(client: TestClient) -> TestClient:
    resp = client.post("/signin", follow_redirects=False)
    assert resp.status_code == 303
    return client


def _drive_pipeline(store: InMemoryStore) -> dict[str, str]:
    from eden_contracts import Idea, Variant

    store.create_ideation_task("plan-1")
    pclaim = store.claim("plan-1", "ideator-w")
    idea_id = "idea-alpha"
    store.create_idea(
        Idea(
            idea_id=idea_id,
            experiment_id=store.experiment_id,
            slug="alpha",
            priority=1.0,
            parent_commits=[BASE_SHA],
            artifacts_uri="https://example.invalid/x.md",
            state="drafting",
            created_at="2026-04-24T11:00:00Z",
            created_by="ideator-w",
        )
    )
    store.mark_idea_ready(idea_id)
    store.submit(
        "plan-1",
        pclaim.worker_id,
        IdeaSubmission(status="success", idea_ids=(idea_id,)),
    )
    store.accept("plan-1")

    store.create_execution_task("exec-1", idea_id)
    eclaim = store.claim("exec-1", "executor-w")
    store.create_variant(
        Variant(
            variant_id="v-1",
            experiment_id=store.experiment_id,
            idea_id=idea_id,
            status="starting",
            parent_commits=[BASE_SHA],
            branch="work/v-1",
            started_at="2026-04-24T12:00:00Z",
        )
    )
    store.submit(
        "exec-1",
        eclaim.worker_id,
        VariantSubmission(status="success", variant_id="v-1", commit_sha="b" * 40),
    )
    store.accept("exec-1")
    return {"ideation_task_id": "plan-1", "idea_id": idea_id, "variant_id": "v-1"}


class TestIdeasFlow:
    def test_index_then_detail_then_linked_variant(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        _signed_in(client)
        ids = _drive_pipeline(store)

        # 1. index lists the idea
        resp = client.get("/admin/ideas/")
        assert resp.status_code == 200
        assert ids["idea_id"] in resp.text

        # 2. detail renders + carries lineage to the ideation task + variants
        resp = client.get(f"/admin/ideas/{ids['idea_id']}/")
        assert resp.status_code == 200
        assert f"/admin/tasks/{ids['ideation_task_id']}/" in resp.text
        assert f"/admin/variants/{ids['variant_id']}/" in resp.text

        # 3. linked variant page renders 200
        resp = client.get(f"/admin/variants/{ids['variant_id']}/")
        assert resp.status_code == 200

    def test_lineage_reverse_walk_to_correct_ideation_task(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        """Two ideation tasks, only one produced this idea — pick that one."""
        _signed_in(client)
        from eden_contracts import Idea

        # First ideation task (unrelated)
        store.create_ideation_task("plan-A")
        a_claim = store.claim("plan-A", "ideator-w")
        store.create_idea(
            Idea(
                idea_id="idea-unrelated",
                experiment_id=store.experiment_id,
                slug="unrelated",
                priority=1.0,
                parent_commits=[BASE_SHA],
                artifacts_uri="https://example.invalid/u.md",
                state="drafting",
                created_at="2026-04-24T11:00:00Z",
            )
        )
        store.mark_idea_ready("idea-unrelated")
        store.submit(
            "plan-A",
            a_claim.worker_id,
            IdeaSubmission(status="success", idea_ids=("idea-unrelated",)),
        )

        # Second ideation task — the one we want to find
        store.create_ideation_task("plan-B")
        b_claim = store.claim("plan-B", "ideator-w")
        store.create_idea(
            Idea(
                idea_id="idea-target",
                experiment_id=store.experiment_id,
                slug="target",
                priority=1.0,
                parent_commits=[BASE_SHA],
                artifacts_uri="https://example.invalid/t.md",
                state="drafting",
                created_at="2026-04-24T11:00:00Z",
            )
        )
        store.mark_idea_ready("idea-target")
        store.submit(
            "plan-B",
            b_claim.worker_id,
            IdeaSubmission(status="success", idea_ids=("idea-target",)),
        )

        resp = client.get("/admin/ideas/idea-target/")
        assert resp.status_code == 200
        assert "/admin/tasks/plan-B/" in resp.text
        assert "/admin/tasks/plan-A/" not in resp.text

    def test_idea_with_pending_ideation_task_renders_in_progress(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        """An idea created out-of-band (no ideation submission) shows
        the lineage placeholder."""
        _signed_in(client)
        from eden_contracts import Idea

        store.create_idea(
            Idea(
                idea_id="idea-orphan",
                experiment_id=store.experiment_id,
                slug="orphan",
                priority=1.0,
                parent_commits=[BASE_SHA],
                artifacts_uri="https://example.invalid/o.md",
                state="drafting",
                created_at="2026-04-24T11:00:00Z",
            )
        )
        resp = client.get("/admin/ideas/idea-orphan/")
        assert resp.status_code == 200
        assert "(ideation task:" in resp.text
