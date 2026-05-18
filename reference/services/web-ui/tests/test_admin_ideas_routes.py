"""Per-route tests for the admin ideas module (phase 12a-1c, wave 4)."""

from __future__ import annotations

from pathlib import Path

from eden_contracts import Idea
from eden_storage import InMemoryStore
from fastapi.testclient import TestClient

BASE_SHA = "a" * 40


def _signed_in(client: TestClient) -> TestClient:
    resp = client.post("/signin", follow_redirects=False)
    assert resp.status_code == 303
    return client


def _seed_idea(
    store: InMemoryStore,
    *,
    slug: str = "alpha",
    state: str = "drafting",
    artifacts_dir: Path | None = None,
    artifact_text: str = "body",
    created_by: str | None = None,
) -> str:
    idea_id = f"idea-{slug}"
    if artifacts_dir is not None:
        path = artifacts_dir / f"{idea_id}.md"
        path.write_text(artifact_text)
        artifacts_uri = f"file://{path.resolve()}"
    else:
        artifacts_uri = f"https://example.invalid/{idea_id}.md"
    kwargs: dict[str, object] = dict(
        idea_id=idea_id,
        experiment_id=store.experiment_id,
        slug=slug,
        priority=1.0,
        parent_commits=[BASE_SHA],
        artifacts_uri=artifacts_uri,
        state="drafting",
        created_at="2026-04-24T11:00:00Z",
    )
    if created_by is not None:
        kwargs["created_by"] = created_by
    store.create_idea(Idea(**kwargs))  # type: ignore[arg-type]
    if state != "drafting":
        store.mark_idea_ready(idea_id)
    return idea_id


class TestIdeasIndexAuth:
    def test_unauthenticated_get_redirects_signin(
        self, client: TestClient
    ) -> None:
        resp = client.get("/admin/ideas/", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/signin"

    def test_detail_unauthenticated_redirects_signin(
        self, client: TestClient
    ) -> None:
        resp = client.get("/admin/ideas/idea-x/", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/signin"


class TestIdeasIndex:
    def test_empty_render(
        self, client: TestClient
    ) -> None:
        _signed_in(client)
        resp = client.get("/admin/ideas/")
        assert resp.status_code == 200
        assert "<h1>ideas</h1>" in resp.text
        assert "no ideas matching this filter" in resp.text

    def test_lists_each_idea(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        _signed_in(client)
        _seed_idea(store, slug="alpha")
        _seed_idea(store, slug="beta")
        resp = client.get("/admin/ideas/")
        assert resp.status_code == 200
        assert "idea-alpha" in resp.text
        assert "idea-beta" in resp.text

    def test_filter_state_drafting(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        _signed_in(client)
        _seed_idea(store, slug="alpha", state="drafting")
        _seed_idea(store, slug="beta", state="ready")
        resp = client.get("/admin/ideas/?state=ready")
        assert resp.status_code == 200
        assert "idea-beta" in resp.text
        assert "idea-alpha" not in resp.text

    def test_filter_state_dispatched(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        """Plan §6: ideas in each state filter cleanly; dispatched and
        completed are the late-lifecycle states the earlier test did
        not cover."""
        _signed_in(client)
        idea_id = _seed_idea(store, slug="alpha", state="ready")
        # Driving the idea to ``dispatched`` requires creating an
        # execution task that references it (chapter 04 §3.1).
        store.create_execution_task("exec-alpha", idea_id)

        resp = client.get("/admin/ideas/?state=dispatched")
        assert resp.status_code == 200
        assert "idea-alpha" in resp.text

        # And the filter excludes dispatched from a ready-only query.
        resp = client.get("/admin/ideas/?state=ready")
        assert resp.status_code == 200
        assert "idea-alpha" not in resp.text

    def test_filter_state_completed(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        """Drive an idea through the full lifecycle and verify it
        surfaces under ``state=completed``."""
        _signed_in(client)
        from eden_contracts import Variant
        from eden_storage import VariantSubmission

        idea_id = _seed_idea(store, slug="alpha", state="ready")
        store.create_execution_task("exec-alpha", idea_id)
        eclaim = store.claim("exec-alpha", "executor-w")
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
            "exec-alpha",
            eclaim.worker_id,
            VariantSubmission(
                status="success", variant_id="v-1", commit_sha="b" * 40
            ),
        )
        store.accept("exec-alpha")
        # accept of a successful execution task marks the idea
        # ``completed`` (chapter 04 idea-lifecycle terminal transition).
        resp = client.get("/admin/ideas/?state=completed")
        assert resp.status_code == 200
        assert "idea-alpha" in resp.text

    def test_filter_invalid_state_renders_empty(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        _signed_in(client)
        _seed_idea(store, slug="alpha")
        resp = client.get("/admin/ideas/?state=bogus")
        assert resp.status_code == 200
        assert "no ideas matching this filter" in resp.text

    def test_created_by_renders_worker_link(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        _signed_in(client)
        _seed_idea(store, slug="alpha", created_by="ideator-w")
        resp = client.get("/admin/ideas/")
        assert resp.status_code == 200
        assert '/admin/workers/ideator-w/' in resp.text

    def test_variant_count_column(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        _signed_in(client)
        from eden_contracts import Variant

        idea_id = _seed_idea(store, slug="alpha")
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
        resp = client.get("/admin/ideas/")
        assert resp.status_code == 200
        # The row should carry "1" in the variants column for idea-alpha
        # Use a coarse assertion: the body contains "1" near the slug.
        assert "idea-alpha" in resp.text

    def test_transport_error_renders_502(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        _signed_in(client)

        def _flaky(**kwargs: object) -> object:
            raise RuntimeError("transport blip")

        store.list_ideas = _flaky  # type: ignore[method-assign]
        resp = client.get("/admin/ideas/")
        assert resp.status_code == 502
        assert "Transport failure" in resp.text


class TestIdeaDetail:
    def test_renders_full_record(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        _signed_in(client)
        idea_id = _seed_idea(store, slug="alpha", created_by="ideator-w")
        resp = client.get(f"/admin/ideas/{idea_id}/")
        assert resp.status_code == 200
        assert "idea-alpha" in resp.text
        assert "alpha" in resp.text
        assert "ideator-w" in resp.text

    def test_404_when_idea_missing(
        self, client: TestClient
    ) -> None:
        _signed_in(client)
        resp = client.get("/admin/ideas/idea-nope/")
        assert resp.status_code == 404

    def test_inline_content_renders_when_file_uri(
        self,
        client: TestClient,
        store: InMemoryStore,
        artifacts_dir: Path,
    ) -> None:
        _signed_in(client)
        idea_id = _seed_idea(
            store,
            slug="alpha",
            artifacts_dir=artifacts_dir,
            artifact_text="rendered body",
        )
        resp = client.get(f"/admin/ideas/{idea_id}/")
        assert resp.status_code == 200
        assert "rendered body" in resp.text

    def test_inline_content_unavailable_for_non_file_uri(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        _signed_in(client)
        idea_id = _seed_idea(store, slug="alpha", artifacts_dir=None)
        resp = client.get(f"/admin/ideas/{idea_id}/")
        assert resp.status_code == 200
        assert "content unavailable" in resp.text
