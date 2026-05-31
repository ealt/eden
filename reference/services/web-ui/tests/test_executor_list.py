"""Executor pending-task list rendering (issue #137 redesign).

Covers the 5-column table, priority-default sort + slug sort, the
eligibility / target / group filters, the eligibility-aware claim
button, the click-to-expand context row, and the two warning banners.
Supersedes the pre-redesign inline-preview tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import seed_implement_task
from eden_contracts import TaskTarget
from eden_storage import InMemoryStore
from fastapi.testclient import TestClient

BASE_SHA = "a" * 40


def _signed_in(client: TestClient) -> TestClient:
    resp = client.post("/signin", follow_redirects=False)
    assert resp.status_code == 303
    return client


@pytest.fixture(autouse=True)
def _isolate_claims():
    from eden_web_ui.routes import executor as executor_routes

    executor_routes._CLAIMS.clear()
    yield
    executor_routes._CLAIMS.clear()


class TestColumns:
    def test_renders_slug_priority_target_created_by(
        self,
        exec_client: TestClient,
        store: InMemoryStore,
        artifacts_dir: Path,
    ) -> None:
        client = _signed_in(exec_client)
        seed_implement_task(
            store,
            slug="alpha",
            base_sha=BASE_SHA,
            artifacts_dir=artifacts_dir,
            priority=3.0,
            created_by="ideator-1",
        )
        resp = client.get("/executor/")
        assert resp.status_code == 200
        body = resp.text
        assert "alpha" in body
        assert "3.0" in body
        assert "ideator-1" in body
        assert "priority" in body
        assert "created by" in body

    def test_inline_preview_is_gone(
        self,
        exec_client: TestClient,
        store: InMemoryStore,
        artifacts_dir: Path,
    ) -> None:
        client = _signed_in(exec_client)
        seed_implement_task(
            store,
            slug="alpha",
            base_sha=BASE_SHA,
            artifacts_dir=artifacts_dir,
            artifact_text="hello idea body",
        )
        resp = client.get("/executor/")
        assert resp.status_code == 200
        # No inline content preview any more — the expansion is
        # navigation-only.
        assert "hello idea body" not in resp.text
        assert "<summary>preview</summary>" not in resp.text

    def test_admin_lineage_link_present(
        self,
        exec_client: TestClient,
        store: InMemoryStore,
        artifacts_dir: Path,
    ) -> None:
        client = _signed_in(exec_client)
        task_id, _ = seed_implement_task(
            store, slug="alpha", base_sha=BASE_SHA, artifacts_dir=artifacts_dir
        )
        resp = client.get("/executor/")
        assert f"/admin/tasks/{task_id}/" in resp.text


class TestSort:
    def test_default_sort_is_priority_desc(
        self, exec_client: TestClient, store: InMemoryStore
    ) -> None:
        client = _signed_in(exec_client)
        seed_implement_task(store, slug="low", base_sha=BASE_SHA, priority=1.0)
        seed_implement_task(store, slug="high", base_sha=BASE_SHA, priority=9.0)
        resp = client.get("/executor/")
        body = resp.text
        assert body.index("high") < body.index("low")

    def test_slug_sort_ascending(
        self, exec_client: TestClient, store: InMemoryStore
    ) -> None:
        client = _signed_in(exec_client)
        seed_implement_task(store, slug="zeta", base_sha=BASE_SHA, priority=9.0)
        seed_implement_task(store, slug="alpha", base_sha=BASE_SHA, priority=1.0)
        resp = client.get("/executor/?sort=slug&dir=asc")
        body = resp.text
        assert body.index(">alpha<") < body.index(">zeta<")

    def test_sort_header_links_present_and_flip(
        self, exec_client: TestClient, store: InMemoryStore
    ) -> None:
        client = _signed_in(exec_client)
        seed_implement_task(store, slug="alpha", base_sha=BASE_SHA)
        resp = client.get("/executor/")
        # Default is priority desc; the priority header link flips to asc.
        assert "sort=priority&amp;dir=asc" in resp.text
        # The slug header link defaults to ascending.
        assert "sort=slug&amp;dir=asc" in resp.text

    def test_unknown_sort_param_falls_back_to_default(
        self, exec_client: TestClient, store: InMemoryStore
    ) -> None:
        client = _signed_in(exec_client)
        seed_implement_task(store, slug="low", base_sha=BASE_SHA, priority=1.0)
        seed_implement_task(store, slug="high", base_sha=BASE_SHA, priority=9.0)
        # An attacker-supplied sort value must not reflect into hrefs and
        # must fall back to the priority-desc default.
        resp = client.get("/executor/?sort=<script>&dir=evil")
        body = resp.text
        assert "<script>" not in body
        assert body.index("high") < body.index("low")


class TestEligibilityFilter:
    def test_untargeted_is_eligible_with_claim_form(
        self, exec_client: TestClient, store: InMemoryStore
    ) -> None:
        client = _signed_in(exec_client)
        task_id, _ = seed_implement_task(store, slug="alpha", base_sha=BASE_SHA)
        resp = client.get("/executor/")
        assert f'action="/executor/{task_id}/claim"' in resp.text

    def test_ineligible_hidden_by_default_filter(
        self, exec_client: TestClient, store: InMemoryStore
    ) -> None:
        client = _signed_in(exec_client)
        seed_implement_task(
            store,
            slug="alpha",
            base_sha=BASE_SHA,
            target=TaskTarget(kind="worker", id="other-w"),
        )
        resp = client.get("/executor/")
        # Default eligible=1 hides the worker-targeted-at-someone-else row.
        assert "alpha" not in resp.text
        assert "no pending execution tasks eligible" in resp.text

    def test_ineligible_shown_disabled_when_filter_off(
        self, exec_client: TestClient, store: InMemoryStore
    ) -> None:
        client = _signed_in(exec_client)
        task_id, _ = seed_implement_task(
            store,
            slug="alpha",
            base_sha=BASE_SHA,
            target=TaskTarget(kind="worker", id="other-w"),
        )
        resp = client.get("/executor/?eligible=0")
        body = resp.text
        assert "alpha" in body
        assert "disabled" in body
        assert "you are not in its target" in body
        # No claim form for the ineligible task.
        assert f'action="/executor/{task_id}/claim"' not in body

    def test_group_target_eligible_when_member(
        self, exec_client: TestClient, store: InMemoryStore
    ) -> None:
        client = _signed_in(exec_client)
        # ui-w (the session worker) is a member of the seeded "admins" group.
        task_id, _ = seed_implement_task(
            store,
            slug="alpha",
            base_sha=BASE_SHA,
            target=TaskTarget(kind="group", id="admins"),
        )
        resp = client.get("/executor/")
        assert f'action="/executor/{task_id}/claim"' in resp.text


class TestTargetFilter:
    def test_targeted_only(
        self, exec_client: TestClient, store: InMemoryStore
    ) -> None:
        client = _signed_in(exec_client)
        seed_implement_task(store, slug="free", base_sha=BASE_SHA)
        seed_implement_task(
            store,
            slug="bound",
            base_sha=BASE_SHA,
            target=TaskTarget(kind="group", id="admins"),
        )
        resp = client.get("/executor/?target=targeted")
        body = resp.text
        assert "bound" in body
        assert "free" not in body

    def test_untargeted_only(
        self, exec_client: TestClient, store: InMemoryStore
    ) -> None:
        client = _signed_in(exec_client)
        seed_implement_task(store, slug="free", base_sha=BASE_SHA)
        seed_implement_task(
            store,
            slug="bound",
            base_sha=BASE_SHA,
            target=TaskTarget(kind="group", id="admins"),
        )
        resp = client.get("/executor/?target=untargeted")
        body = resp.text
        assert "free" in body
        assert "bound" not in body


class TestGroupByCreator:
    def test_group_toggle_wraps_rows_by_creator(
        self, exec_client: TestClient, store: InMemoryStore
    ) -> None:
        client = _signed_in(exec_client)
        seed_implement_task(
            store, slug="a1", base_sha=BASE_SHA, created_by="ideator-1"
        )
        seed_implement_task(
            store, slug="b1", base_sha=BASE_SHA, created_by="ideator-w"
        )
        resp = client.get("/executor/?group=1")
        body = resp.text
        assert 'class="creator-group"' in body
        assert "ideator-1" in body
        assert "ideator-w" in body


class TestExpansion:
    def test_expansion_has_context_links(
        self,
        exec_client: TestClient,
        store: InMemoryStore,
        artifacts_dir: Path,
    ) -> None:
        client = _signed_in(exec_client)
        task_id, idea_id = seed_implement_task(
            store,
            slug="alpha",
            base_sha=BASE_SHA,
            artifacts_dir=artifacts_dir,
            created_by="ideator-1",
        )
        resp = client.get("/executor/")
        body = resp.text
        assert "context links" in body
        assert f"/admin/ideas/{idea_id}/" in body
        assert "/admin/workers/ideator-1/" in body
        # File artifact → a "view content" link into /artifacts.
        assert "/artifacts?uri=" in body


class TestDegradedRows:
    def test_idea_unavailable_renders_degraded(
        self, exec_client: TestClient, store: InMemoryStore
    ) -> None:
        client = _signed_in(exec_client)
        seed_implement_task(store, slug="alpha", base_sha=BASE_SHA)
        from eden_storage.errors import NotFound

        def _missing(_: str) -> object:
            raise NotFound("no such idea")

        store.read_idea = _missing  # type: ignore[method-assign]
        resp = client.get("/executor/?eligible=0")
        assert "(idea unavailable)" in resp.text

    def test_transport_error_increments_read_banner(
        self, exec_client: TestClient, store: InMemoryStore
    ) -> None:
        client = _signed_in(exec_client)
        seed_implement_task(store, slug="alpha", base_sha=BASE_SHA)

        def _flaky(_: str) -> object:
            raise RuntimeError("transport blip")

        store.read_idea = _flaky  # type: ignore[method-assign]
        resp = client.get("/executor/?eligible=0")
        assert "idea read(s) failed" in resp.text
        assert "(read failed)" in resp.text

    def test_degraded_rows_sink_to_bottom_both_directions(
        self, exec_client: TestClient, store: InMemoryStore
    ) -> None:
        client = _signed_in(exec_client)
        seed_implement_task(
            store, slug="present", base_sha=BASE_SHA, priority=5.0
        )
        seed_implement_task(store, slug="gone", base_sha=BASE_SHA, priority=9.0)
        from eden_storage.errors import NotFound

        orig = store.read_idea

        def _selective(idea_id: str) -> object:
            if idea_id == "idea-gone":
                raise NotFound("gone")
            return orig(idea_id)

        store.read_idea = _selective  # type: ignore[method-assign]
        for direction in ("asc", "desc"):
            resp = client.get(
                f"/executor/?eligible=0&sort=priority&dir={direction}"
            )
            body = resp.text
            # The present row always precedes the degraded one regardless
            # of sort direction (partition, not sentinel).
            assert body.index("present") < body.index("idea unavailable")


class TestRegistrationLadder:
    def test_not_registered_shows_note_and_empty(
        self, exec_client: TestClient, store: InMemoryStore
    ) -> None:
        client = _signed_in(exec_client)
        seed_implement_task(store, slug="alpha", base_sha=BASE_SHA)
        from eden_storage.errors import NotFound

        def _missing(_: str) -> object:
            raise NotFound("not registered")

        store.read_worker = _missing  # type: ignore[method-assign]
        resp = client.get("/executor/")
        body = resp.text
        assert "not registered for this experiment" in body
        # eligible=1 default → all rows ineligible → hidden.
        assert "alpha" not in body

    def test_registration_transport_failure_marks_unknown(
        self, exec_client: TestClient, store: InMemoryStore
    ) -> None:
        client = _signed_in(exec_client)
        seed_implement_task(store, slug="alpha", base_sha=BASE_SHA)

        def _flaky(_: str) -> object:
            raise RuntimeError("transport blip")

        store.read_worker = _flaky  # type: ignore[method-assign]
        resp = client.get("/executor/")
        body = resp.text
        # The row stays visible (unknown ≠ definitively ineligible) but
        # the claim button is disabled and the eligibility banner fires.
        assert "alpha" in body
        assert "eligibility check(s) could not be resolved" in body
        assert "disabled" in body

    def test_group_probe_transport_failure_marks_unknown(
        self, exec_client: TestClient, store: InMemoryStore
    ) -> None:
        client = _signed_in(exec_client)
        seed_implement_task(
            store,
            slug="alpha",
            base_sha=BASE_SHA,
            target=TaskTarget(kind="group", id="admins"),
        )

        def _flaky(_w: str, _g: str) -> bool:
            raise RuntimeError("transport blip")

        store.resolve_worker_in_group = _flaky  # type: ignore[method-assign]
        resp = client.get("/executor/")
        body = resp.text
        assert "alpha" in body
        assert "eligibility check(s) could not be resolved" in body
        assert "(eligibility unknown)" in body
