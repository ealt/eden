"""Lineage + attribution rendering tests for admin task detail (phase 12a-1c)."""
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

from conftest import seed_evaluate_task
from eden_storage import IdeaSubmission, InMemoryStore, VariantSubmission
from fastapi.testclient import TestClient


def _signed_in(client: TestClient) -> TestClient:
    resp = client.post("/signin", follow_redirects=False)
    assert resp.status_code == 303
    return client


def _drive_full_pipeline(
    store: InMemoryStore,
) -> dict[str, str]:
    """Drive an ideation → idea → execution → variant → evaluation pipeline.

    Returns a dict of {ideation_task_id, idea_id, exec_task_id, variant_id,
    eval_task_id} for assertion convenience.
    """
    ideation_task_id = "plan-1"
    store.create_ideation_task(ideation_task_id)
    pclaim = store.claim(ideation_task_id, store._test_worker_ids["ideator-w"])

    # Seed idea + drive to ready, attribute to ideator-w
    from eden_contracts import Idea

    idea_id = "idea-alpha"
    store.create_idea(
        Idea(
            idea_id=idea_id,
            experiment_id=store.experiment_id,
            slug="alpha",
            priority=1.0,
            parent_commits=["a" * 40],
            artifacts_uri="https://example.invalid/a.md",
            state="drafting",
            created_at="2026-04-24T11:00:00Z",
            created_by=store._test_worker_ids["ideator-w"],
        )
    )
    store.mark_idea_ready(idea_id)
    store.submit(
        ideation_task_id,
        pclaim.worker_id,
        IdeaSubmission(status="success", idea_ids=(idea_id,)),
    )
    store.accept(ideation_task_id)

    # Execution
    from eden_contracts import Variant

    exec_task_id = "exec-1"
    store.create_execution_task(exec_task_id, idea_id)
    eclaim = store.claim(exec_task_id, store._test_worker_ids["executor-w"])
    variant_id = "v-1"
    store.create_variant(
        Variant(
            variant_id=variant_id,
            experiment_id=store.experiment_id,
            idea_id=idea_id,
            status="starting",
            parent_commits=["a" * 40],
            branch="work/v-1",
            started_at="2026-04-24T12:00:00Z",
            executed_by=store._test_worker_ids["executor-w"],
        )
    )
    store.submit(
        exec_task_id,
        eclaim.worker_id,
        VariantSubmission(
            status="success", variant_id=variant_id, commit_sha="b" * 40
        ),
    )
    store.accept(exec_task_id)

    # Evaluation
    eval_task_id = "eval-1"
    store.create_evaluation_task(eval_task_id, variant_id)
    return {
        "ideation_task_id": ideation_task_id,
        "idea_id": idea_id,
        "exec_task_id": exec_task_id,
        "variant_id": variant_id,
        "eval_task_id": eval_task_id,
    }


class TestTaskAttribution:
    def test_attribution_section_renders_target_anyone(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        _signed_in(client)
        store.create_ideation_task("plan-A")

        resp = client.get("/admin/tasks/plan-A/")
        assert resp.status_code == 200
        body = resp.text
        assert "attribution" in body
        assert "anyone" in body

    def test_attribution_section_renders_target_worker(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        _signed_in(client)
        store.create_ideation_task("plan-A")
        # Reassign to a worker target — the registry contains "ideator-w".
        store.reassign_task(
            "plan-A",
            None,
            reason="setup",
            reassigned_by="admin",
        )
        # Now target it
        from eden_contracts import TaskTarget

        store.reassign_task(
            "plan-A",
            TaskTarget(kind="worker", id=store._test_worker_ids["ideator-w"]),
            reason="setup",
            reassigned_by="admin",
        )

        resp = client.get("/admin/tasks/plan-A/")
        assert resp.status_code == 200
        body = resp.text
        assert "worker:" in body
        assert "ideator-w" in body

    def test_attribution_renders_created_by_link(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        _signed_in(client)
        ids = _drive_full_pipeline(store)
        resp = client.get(f"/admin/tasks/{ids['ideation_task_id']}/")
        assert resp.status_code == 200
        body = resp.text
        # submitted_by is set via the submit flow; rendered as name(id)
        # with the link keyed on the minted opaque id.
        assert "ideator-w" in body
        assert f'/admin/workers/{store._test_worker_ids["ideator-w"]}/' in body

    def test_attribution_renders_claim_worker_id(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        """Plan §D.6: the attribution section surfaces target /
        created_by / submitted_by / claim.worker_id. The claim section
        also shows the worker at the top, but the attribution
        section's claim.worker_id row is the canonical operator-
        attribution surface."""
        _signed_in(client)
        store.create_ideation_task("plan-claim-A")
        worker, _ = store.register_worker("ideator-claim")
        store.claim("plan-claim-A", worker.worker_id)
        resp = client.get("/admin/tasks/plan-claim-A/")
        assert resp.status_code == 200
        body = resp.text
        attribution_block = body.split("attribution", 1)[1].split("payload", 1)[0]
        assert "claim.worker_id" in attribution_block
        # Rendered as name(id): both the name and the minted id appear.
        assert "ideator-claim" in attribution_block
        assert f"/admin/workers/{worker.worker_id}/" in attribution_block

    def test_attribution_claim_worker_id_em_dash_when_unclaimed(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        """Unclaimed task renders em dash in the claim.worker_id row,
        not a broken link."""
        _signed_in(client)
        store.create_ideation_task("plan-unclaimed-A")
        resp = client.get("/admin/tasks/plan-unclaimed-A/")
        assert resp.status_code == 200
        body = resp.text
        attribution_block = body.split("attribution", 1)[1].split("payload", 1)[0]
        assert "claim.worker_id" in attribution_block
        # The em-dash placeholder; no link.
        assert "—" in attribution_block


class TestIdeationTaskLineage:
    def test_pending_ideation_task_shows_no_ideas(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        _signed_in(client)
        store.create_ideation_task("plan-A")
        resp = client.get("/admin/tasks/plan-A/")
        assert resp.status_code == 200
        body = resp.text
        assert "lineage" in body
        assert "no ideas produced yet" in body

    def test_submitted_ideation_task_lists_ideas(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        _signed_in(client)
        ids = _drive_full_pipeline(store)
        resp = client.get(f"/admin/tasks/{ids['ideation_task_id']}/")
        assert resp.status_code == 200
        body = resp.text
        assert f'/admin/ideas/{ids["idea_id"]}/' in body
        assert "slug=alpha" in body


class TestExecutionTaskLineage:
    def test_execution_task_back_to_idea_forward_to_variant(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        _signed_in(client)
        ids = _drive_full_pipeline(store)
        resp = client.get(f"/admin/tasks/{ids['exec_task_id']}/")
        assert resp.status_code == 200
        body = resp.text
        assert f'/admin/ideas/{ids["idea_id"]}/' in body
        assert f'/admin/variants/{ids["variant_id"]}/' in body

    def test_execution_task_unknown_idea_shows_placeholder(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        _signed_in(client)
        # Seed via the test fixture's seed_evaluate_task helper (which
        # leaves an execution task in `completed`), then delete the
        # underlying idea by monkeypatching read_idea.
        eval_task_id, variant_id, idea_id = seed_evaluate_task(store)
        from eden_storage.errors import NotFound

        orig_read_idea = store.read_idea

        def _read_idea(target: str) -> object:
            if target == idea_id:
                raise NotFound(f"idea {target!r}")
            return orig_read_idea(target)

        store.read_idea = _read_idea  # type: ignore[method-assign]
        # The execution task seeded by seed_evaluate_task is "execute-demo".
        resp = client.get("/admin/tasks/execute-demo/")
        assert resp.status_code == 200
        body = resp.text
        assert "(idea unknown)" in body


class TestEvaluationTaskLineage:
    def test_evaluation_task_back_to_variant(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        _signed_in(client)
        ids = _drive_full_pipeline(store)
        resp = client.get(f"/admin/tasks/{ids['eval_task_id']}/")
        assert resp.status_code == 200
        body = resp.text
        assert f'/admin/variants/{ids["variant_id"]}/' in body

    def test_evaluation_task_missing_variant_shows_placeholder(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        _signed_in(client)
        ids = _drive_full_pipeline(store)
        from eden_storage.errors import NotFound

        orig_read_variant = store.read_variant

        def _read_variant(target: str) -> object:
            if target == ids["variant_id"]:
                raise NotFound(f"variant {target!r}")
            return orig_read_variant(target)

        store.read_variant = _read_variant  # type: ignore[method-assign]
        resp = client.get(f"/admin/tasks/{ids['eval_task_id']}/")
        assert resp.status_code == 200
        body = resp.text
        assert "(variant unknown)" in body


class TestLineageTransportErrors:
    def test_transport_error_renders_banner_without_crash(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        _signed_in(client)
        ids = _drive_full_pipeline(store)
        # Cause a transport-shaped failure on the forward variants
        # walk.
        orig_list_variants = store.list_variants

        def _bad_list_variants(*args: object, **kwargs: object) -> object:
            raise RuntimeError("transport blip")

        store.list_variants = _bad_list_variants  # type: ignore[method-assign]
        try:
            resp = client.get(f"/admin/tasks/{ids['exec_task_id']}/")
            assert resp.status_code == 200
            assert "lineage may be incomplete" in resp.text
        finally:
            store.list_variants = orig_list_variants

    def test_transport_error_distinguishes_slot_message(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        """Plan §D.6: when a lineage read fails, the slot renders
        ``(... — read error)`` AND the section-scoped banner fires.
        Missing-but-no-transport-error renders plain ``(... unknown)``
        WITHOUT the banner."""
        _signed_in(client)
        ids = _drive_full_pipeline(store)
        # Force a transport-shaped failure on read_idea so the
        # execution-task's upstream "idea" slot bumps transport_errors
        # without the helper returning a link.
        orig_read_idea = store.read_idea

        def _flaky(_: str) -> object:
            raise RuntimeError("transport blip")

        store.read_idea = _flaky  # type: ignore[method-assign]
        try:
            resp = client.get(f"/admin/tasks/{ids['exec_task_id']}/")
            assert resp.status_code == 200
            assert "(idea unknown — read error)" in resp.text
        finally:
            store.read_idea = orig_read_idea
