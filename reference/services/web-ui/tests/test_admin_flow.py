"""Cross-request flow tests for the admin module (chunk 9e).

Exercises end-to-end interactions that cross module boundaries —
e.g. claim an ideator task, observe it on the admin dashboard,
operator-reclaim it from admin, verify the ideator side notices.
"""

from __future__ import annotations

from pathlib import Path

from conftest import get_csrf, seed_evaluate_task
from eden_storage import InMemoryStore
from fastapi.testclient import TestClient


def _seed_ideate_task(store: InMemoryStore, task_id: str = "ideate-A") -> str:
    store.create_ideate_task(task_id)
    return task_id


class TestAdminReclaimIdeatorClaim:
    def test_claim_then_admin_reclaim_round_trip(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        task_id = _seed_ideate_task(store, "ideate-A")
        # Claim via the ideator module.
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            f"/ideator/{task_id}/claim",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert store.read_task(task_id).state == "claimed"

        # Admin task list shows the claim.
        resp = signed_in_client.get("/admin/tasks/?state=claimed")
        assert task_id in resp.text

        # Operator reclaim from admin.
        resp = signed_in_client.post(
            f"/admin/tasks/{task_id}/reclaim",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "?reclaimed=ok" in resp.headers["location"]

        # Task is back to pending; ideator-side claim broken.
        assert store.read_task(task_id).state == "pending"
        # The events page reflects the reclaim with cause=operator.
        events = store.replay()
        reclaim_events = [e for e in events if e.type == "task.reclaimed"]
        assert len(reclaim_events) == 1
        assert reclaim_events[0].data["cause"] == "operator"


class TestAdminVariantDetailFromEvaluatorFlow:
    def test_evaluate_seeded_variant_renders_with_idea_context(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        artifacts_dir: Path,
    ) -> None:
        seed_evaluate_task(
            store, slug="flow", variant_id="variant-F", artifacts_dir=artifacts_dir
        )
        resp = signed_in_client.get("/admin/variants/variant-F/")
        assert resp.status_code == 200
        assert "variant-F" in resp.text
        assert "idea-flow" in resp.text
        # Execute task events for the parent idea should be in the
        # related-events table.
        assert "task.created" in resp.text


class TestAdminEventsReflectsAllRoles:
    def test_events_view_shows_lifecycle_progression(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        artifacts_dir: Path,
    ) -> None:
        seed_evaluate_task(
            store, slug="x", variant_id="variant-LX", artifacts_dir=artifacts_dir
        )
        resp = signed_in_client.get("/admin/events/")
        assert resp.status_code == 200
        # Spot-check that several event types from the seed flow show
        # up in the rendered log.
        for event_type in (
            "idea.drafted",
            "idea.ready",
            "task.created",
            "task.claimed",
            "task.submitted",
            "variant.started",
        ):
            assert event_type in resp.text, f"missing {event_type}"
