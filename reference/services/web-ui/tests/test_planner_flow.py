"""Cross-request planner-flow tests.

These exercise full happy-path and recovery flows that the
per-route ``test_app.py`` deliberately does not cover.
"""

from __future__ import annotations

from datetime import UTC, datetime

from conftest import (
    EXPERIMENT_ID,
    SESSION_SECRET,
    WORKER_ID,
    _config,
    get_csrf,
)
from eden_dispatch import sweep_expired_claims
from eden_storage import InMemoryStore
from eden_web_ui import make_app
from fastapi.testclient import TestClient


def _draft_form(slug: str) -> dict[str, str]:
    return {
        "status": "success",
        "slug": slug,
        "priority": "1.5",
        "parent_commits": "a" * 40,
        "rationale": "## why\n\nBecause it's a sound plan.",
    }


class TestHappyPath:
    def test_claim_draft_submit_completes_task(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        store.create_plan_task("t-1")
        token = get_csrf(signed_in_client)

        resp = signed_in_client.post(
            "/planner/t-1/claim",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        draft = signed_in_client.get("/planner/t-1/draft")
        assert draft.status_code == 200
        assert "draft proposals" in draft.text.lower()

        form = _draft_form("first-feature") | {"csrf_token": token}
        resp = signed_in_client.post("/planner/t-1/submit", data=form)
        assert resp.status_code == 200
        assert "submitted" in resp.text.lower()

        assert store.read_task("t-1").state == "submitted"
        proposals = store.list_proposals(state="ready")
        assert len(proposals) == 1
        assert proposals[0].slug == "first-feature"

    def test_submit_error_path_writes_no_proposals(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        store.create_plan_task("t-err")
        token = get_csrf(signed_in_client)
        signed_in_client.post(
            "/planner/t-err/claim",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        resp = signed_in_client.post(
            "/planner/t-err/submit",
            data={"csrf_token": token, "status": "error"},
        )
        assert resp.status_code == 200
        assert store.read_task("t-err").state == "submitted"
        assert store.list_proposals() == []


class TestValidationRecovery:
    def test_invalid_form_re_renders_with_errors_and_input_preserved(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        store.create_plan_task("t-bad")
        token = get_csrf(signed_in_client)
        signed_in_client.post(
            "/planner/t-bad/claim",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        # priority is not a number; rationale empty; parent_commits invalid.
        resp = signed_in_client.post(
            "/planner/t-bad/submit",
            data={
                "csrf_token": token,
                "status": "success",
                "slug": "good-slug",
                "priority": "not-a-number",
                "parent_commits": "not-a-sha",
                "rationale": "",
            },
        )
        assert resp.status_code == 400
        # User input preserved.
        assert "good-slug" in resp.text
        assert "not-a-number" in resp.text
        assert "not-a-sha" in resp.text
        # Field errors surfaced.
        assert "priority must be a number" in resp.text
        assert "rationale markdown is required" in resp.text
        # No store mutation.
        assert store.list_proposals() == []
        assert store.read_task("t-bad").state == "claimed"


class TestMultiRow:
    def test_add_row_full_page_returns_form_with_one_more_empty_row(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        """No-JS path: add_row returns the full draft page with one more row."""
        store.create_plan_task("t-add")
        token = get_csrf(signed_in_client)
        signed_in_client.post(
            "/planner/t-add/claim",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        resp = signed_in_client.post(
            "/planner/t-add/add_row",
            data={
                "csrf_token": token,
                "slug": "feat-1",
                "priority": "1.0",
                "parent_commits": "a" * 40,
                "rationale": "first proposal rationale",
            },
        )
        assert resp.status_code == 200
        # First row's input is preserved.
        assert "feat-1" in resp.text
        assert "first proposal rationale" in resp.text
        # The form now has 2 rows — count textareas as a per-row marker.
        assert resp.text.count('name="rationale"') == 2
        # Full page: includes the base layout (topbar nav).
        assert "<header" in resp.text

    def test_add_row_htmx_returns_partial_only(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        """JS path: add_row with HX-Request: true returns only the row partial."""
        store.create_plan_task("t-htmx")
        token = get_csrf(signed_in_client)
        signed_in_client.post(
            "/planner/t-htmx/claim",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        resp = signed_in_client.post(
            "/planner/t-htmx/add_row",
            data={
                "csrf_token": token,
                "slug": "feat-1",
                "priority": "1.0",
                "parent_commits": "a" * 40,
                "rationale": "first",
            },
            headers={"hx-request": "true"},
        )
        assert resp.status_code == 200
        # Just the new (empty) row partial — no <header>, no <html>.
        assert "<header" not in resp.text
        assert "<html" not in resp.text
        # Contains exactly one textarea (the new row's rationale field).
        assert resp.text.count('name="rationale"') == 1
        # The new row's index is 1 (zero-based; row 1 is the second row,
        # since the user already had 1 row before adding).
        assert 'data-row-index="1"' in resp.text

    def test_skip_fully_blank_row_on_submit(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        """Submitting with a trailing blank row only persists the filled rows."""
        from urllib.parse import urlencode

        store.create_plan_task("t-skip")
        token = get_csrf(signed_in_client)
        signed_in_client.post(
            "/planner/t-skip/claim",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        body = urlencode(
            [
                ("csrf_token", token),
                ("status", "success"),
                ("slug", "feat-only"),
                ("priority", "1.0"),
                ("parent_commits", "a" * 40),
                ("rationale", "real proposal"),
                ("slug", ""),
                ("priority", "1.0"),
                ("parent_commits", ""),
                ("rationale", ""),
            ]
        )
        resp = signed_in_client.post(
            "/planner/t-skip/submit",
            content=body,
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        assert resp.status_code == 200
        ready = store.list_proposals(state="ready")
        assert len(ready) == 1
        assert ready[0].slug == "feat-only"

    def test_all_blank_rejected(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        """If every row is blank, submit re-renders with a form-level error."""
        store.create_plan_task("t-all-blank")
        token = get_csrf(signed_in_client)
        signed_in_client.post(
            "/planner/t-all-blank/claim",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        resp = signed_in_client.post(
            "/planner/t-all-blank/submit",
            data={
                "csrf_token": token,
                "status": "success",
                "slug": "",
                "priority": "1.0",
                "parent_commits": "",
                "rationale": "",
            },
        )
        assert resp.status_code == 400
        assert "at least one proposal row must be filled in" in resp.text


class TestPerSessionClaimIsolation:
    def test_other_session_with_same_worker_id_cannot_use_first_session_claim(
        self, app, store: InMemoryStore
    ) -> None:
        """Two browser sessions with the same worker_id must not share claims."""
        store.create_plan_task("t-iso")
        with TestClient(app) as a:
            a.post("/signin", follow_redirects=False)
            csrf_a = get_csrf(a)
            resp = a.post(
                "/planner/t-iso/claim",
                data={"csrf_token": csrf_a},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            # First session can see its own draft form.
            resp = a.get("/planner/t-iso/draft")
            assert resp.status_code == 200

        # Second session — fresh sign-in (fresh CSRF) — cannot reach
        # the draft page, which means it cannot reuse the first
        # session's claim.
        with TestClient(app) as b:
            b.post("/signin", follow_redirects=False)
            resp = b.get("/planner/t-iso/draft", follow_redirects=False)
            assert resp.status_code == 303
            assert "claim+missing" in resp.headers["location"]


class TestDefinitiveSubmitErrors:
    """Each chapter-07 §7 error name surfaces on the orphan page.

    Note on operational semantics: by the time Phase 3 submit runs,
    Phase 1 has already created proposals (drafting) and Phase 2 has
    marked them ready. A definitive wire error at submit means those
    proposals are orphaned — the form-input preservation that
    applies to *validation* errors does not apply here, because the
    work product (the proposals themselves) is what needs recovery,
    not the form inputs. The orphan page lists the orphaned
    proposal IDs and surfaces the canonical error type so an
    operator can decide whether to reclaim the plan task or
    garbage-collect.
    """

    def _setup_claim(
        self, client: TestClient, store: InMemoryStore, task_id: str
    ) -> str:
        store.create_plan_task(task_id)
        token = get_csrf(client)
        resp = client.post(
            f"/planner/{task_id}/claim",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        return token

    def _submit(
        self, client: TestClient, task_id: str, csrf: str
    ):
        return client.post(
            f"/planner/{task_id}/submit",
            data={
                "csrf_token": csrf,
                "status": "success",
                "slug": "feat-x",
                "priority": "1.0",
                "parent_commits": "a" * 40,
                "rationale": "rationale",
            },
        )

    def test_wrong_token_lands_on_orphan_with_banner(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        monkeypatch,
    ) -> None:
        from eden_storage import WrongToken

        token = self._setup_claim(signed_in_client, store, "t-wt")

        def fail(*args, **kwargs):
            raise WrongToken("wrong token")

        monkeypatch.setattr(store, "submit", fail)
        resp = self._submit(signed_in_client, "t-wt", token)
        assert resp.status_code == 502
        assert "eden://error/wrong-token" in resp.text
        assert len(store.list_proposals(state="ready")) == 1

    def test_illegal_transition_lands_on_orphan_with_banner(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        monkeypatch,
    ) -> None:
        from eden_storage import IllegalTransition

        token = self._setup_claim(signed_in_client, store, "t-it")

        def fail(*args, **kwargs):
            raise IllegalTransition("not in claimed state")

        monkeypatch.setattr(store, "submit", fail)
        resp = self._submit(signed_in_client, "t-it", token)
        assert resp.status_code == 502
        assert "eden://error/illegal-transition" in resp.text

    def test_conflicting_resubmission_lands_on_orphan_with_banner(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        monkeypatch,
    ) -> None:
        from eden_storage import ConflictingResubmission

        token = self._setup_claim(signed_in_client, store, "t-cr")

        def fail(*args, **kwargs):
            raise ConflictingResubmission("payload diverged")

        monkeypatch.setattr(store, "submit", fail)
        resp = self._submit(signed_in_client, "t-cr", token)
        assert resp.status_code == 502
        assert "eden://error/conflicting-resubmission" in resp.text

    def test_invalid_precondition_lands_on_orphan_with_banner(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        monkeypatch,
    ) -> None:
        from eden_storage import InvalidPrecondition

        token = self._setup_claim(signed_in_client, store, "t-ip")

        def fail(*args, **kwargs):
            raise InvalidPrecondition("trial wrong status")

        monkeypatch.setattr(store, "submit", fail)
        resp = self._submit(signed_in_client, "t-ip", token)
        assert resp.status_code == 502
        assert "eden://error/invalid-precondition" in resp.text


class TestRetryBeforeOrphan:
    def test_transport_exception_retries_then_orphan(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        monkeypatch,
    ) -> None:
        """Raw transport exceptions (httpx-style, not DispatchError) are retried."""
        store.create_plan_task("t-trans")
        token = get_csrf(signed_in_client)
        signed_in_client.post(
            "/planner/t-trans/claim",
            data={"csrf_token": token},
            follow_redirects=False,
        )

        attempts = {"n": 0}

        class TransportError(Exception):
            pass

        def transport_fail(*args, **kwargs):
            attempts["n"] += 1
            raise TransportError("connect timeout")

        monkeypatch.setattr(store, "submit", transport_fail)
        resp = signed_in_client.post(
            "/planner/t-trans/submit",
            data={
                "csrf_token": token,
                "status": "success",
                "slug": "feat-trans",
                "priority": "1.0",
                "parent_commits": "a" * 40,
                "rationale": "rationale",
            },
        )
        assert resp.status_code == 502
        # Retried 3 times.
        assert attempts["n"] == 3
        # Banner names the transport error class.
        assert "TransportError" in resp.text


class TestStrandedClaim:
    def test_expired_claim_reclaims_via_sweeper(
        self,
        store: InMemoryStore,
        artifacts_dir,
        tmp_path,
    ) -> None:
        """Setting expires_at + sweeping returns a stranded task to pending."""
        # Build an app whose `now()` advances on each call so the second
        # tick is past the claim's expires_at.
        instants = iter([
            datetime(2026, 4, 24, 12, 0, tzinfo=UTC),
            datetime(2026, 4, 24, 14, 0, tzinfo=UTC),  # +2h
        ])

        def fake_now() -> datetime:
            try:
                return next(instants)
            except StopIteration:
                return datetime(2026, 4, 24, 14, 0, tzinfo=UTC)

        app = make_app(
            store=store,
            experiment_id=EXPERIMENT_ID,
            experiment_config=_config(),
            worker_id=WORKER_ID,
            session_secret=SESSION_SECRET,
            claim_ttl_seconds=60,  # 1 minute
            artifacts_dir=artifacts_dir,
            secure_cookies=False,
            now=fake_now,
        )
        with TestClient(app) as c:
            c.post("/signin", follow_redirects=False)
            store.create_plan_task("t-strand")
            csrf = get_csrf(c)
            c.post(
                "/planner/t-strand/claim",
                data={"csrf_token": csrf},
                follow_redirects=False,
            )
            assert store.read_task("t-strand").state == "claimed"

        # Sweep using a now that's well past the expiry.
        n = sweep_expired_claims(
            store, now=datetime(2026, 4, 24, 12, 5, tzinfo=UTC)
        )
        assert n == 1
        assert store.read_task("t-strand").state == "pending"
