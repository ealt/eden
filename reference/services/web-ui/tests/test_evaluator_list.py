"""Evaluator pending-task list rendering (issue #137 redesign).

Mirrors the executor list tests: 5-column table, priority-default sort,
eligibility / target / group filters, eligibility-aware claim button,
click-to-expand context row (with the evaluator-only variant link), and
the two warning banners. Supersedes the pre-redesign inline-preview
tests.
"""

from __future__ import annotations

import pytest
from conftest import group_id_by_name, seed_evaluate_task
from eden_contracts import TaskTarget
from eden_storage import InMemoryStore
from fastapi.testclient import TestClient


def _signed_in(client: TestClient) -> TestClient:
    resp = client.post("/signin", follow_redirects=False)
    assert resp.status_code == 303
    return client


def _pending_html(body: str) -> str:
    """Slice out just the pending-tasks section.

    The page also renders a "recent variants" table whose branch names
    embed slug substrings (``work/va-alpha``); negative slug assertions
    must be scoped to the pending section to avoid false matches there.
    """
    start = body.index('<section class="pending">')
    end = body.index('<section class="recent">', start)
    return body[start:end]


@pytest.fixture(autouse=True)
def _isolate_claims():
    from eden_web_ui.routes import evaluator as evaluator_routes

    evaluator_routes._CLAIMS.clear()
    yield
    evaluator_routes._CLAIMS.clear()


class TestColumns:
    def test_renders_slug_priority_target_created_by(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        _signed_in(client)
        seed_evaluate_task(
            store,
            slug="alpha",
            variant_id="va",
            priority=3.0,
            created_by=store._test_worker_ids["ideator-1"],
        )
        resp = client.get("/evaluator/")
        assert resp.status_code == 200
        body = resp.text
        assert "alpha" in body
        assert "3.0" in body
        assert "ideator-1" in body
        assert "priority" in body
        assert "created by" in body

    def test_inline_artifact_preview_is_gone(
        self, client: TestClient, store: InMemoryStore, artifacts_dir
    ) -> None:
        _signed_in(client)
        path = artifacts_dir / "variant-artifact.md"
        path.write_text("variant artifact body")
        seed_evaluate_task(
            store, slug="alpha", variant_id="va", variant_artifact_path=path
        )
        resp = client.get("/evaluator/")
        assert resp.status_code == 200
        # No inline variant-artifact body any more.
        assert "variant artifact body" not in resp.text
        assert "artifacts preview" not in resp.text

    def test_admin_lineage_link_present(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        _signed_in(client)
        eval_id, _, _ = seed_evaluate_task(store, slug="alpha", variant_id="va")
        resp = client.get("/evaluator/")
        assert f"/admin/tasks/{eval_id}/" in resp.text


class TestSort:
    def test_default_sort_is_priority_desc(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        _signed_in(client)
        seed_evaluate_task(store, slug="low", variant_id="vl", priority=1.0)
        seed_evaluate_task(store, slug="high", variant_id="vh", priority=9.0)
        resp = client.get("/evaluator/")
        body = resp.text
        assert body.index("high") < body.index("low")

    def test_slug_sort_ascending(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        _signed_in(client)
        seed_evaluate_task(store, slug="zeta", variant_id="vz", priority=9.0)
        seed_evaluate_task(store, slug="alpha", variant_id="va", priority=1.0)
        resp = client.get("/evaluator/?sort=slug&dir=asc")
        body = resp.text
        assert body.index(">alpha<") < body.index(">zeta<")

    def test_unknown_sort_param_falls_back_to_default(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        _signed_in(client)
        seed_evaluate_task(store, slug="low", variant_id="vl", priority=1.0)
        seed_evaluate_task(store, slug="high", variant_id="vh", priority=9.0)
        resp = client.get("/evaluator/?sort=<script>&dir=evil")
        body = resp.text
        assert "<script>" not in body
        assert body.index("high") < body.index("low")


class TestEligibilityFilter:
    def test_untargeted_is_eligible_with_claim_form(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        _signed_in(client)
        eval_id, _, _ = seed_evaluate_task(store, slug="alpha", variant_id="va")
        resp = client.get("/evaluator/")
        assert f'action="/evaluator/{eval_id}/claim"' in resp.text

    def test_ineligible_hidden_by_default_filter(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        _signed_in(client)
        seed_evaluate_task(
            store,
            slug="alpha",
            variant_id="va",
            target=TaskTarget(kind="worker", id=store._test_worker_ids["other-w"]),
        )
        resp = client.get("/evaluator/")
        pending = _pending_html(resp.text)
        assert "alpha" not in pending
        assert "no pending evaluation tasks eligible" in pending

    def test_ineligible_shown_disabled_when_filter_off(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        _signed_in(client)
        eval_id, _, _ = seed_evaluate_task(
            store,
            slug="alpha",
            variant_id="va",
            target=TaskTarget(kind="worker", id=store._test_worker_ids["other-w"]),
        )
        resp = client.get("/evaluator/?eligible=0")
        body = resp.text
        assert "alpha" in body
        assert "disabled" in body
        assert "you are not in its target" in body
        assert f'action="/evaluator/{eval_id}/claim"' not in body

    def test_group_target_eligible_when_member(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        _signed_in(client)
        eval_id, _, _ = seed_evaluate_task(
            store,
            slug="alpha",
            variant_id="va",
            target=TaskTarget(kind="group", id=group_id_by_name(store, "admins")),
        )
        resp = client.get("/evaluator/")
        assert f'action="/evaluator/{eval_id}/claim"' in resp.text


class TestTargetFilter:
    def test_targeted_only(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        _signed_in(client)
        seed_evaluate_task(store, slug="free", variant_id="vf")
        seed_evaluate_task(
            store,
            slug="bound",
            variant_id="vb",
            target=TaskTarget(kind="group", id=group_id_by_name(store, "admins")),
        )
        resp = client.get("/evaluator/?target=targeted")
        pending = _pending_html(resp.text)
        assert "bound" in pending
        assert "free" not in pending

    def test_untargeted_only(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        _signed_in(client)
        seed_evaluate_task(store, slug="free", variant_id="vf")
        seed_evaluate_task(
            store,
            slug="bound",
            variant_id="vb",
            target=TaskTarget(kind="group", id=group_id_by_name(store, "admins")),
        )
        resp = client.get("/evaluator/?target=untargeted")
        pending = _pending_html(resp.text)
        assert "free" in pending
        assert "bound" not in pending


class TestGroupByCreator:
    def test_group_toggle_wraps_rows_by_creator(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        _signed_in(client)
        seed_evaluate_task(
            store, slug="a1", variant_id="va", created_by=store._test_worker_ids["ideator-1"]
        )
        seed_evaluate_task(
            store, slug="b1", variant_id="vb", created_by=store._test_worker_ids["ideator-w"]
        )
        resp = client.get("/evaluator/?group=1")
        body = resp.text
        assert 'class="creator-group"' in body
        assert "ideator-1" in body
        assert "ideator-w" in body


class TestExpansion:
    def test_expansion_has_variant_and_context_links(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        _signed_in(client)
        eval_id, variant_id, idea_id = seed_evaluate_task(
            store, slug="alpha", variant_id="va", created_by=store._test_worker_ids["ideator-1"]
        )
        resp = client.get("/evaluator/")
        body = resp.text
        assert "context links" in body
        # Evaluator-specific: variant detail link + branch text.
        assert f"/admin/variants/{variant_id}/" in body
        assert "work/va-alpha" in body
        assert f"/admin/ideas/{idea_id}/" in body
        assert f"/admin/workers/{store._test_worker_ids['ideator-1']}/" in body


class TestDegradedRows:
    def test_variant_unavailable_renders_degraded(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        _signed_in(client)
        seed_evaluate_task(store, slug="alpha", variant_id="va")
        from eden_storage.errors import NotFound

        def _missing(_: str) -> object:
            raise NotFound("no such variant")

        store.read_variant = _missing  # type: ignore[method-assign]
        resp = client.get("/evaluator/?eligible=0")
        assert "(variant unavailable)" in resp.text

    def test_variant_transport_error_increments_read_banner(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        _signed_in(client)
        seed_evaluate_task(store, slug="alpha", variant_id="va")

        def _flaky(_: str) -> object:
            raise RuntimeError("transport blip")

        store.read_variant = _flaky  # type: ignore[method-assign]
        resp = client.get("/evaluator/?eligible=0")
        assert "variant/idea read(s) failed" in resp.text
        assert "(read failed)" in resp.text

    def test_idea_read_failure_keeps_variant_context(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        _signed_in(client)
        seed_evaluate_task(store, slug="alpha", variant_id="va")

        orig = store.read_idea

        def _flaky(_: str) -> object:
            raise RuntimeError("transport blip")

        store.read_idea = _flaky  # type: ignore[method-assign]
        try:
            resp = client.get("/evaluator/?eligible=0")
            body = resp.text
            # variant context (branch link) still surfaces; only the idea
            # half degraded.
            assert "work/va-alpha" in body
            assert "variant/idea read(s) failed" in body
            # The variant read succeeded, so the row is not "(read failed)".
            assert "(read failed)" not in body
        finally:
            store.read_idea = orig


class TestRegistrationLadder:
    def test_not_registered_shows_note_and_empty(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        _signed_in(client)
        seed_evaluate_task(store, slug="alpha", variant_id="va")
        from eden_storage.errors import NotFound

        def _missing(_: str) -> object:
            raise NotFound("not registered")

        store.read_worker = _missing  # type: ignore[method-assign]
        resp = client.get("/evaluator/")
        assert "not registered for this experiment" in resp.text
        assert "alpha" not in _pending_html(resp.text)

    def test_registration_transport_failure_marks_unknown(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        _signed_in(client)
        seed_evaluate_task(store, slug="alpha", variant_id="va")

        def _flaky(_: str) -> object:
            raise RuntimeError("transport blip")

        store.read_worker = _flaky  # type: ignore[method-assign]
        resp = client.get("/evaluator/")
        body = resp.text
        assert "alpha" in body
        assert "eligibility check(s) could not be resolved" in body
        assert "disabled" in body

    def test_group_probe_transport_failure_marks_unknown(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        _signed_in(client)
        seed_evaluate_task(
            store,
            slug="alpha",
            variant_id="va",
            target=TaskTarget(kind="group", id=group_id_by_name(store, "admins")),
        )

        def _flaky(_w: str, _g: str) -> bool:
            raise RuntimeError("transport blip")

        store.resolve_worker_in_group = _flaky  # type: ignore[method-assign]
        resp = client.get("/evaluator/")
        body = resp.text
        assert "alpha" in body
        assert "eligibility check(s) could not be resolved" in body
        assert "(eligibility unknown)" in body
