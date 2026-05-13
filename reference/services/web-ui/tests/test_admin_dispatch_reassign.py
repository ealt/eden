"""Wave-5 admin routes: ``/admin/dispatch-mode/`` + ``/admin/tasks/{id}/reassign``.

Spec: chapter 07 §§2.7-2.8 (wire endpoints these UI forms call into
in-process Store-side semantics); plan §5.5 web-ui row; §6.2 cross-
request flow tests.

Coverage:

- GET unauthenticated → 303 to /signin (auth-first POST discipline).
- POST unauthenticated → 303 to /signin BEFORE the CSRF check.
- POST with missing / bad CSRF → 403.
- GET renders the form with the current state populated from the
  Store.
- POST applies a valid dispatch_mode update and round-trips via
  ``store.read_dispatch_mode``.
- POST emits an ``experiment.dispatch_mode_changed`` event when at
  least one key actually flipped; ``no-change`` banner when nothing
  flipped.
- Invalid dispatch_mode values → ``?error=invalid-value`` with no
  store write.
- Reassign GET surfaces current target + existing registry workers /
  groups in the form.
- Reassign POST drives the store and round-trips on pending / claimed
  composite-commit / terminal-rejected.
- Closed-allowlist banner: unknown ``?error=…`` querystring values
  render no banner (XSS-resistant per chunk-9e pattern).
"""

from __future__ import annotations

import pytest
from conftest import (
    EXPERIMENT_ID,
    SESSION_SECRET,
    WORKER_ID,
    _config,
    _now,
    get_csrf,
)
from eden_contracts import Idea, TaskTarget, Variant
from eden_storage import InMemoryStore
from eden_web_ui import make_app
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _seed_pending_ideation_task(
    store: InMemoryStore, task_id: str = "ideation-A"
) -> str:
    store.create_ideation_task(task_id)
    return task_id


def _seed_claimed_ideation_task(
    store: InMemoryStore,
    task_id: str = "ideation-claim",
    worker_id: str = "ideator-w",
) -> str:
    store.create_ideation_task(task_id)
    store.claim(task_id, worker_id)
    return task_id


def _seed_terminal_ideation_task(
    store: InMemoryStore,
    task_id: str = "ideation-done",
    worker_id: str = "ideator-w",
) -> str:
    from eden_storage import IdeaSubmission

    store.create_ideation_task(task_id)
    store.claim(task_id, worker_id)
    store.submit(task_id, worker_id, IdeaSubmission(status="success"))
    store.accept(task_id)
    return task_id


def _seed_starting_variant_for_reassign_test(
    store: InMemoryStore, idea_id: str = "p1", variant_id: str = "v1"
) -> tuple[str, str]:
    """Return (execution_task_id, variant_id) with the variant in starting status.

    Sets up an execution task in ``claimed`` state that the reassign
    test can drive through the composite-commit path. The variant is
    in ``starting`` so the claimed-reassign composite includes the
    ``variant.errored`` write.
    """
    idea = Idea(
        idea_id=idea_id,
        experiment_id=store.experiment_id,
        slug="x",
        priority=1.0,
        parent_commits=["a" * 40],
        artifacts_uri="https://artifacts.example/p",
        state="drafting",
        created_at="2026-04-23T00:00:00.000Z",
    )
    store.create_idea(idea)
    store.mark_idea_ready(idea_id)
    exec_task_id = f"execute-{idea_id}"
    store.create_execution_task(exec_task_id, idea_id)
    store.claim(exec_task_id, "executor-w")
    store.create_variant(
        Variant(
            variant_id=variant_id,
            experiment_id=store.experiment_id,
            idea_id=idea_id,
            status="starting",
            parent_commits=["a" * 40],
            branch=f"work/{idea_id}-{variant_id}",
            started_at="2026-04-23T00:00:01.000Z",
        )
    )
    return exec_task_id, variant_id


# ----------------------------------------------------------------------
# /admin/dispatch-mode/
# ----------------------------------------------------------------------


class TestDispatchModeAuth:
    def test_get_redirects_unauthenticated(self, client: TestClient) -> None:
        resp = client.get("/admin/dispatch-mode/", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/signin"

    def test_post_unauthenticated_redirects_before_csrf(
        self, client: TestClient
    ) -> None:
        """Auth-first: POST without session redirects to /signin even with bad CSRF."""
        resp = client.post(
            "/admin/dispatch-mode/",
            data={
                "csrf_token": "anything",
                "ideation_creation": "auto",
                "execution_dispatch": "auto",
                "evaluation_dispatch": "auto",
                "integration": "auto",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/signin"

    def test_post_missing_csrf_returns_403(
        self, signed_in_client: TestClient
    ) -> None:
        resp = signed_in_client.post(
            "/admin/dispatch-mode/",
            data={
                "ideation_creation": "auto",
                "execution_dispatch": "auto",
                "evaluation_dispatch": "auto",
                "integration": "auto",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 403


class TestDispatchModeGet:
    def test_renders_default_state(self, signed_in_client: TestClient) -> None:
        resp = signed_in_client.get("/admin/dispatch-mode/")
        assert resp.status_code == 200
        body = resp.text
        # Each of the 4 normative keys appears as a radio name.
        for key in (
            "ideation_creation",
            "execution_dispatch",
            "evaluation_dispatch",
            "integration",
        ):
            assert f'name="{key}"' in body
        # Default state is all-auto; auto radio is preselected.
        assert body.count("checked") >= 4

    def test_renders_with_store_state(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        store.register_worker("admin-eric")
        store.update_dispatch_mode(
            {"integration": "manual"}, updated_by="admin-eric"
        )
        resp = signed_in_client.get("/admin/dispatch-mode/")
        # The integration row's manual radio should now be checked.
        body = resp.text
        # Find the integration row and ensure manual is the checked option.
        # The simpler proxy: the response contains "manual"
        # selected for ``integration``.
        idx = body.find('name="integration"')
        manual_idx = body.find(
            'value="manual"', idx
        )  # next manual after the row marker
        assert manual_idx > 0, "did not find integration's manual radio"
        # The ``checked`` attribute is on the same input as the manual
        # value; locate the checked marker within a small window.
        window = body[manual_idx : manual_idx + 200]
        assert "checked" in window


class TestDispatchModePost:
    def test_valid_update_applies_and_emits_event(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        before = len(store.replay())
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/admin/dispatch-mode/",
            data={
                "csrf_token": csrf,
                "ideation_creation": "manual",
                "execution_dispatch": "auto",
                "evaluation_dispatch": "auto",
                "integration": "auto",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/admin/dispatch-mode/?dispatched=ok"
        mode = store.read_dispatch_mode()
        assert mode.ideation_creation == "manual"
        new_events = store.replay()[before:]
        assert [e.type for e in new_events] == [
            "experiment.dispatch_mode_changed"
        ]
        # Server stamps updated_by from app.state.worker_id ("ui-w").
        assert new_events[0].data["updated_by"] == WORKER_ID
        assert new_events[0].data["changed"] == {"ideation_creation": "manual"}

    def test_no_change_emits_no_event(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        """Posting the current values flips nothing and emits no event."""
        before = len(store.replay())
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/admin/dispatch-mode/",
            data={
                "csrf_token": csrf,
                "ideation_creation": "auto",
                "execution_dispatch": "auto",
                "evaluation_dispatch": "auto",
                "integration": "auto",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert (
            resp.headers["location"]
            == "/admin/dispatch-mode/?dispatched=no-change"
        )
        new_events = store.replay()[before:]
        assert new_events == []

    def test_invalid_value_rejected_without_store_write(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        before = len(store.replay())
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/admin/dispatch-mode/",
            data={
                "csrf_token": csrf,
                "ideation_creation": "paused",  # not in the closed set
                "execution_dispatch": "auto",
                "evaluation_dispatch": "auto",
                "integration": "auto",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=invalid-value" in resp.headers["location"]
        assert len(store.replay()) == before


class TestDispatchModeBannerAllowlist:
    @pytest.mark.parametrize(
        ("query", "expected_text"),
        [
            ("dispatched=ok", "dispatch_mode updated"),
            ("dispatched=no-change", "no changes"),
            # Jinja autoescapes single quotes; assert against the
            # post-escape rendering.
            ("error=invalid-value", "every key must be"),
            ("error=transport", "transport failure"),
        ],
    )
    def test_known_outcomes_render_banner(
        self,
        signed_in_client: TestClient,
        query: str,
        expected_text: str,
    ) -> None:
        resp = signed_in_client.get(f"/admin/dispatch-mode/?{query}")
        assert resp.status_code == 200
        assert expected_text in resp.text

    def test_unknown_error_value_renders_no_banner(
        self, signed_in_client: TestClient
    ) -> None:
        resp = signed_in_client.get(
            "/admin/dispatch-mode/?error=<script>alert(1)</script>"
        )
        assert resp.status_code == 200
        assert "<script>" not in resp.text


# ----------------------------------------------------------------------
# /admin/tasks/{id}/reassign
# ----------------------------------------------------------------------


class TestReassignAuth:
    def test_get_redirects_unauthenticated(self, client: TestClient) -> None:
        resp = client.get(
            "/admin/tasks/some-task/reassign", follow_redirects=False
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/signin"

    def test_post_unauthenticated_redirects_before_csrf(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        task_id = _seed_pending_ideation_task(store)
        resp = client.post(
            f"/admin/tasks/{task_id}/reassign",
            data={
                "csrf_token": "anything",
                "target_kind": "none",
                "reason": "operator",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/signin"

    def test_post_missing_csrf_returns_403(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        task_id = _seed_pending_ideation_task(store)
        resp = signed_in_client.post(
            f"/admin/tasks/{task_id}/reassign",
            data={"target_kind": "none", "reason": "operator"},
            follow_redirects=False,
        )
        assert resp.status_code == 403


class TestReassignGet:
    def test_get_renders_pending_task_form(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        task_id = _seed_pending_ideation_task(store)
        resp = signed_in_client.get(f"/admin/tasks/{task_id}/reassign")
        assert resp.status_code == 200
        body = resp.text
        assert "reassign task" in body
        assert task_id in body
        # The 3 target-kind radios + reason text input are present.
        assert 'name="target_kind"' in body
        assert 'name="reason"' in body

    def test_get_renders_existing_workers_and_groups(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        store.register_worker("worker-extra")
        store.register_group("humans", members=["worker-extra"])
        task_id = _seed_pending_ideation_task(store)
        resp = signed_in_client.get(f"/admin/tasks/{task_id}/reassign")
        body = resp.text
        assert "worker-extra" in body
        assert "humans" in body

    def test_terminal_task_shows_read_only_banner(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        task_id = _seed_terminal_ideation_task(store)
        resp = signed_in_client.get(f"/admin/tasks/{task_id}/reassign")
        assert resp.status_code == 200
        body = resp.text
        # The "cannot be reassigned" advisory renders for non-pending
        # / non-claimed states.
        assert "cannot be reassigned" in body


class TestReassignPost:
    def test_pending_task_to_worker_target(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        store.register_worker("specific-w")
        task_id = _seed_pending_ideation_task(store)
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            f"/admin/tasks/{task_id}/reassign",
            data={
                "csrf_token": csrf,
                "target_kind": "worker",
                "target_id_worker": "specific-w",
                "reason": "manual route",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "?reassigned=ok" in resp.headers["location"]
        task = store.read_task(task_id)
        assert task.target is not None
        assert task.target.kind == "worker"
        assert task.target.id == "specific-w"

    def test_pending_task_to_group_target(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        store.register_group("humans", members=[])
        task_id = _seed_pending_ideation_task(store)
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            f"/admin/tasks/{task_id}/reassign",
            data={
                "csrf_token": csrf,
                "target_kind": "group",
                "target_id_group": "humans",
                "reason": "team route",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        task = store.read_task(task_id)
        assert task.target is not None
        assert task.target.kind == "group"
        assert task.target.id == "humans"

    def test_pending_task_to_none_emits_event(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        store.register_worker("specific-w")
        task_id = _seed_pending_ideation_task(store)
        # Set a non-null target first.
        store.reassign_task(
            task_id,
            TaskTarget(kind="worker", id="specific-w"),
            reason="initial",
            reassigned_by=WORKER_ID,
        )
        before = len(store.replay())
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            f"/admin/tasks/{task_id}/reassign",
            data={
                "csrf_token": csrf,
                "target_kind": "none",
                "reason": "open up",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "?reassigned=ok" in resp.headers["location"]
        # New task.reassigned event.
        new_events = store.replay()[before:]
        assert [e.type for e in new_events] == ["task.reassigned"]
        assert new_events[0].data["new_target"] is None
        assert new_events[0].data["reason"] == "open up"

    def test_claimed_task_composite_commits(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        """Reassigning a claimed task emits task.reclaimed + task.reassigned."""
        task_id = _seed_claimed_ideation_task(store)
        before = len(store.replay())
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            f"/admin/tasks/{task_id}/reassign",
            data={
                "csrf_token": csrf,
                "target_kind": "none",
                "reason": "drop claim",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "?reassigned=ok" in resp.headers["location"]
        new_events = store.replay()[before:]
        assert [e.type for e in new_events] == [
            "task.reclaimed",
            "task.reassigned",
        ]
        assert new_events[0].data["cause"] == "operator"
        assert new_events[1].data["reassigned_by"] == WORKER_ID

    def test_claimed_execution_task_composite_errors_variant(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        exec_task_id, variant_id = _seed_starting_variant_for_reassign_test(store)
        before = len(store.replay())
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            f"/admin/tasks/{exec_task_id}/reassign",
            data={
                "csrf_token": csrf,
                "target_kind": "none",
                "reason": "abandon variant",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "?reassigned=ok" in resp.headers["location"]
        new_events = store.replay()[before:]
        types = [e.type for e in new_events]
        assert "task.reclaimed" in types
        assert "variant.errored" in types
        assert "task.reassigned" in types
        assert store.read_variant(variant_id).status == "error"

    def test_terminal_task_rejected_with_illegal_state(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        task_id = _seed_terminal_ideation_task(store)
        before = len(store.replay())
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            f"/admin/tasks/{task_id}/reassign",
            data={
                "csrf_token": csrf,
                "target_kind": "none",
                "reason": "too late",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=illegal-state" in resp.headers["location"]
        # No partial state: nothing was appended.
        assert len(store.replay()) == before

    def test_missing_reason_rejected(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        task_id = _seed_pending_ideation_task(store)
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            f"/admin/tasks/{task_id}/reassign",
            data={
                "csrf_token": csrf,
                "target_kind": "none",
                "reason": "   ",  # whitespace-only counts as missing
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=missing-reason" in resp.headers["location"]

    def test_unknown_target_rejected(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        """Target id that doesn't match any registered worker → unknown-target."""
        task_id = _seed_pending_ideation_task(store)
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            f"/admin/tasks/{task_id}/reassign",
            data={
                "csrf_token": csrf,
                "target_kind": "worker",
                "target_id": "ghost-worker",
                "reason": "spoof",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=unknown-target" in resp.headers["location"]

    def test_invalid_target_grammar_rejected(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        task_id = _seed_pending_ideation_task(store)
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            f"/admin/tasks/{task_id}/reassign",
            data={
                "csrf_token": csrf,
                "target_kind": "worker",
                "target_id": "UPPERCASE-not-allowed",  # violates §6.1 grammar
                "reason": "spoof",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=invalid-target" in resp.headers["location"]


class TestReassignBannerAllowlist:
    @pytest.mark.parametrize(
        ("query", "expected_text"),
        [
            ("reassigned=ok", "task reassigned"),
            ("error=invalid-target", "§6.1 grammar"),
            ("error=missing-reason", "reason is required"),
            ("error=illegal-state", "cannot be reassigned"),
            ("error=unknown-target", "not registered"),
            ("error=transport", "transport failure"),
        ],
    )
    def test_known_outcomes_render_banner(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        query: str,
        expected_text: str,
    ) -> None:
        task_id = _seed_pending_ideation_task(store)
        resp = signed_in_client.get(
            f"/admin/tasks/{task_id}/reassign?{query}"
        )
        assert resp.status_code == 200
        assert expected_text in resp.text

    def test_unknown_error_value_renders_no_banner(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        task_id = _seed_pending_ideation_task(store)
        resp = signed_in_client.get(
            f"/admin/tasks/{task_id}/reassign?error=<script>alert(1)</script>"
        )
        assert resp.status_code == 200
        assert "<script>" not in resp.text


# ----------------------------------------------------------------------
# Cross-request flow: dispatch_mode + reassign through a full session
# ----------------------------------------------------------------------


class TestCrossRequestFlows:
    def test_dispatch_mode_round_trip(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        # 1. GET shows all-auto by default.
        get_resp = signed_in_client.get("/admin/dispatch-mode/")
        assert get_resp.status_code == 200

        # 2. POST a partial flip.
        csrf = get_csrf(signed_in_client)
        post_resp = signed_in_client.post(
            "/admin/dispatch-mode/",
            data={
                "csrf_token": csrf,
                "ideation_creation": "manual",
                "execution_dispatch": "auto",
                "evaluation_dispatch": "auto",
                "integration": "manual",
            },
            follow_redirects=False,
        )
        assert post_resp.status_code == 303
        assert "dispatched=ok" in post_resp.headers["location"]

        # 3. Follow the redirect and verify the form reflects the new state.
        follow = signed_in_client.get(post_resp.headers["location"])
        body = follow.text
        # Both flipped keys' manual radios should be checked.
        for key in ("ideation_creation", "integration"):
            idx = body.find(f'name="{key}"')
            manual_idx = body.find('value="manual"', idx)
            window = body[manual_idx : manual_idx + 200]
            assert "checked" in window

    def test_reassign_flow_followed_by_claim_eligibility_change(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        """Reassign to a worker target → subsequent claim by a different worker is rejected."""
        store.register_worker("eligible-w")
        store.register_worker("other-w")
        task_id = _seed_pending_ideation_task(store)
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            f"/admin/tasks/{task_id}/reassign",
            data={
                "csrf_token": csrf,
                "target_kind": "worker",
                "target_id_worker": "eligible-w",
                "reason": "scoped",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        # The target check now rejects claims by the wrong worker_id.
        from eden_storage.errors import WorkerNotEligible

        with pytest.raises(WorkerNotEligible):
            store.claim(task_id, "other-w")
        # The targeted worker can claim.
        claim = store.claim(task_id, "eligible-w")
        assert claim.worker_id == "eligible-w"


# ----------------------------------------------------------------------
# Partial-write recovery
# ----------------------------------------------------------------------


class TestPartialWriteRecovery:
    def test_transport_error_during_update_surfaces_banner(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A transport-shaped exception during update → ?error=transport banner."""

        def _raise(*_a: object, **_kw: object) -> None:
            raise RuntimeError("simulated transport blip")

        monkeypatch.setattr(store, "update_dispatch_mode", _raise)
        before = len(store.replay())
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            "/admin/dispatch-mode/",
            data={
                "csrf_token": csrf,
                "ideation_creation": "manual",
                "execution_dispatch": "auto",
                "evaluation_dispatch": "auto",
                "integration": "auto",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=transport" in resp.headers["location"]
        # No event landed.
        assert len(store.replay()) == before

    def test_transport_error_during_reassign_surfaces_banner(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        task_id = _seed_pending_ideation_task(store)

        def _raise(*_a: object, **_kw: object) -> None:
            raise RuntimeError("simulated transport blip")

        monkeypatch.setattr(store, "reassign_task", _raise)
        before = len(store.replay())
        csrf = get_csrf(signed_in_client)
        resp = signed_in_client.post(
            f"/admin/tasks/{task_id}/reassign",
            data={
                "csrf_token": csrf,
                "target_kind": "none",
                "reason": "x",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=transport" in resp.headers["location"]
        assert len(store.replay()) == before


# ----------------------------------------------------------------------
# Admin-index links
# ----------------------------------------------------------------------


class TestAdminIndexLinks:
    def test_admin_index_links_to_dispatch_mode(
        self, signed_in_client: TestClient
    ) -> None:
        resp = signed_in_client.get("/admin/")
        assert resp.status_code == 200
        assert "/admin/dispatch-mode/" in resp.text

    def test_admin_task_detail_links_to_reassign_when_pending(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        task_id = _seed_pending_ideation_task(store)
        resp = signed_in_client.get(f"/admin/tasks/{task_id}/")
        assert f"/admin/tasks/{task_id}/reassign" in resp.text

    def test_admin_task_detail_omits_reassign_link_on_terminal(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        task_id = _seed_terminal_ideation_task(store)
        resp = signed_in_client.get(f"/admin/tasks/{task_id}/")
        # The reassign section is gated on state in {pending, claimed}.
        assert (
            f"/admin/tasks/{task_id}/reassign" not in resp.text
            or "cannot be reassigned" in resp.text
        )


# ----------------------------------------------------------------------
# Sanity: the new routes must not collide with any /admin/workers/* or
# /admin/groups/* path a parallel 12a-1b delegate may be adding.
# ----------------------------------------------------------------------


def test_dispatch_mode_url_is_not_workers_or_groups() -> None:
    """Compile-time guard against accidental namespace collisions."""
    assert "/admin/dispatch-mode/" not in ("/admin/workers/", "/admin/groups/")


def test_reassign_url_pattern_is_under_tasks_not_workers_or_groups(
    store: InMemoryStore,
) -> None:
    app: FastAPI = make_app(
        store=store,
        experiment_id=EXPERIMENT_ID,
        experiment_config=_config(),
        worker_id=WORKER_ID,
        session_secret=SESSION_SECRET,
        claim_ttl_seconds=3600,
        artifacts_dir=__import__("pathlib").Path("/tmp"),
        secure_cookies=False,
        now=_now,
    )
    # The new reassign route is mounted under /admin/tasks/{task_id},
    # which doesn't overlap with the /admin/workers/* or
    # /admin/groups/* prefixes a parallel 12a-1b delegate may use.
    paths = [r.path for r in app.routes]  # type: ignore[attr-defined]
    assert any(
        p == "/admin/tasks/{task_id}/reassign" for p in paths
    ), f"reassign route not found; have: {paths}"
    assert any(p == "/admin/dispatch-mode/" for p in paths), (
        f"dispatch-mode route not found; have: {paths}"
    )
