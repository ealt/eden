"""Cross-request ideator-flow tests.

These exercise full happy-path and recovery flows that the
per-route ``test_app.py`` deliberately does not cover.
"""
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

from datetime import UTC, datetime

from conftest import (
    EXPERIMENT_ID,
    SESSION_SECRET,
    _config,
    _one_experiment_factory,
    get_csrf,
    web_ui_worker_id,
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
        "content": "## why\n\nBecause it's a sound plan.",
    }


class TestHappyPath:
    def test_claim_draft_submit_completes_task(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        store.create_ideation_task("t-1")
        token = get_csrf(signed_in_client)

        resp = signed_in_client.post(
            "/ideator/t-1/claim",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        draft = signed_in_client.get("/ideator/t-1/draft")
        assert draft.status_code == 200
        assert "draft ideas" in draft.text.lower()

        form = _draft_form("first-feature") | {"csrf_token": token}
        resp = signed_in_client.post("/ideator/t-1/submit", data=form)
        assert resp.status_code == 200
        assert "submitted" in resp.text.lower()

        assert store.read_task("t-1").state == "submitted"
        ideas = store.list_ideas(state="ready")
        assert len(ideas) == 1
        assert ideas[0].slug == "first-feature"

    def test_submit_error_path_writes_no_ideas(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        store.create_ideation_task("t-err")
        token = get_csrf(signed_in_client)
        signed_in_client.post(
            "/ideator/t-err/claim",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        resp = signed_in_client.post(
            "/ideator/t-err/submit",
            data={"csrf_token": token, "status": "error"},
        )
        assert resp.status_code == 200
        assert store.read_task("t-err").state == "submitted"
        assert store.list_ideas() == []


class TestSlugSoftCheck:
    """Issue #121: when an ideator submits an idea whose slug collides
    with an existing idea in the experiment, the submitted page surfaces
    an advisory warning. Submission still succeeds — slug uniqueness is
    not a protocol invariant.
    """

    def _seed_ready_idea(self, store: InMemoryStore, *, idea_id: str, slug: str) -> None:
        from eden_contracts import Idea
        store.create_idea(
            Idea.model_validate(
                {
                    "idea_id": idea_id,
                    "experiment_id": EXPERIMENT_ID,
                    "slug": slug,
                    "priority": 0.0,
                    "parent_commits": ["a" * 40],
                    "artifacts_uri": "file:///tmp/seed.md",
                    "state": "drafting",
                    "created_at": "2026-05-01T00:00:00Z",
                }
            )
        )
        store.mark_idea_ready(idea_id)

    def test_duplicate_slug_surfaces_warning_on_success_page(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        self._seed_ready_idea(store, idea_id="idea-prior", slug="dup-slug")
        store.create_ideation_task("t-dup")
        token = get_csrf(signed_in_client)
        signed_in_client.post(
            "/ideator/t-dup/claim",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        form = _draft_form("dup-slug") | {"csrf_token": token}
        resp = signed_in_client.post("/ideator/t-dup/submit", data=form)
        assert resp.status_code == 200
        # Submission still succeeded.
        assert store.read_task("t-dup").state == "submitted"
        assert len(store.list_ideas(state="ready")) == 2
        # Warning rendered on the success page.
        body = resp.text
        assert "warnings" in body.lower()
        assert "dup-slug" in body
        assert "idea-prior" in body

    def test_unique_slug_does_not_render_warnings_block(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        store.create_ideation_task("t-uniq")
        token = get_csrf(signed_in_client)
        signed_in_client.post(
            "/ideator/t-uniq/claim",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        form = _draft_form("unique-slug") | {"csrf_token": token}
        resp = signed_in_client.post("/ideator/t-uniq/submit", data=form)
        assert resp.status_code == 200
        # No warnings block on the unique-slug submission.
        assert 'class="slug-warnings"' not in resp.text


class TestValidationRecovery:
    def test_invalid_form_re_renders_with_errors_and_input_preserved(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        store.create_ideation_task("t-bad")
        token = get_csrf(signed_in_client)
        signed_in_client.post(
            "/ideator/t-bad/claim",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        # priority is not a number; content empty; parent_commits invalid.
        resp = signed_in_client.post(
            "/ideator/t-bad/submit",
            data={
                "csrf_token": token,
                "status": "success",
                "slug": "good-slug",
                "priority": "not-a-number",
                "parent_commits": "not-a-sha",
                "content": "",
            },
        )
        assert resp.status_code == 400
        # User input preserved.
        assert "good-slug" in resp.text
        assert "not-a-number" in resp.text
        assert "not-a-sha" in resp.text
        # Field errors surfaced.
        assert "priority must be a number" in resp.text
        assert "content markdown is required" in resp.text
        # No store mutation.
        assert store.list_ideas() == []
        assert store.read_task("t-bad").state == "claimed"


class TestMultiRow:
    def test_add_row_full_page_returns_form_with_one_more_empty_row(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        """No-JS path: add_row returns the full draft page with one more row."""
        store.create_ideation_task("t-add")
        token = get_csrf(signed_in_client)
        signed_in_client.post(
            "/ideator/t-add/claim",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        resp = signed_in_client.post(
            "/ideator/t-add/add_row",
            data={
                "csrf_token": token,
                "slug": "feat-1",
                "priority": "1.0",
                "parent_commits": "a" * 40,
                "content": "first idea content",
            },
        )
        assert resp.status_code == 200
        # First row's input is preserved.
        assert "feat-1" in resp.text
        assert "first idea content" in resp.text
        # The form now has 2 rows — count textareas as a per-row marker.
        assert resp.text.count('name="content"') == 2
        # Full page: includes the base layout (topbar nav).
        assert "<header" in resp.text

    def test_add_row_htmx_no_session_returns_hx_redirect_to_signin(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        """HTMX request with no session must use HX-Redirect, not 303 to a full page.

        HTMX follows 3xx transparently and swaps the redirected
        response into the configured target — for the add_row
        button (``hx-target="#idea-rows"``) that would dump
        ``<html>...</html>`` of the sign-in page into the rows
        container. Returning ``HX-Redirect`` instead makes htmx
        do a real navigation.
        """
        store.create_ideation_task("t-noses")
        resp = client.post(
            "/ideator/t-noses/add_row",
            data={"slug": "x"},
            headers={"hx-request": "true"},
        )
        assert resp.status_code == 204
        assert resp.headers.get("hx-redirect") == "/signin"
        # Empty body is what htmx wants for HX-Redirect — it must NOT
        # contain a <header> or anything else that could end up
        # swapped into #idea-rows.
        assert resp.text == ""

    def test_add_row_htmx_missing_claim_returns_hx_redirect(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        """HTMX request without an active claim must HX-Redirect to /ideator/."""
        store.create_ideation_task("t-noclaim")
        token = get_csrf(signed_in_client)
        # Note: we never call /ideator/{id}/claim, so the in-memory
        # _CLAIMS dict has no entry for this (csrf, task) pair.
        resp = signed_in_client.post(
            "/ideator/t-noclaim/add_row",
            data={"csrf_token": token, "slug": "x"},
            headers={"hx-request": "true"},
        )
        assert resp.status_code == 204
        loc = resp.headers.get("hx-redirect", "")
        assert loc.startswith("/ideator/?banner=claim+missing")
        assert resp.text == ""

    def test_add_row_htmx_csrf_failure_sets_hx_reswap_none(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        """HTMX add_row with bad CSRF must NOT have its body swapped into the page."""
        store.create_ideation_task("t-csrf")
        token = get_csrf(signed_in_client)
        # Claim first so the missing-claim branch isn't what fires.
        signed_in_client.post(
            "/ideator/t-csrf/claim",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        resp = signed_in_client.post(
            "/ideator/t-csrf/add_row",
            data={"csrf_token": "wrong"},
            headers={"hx-request": "true"},
        )
        assert resp.status_code == 403
        # `HX-Reswap: none` tells htmx to not swap the response body
        # into the configured target. Without this, the "CSRF token
        # missing or invalid" string would land in #idea-rows.
        assert resp.headers.get("hx-reswap") == "none"

    def test_add_row_no_js_no_session_303_to_signin(
        self, client: TestClient, store: InMemoryStore
    ) -> None:
        """The no-JS path keeps the conventional 303-to-/signin behavior."""
        store.create_ideation_task("t-nojs")
        resp = client.post(
            "/ideator/t-nojs/add_row",
            data={"slug": "x"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/signin"

    def test_add_row_htmx_returns_partial_only(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        """JS path: add_row with HX-Request: true returns only the row partial."""
        store.create_ideation_task("t-htmx")
        token = get_csrf(signed_in_client)
        signed_in_client.post(
            "/ideator/t-htmx/claim",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        resp = signed_in_client.post(
            "/ideator/t-htmx/add_row",
            data={
                "csrf_token": token,
                "slug": "feat-1",
                "priority": "1.0",
                "parent_commits": "a" * 40,
                "content": "first",
            },
            headers={"hx-request": "true"},
        )
        assert resp.status_code == 200
        # Just the new (empty) row partial — no <header>, no <html>.
        assert "<header" not in resp.text
        assert "<html" not in resp.text
        # Contains exactly one textarea (the new row's content field).
        assert resp.text.count('name="content"') == 1
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

        store.create_ideation_task("t-skip")
        token = get_csrf(signed_in_client)
        signed_in_client.post(
            "/ideator/t-skip/claim",
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
                ("content", "real idea"),
                ("slug", ""),
                ("priority", "1.0"),
                ("parent_commits", ""),
                ("content", ""),
            ]
        )
        resp = signed_in_client.post(
            "/ideator/t-skip/submit",
            content=body,
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        assert resp.status_code == 200
        ready = store.list_ideas(state="ready")
        assert len(ready) == 1
        assert ready[0].slug == "feat-only"

    def test_all_blank_rejected(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        """If every row is blank, submit re-renders with a form-level error."""
        store.create_ideation_task("t-all-blank")
        token = get_csrf(signed_in_client)
        signed_in_client.post(
            "/ideator/t-all-blank/claim",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        resp = signed_in_client.post(
            "/ideator/t-all-blank/submit",
            data={
                "csrf_token": token,
                "status": "success",
                "slug": "",
                "priority": "1.0",
                "parent_commits": "",
                "content": "",
            },
        )
        assert resp.status_code == 400
        assert "at least one idea row must be filled in" in resp.text


class TestPerSessionClaimIsolation:
    def test_other_session_with_same_worker_id_cannot_use_first_session_claim(
        self, app, store: InMemoryStore
    ) -> None:
        """Two browser sessions with the same worker_id must not share claims."""
        store.create_ideation_task("t-iso")
        with TestClient(app) as a:
            a.post("/signin", follow_redirects=False)
            csrf_a = get_csrf(a)
            resp = a.post(
                "/ideator/t-iso/claim",
                data={"csrf_token": csrf_a},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            # First session can see its own draft form.
            resp = a.get("/ideator/t-iso/draft")
            assert resp.status_code == 200

        # Second session — fresh sign-in (fresh CSRF) — cannot reach
        # the draft page, which means it cannot reuse the first
        # session's claim.
        with TestClient(app) as b:
            b.post("/signin", follow_redirects=False)
            resp = b.get("/ideator/t-iso/draft", follow_redirects=False)
            assert resp.status_code == 303
            assert "claim+missing" in resp.headers["location"]


class TestDefinitiveSubmitErrors:
    """Each chapter-07 §7 error name surfaces on the orphan page.

    Note on operational semantics: by the time Phase 3 submit runs,
    Phase 1 has already created ideas (drafting) and Phase 2 has
    marked them ready. A definitive wire error at submit means those
    ideas are orphaned — the form-input preservation that
    applies to *validation* errors does not apply here, because the
    work product (the ideas themselves) is what needs recovery,
    not the form inputs. The orphan page lists the orphaned
    idea IDs and surfaces the canonical error type so an
    operator can decide whether to reclaim the ideation task or
    garbage-collect.
    """

    def _setup_claim(
        self, client: TestClient, store: InMemoryStore, task_id: str
    ) -> str:
        store.create_ideation_task(task_id)
        token = get_csrf(client)
        resp = client.post(
            f"/ideator/{task_id}/claim",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        return token

    def _submit(
        self, client: TestClient, task_id: str, csrf: str
    ):
        return client.post(
            f"/ideator/{task_id}/submit",
            data={
                "csrf_token": csrf,
                "status": "success",
                "slug": "feat-x",
                "priority": "1.0",
                "parent_commits": "a" * 40,
                "content": "content",
            },
        )

    def test_wrong_token_lands_on_orphan_with_banner(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        monkeypatch,
    ) -> None:
        from eden_storage import NotClaimed

        token = self._setup_claim(signed_in_client, store, "t-wt")

        def fail(*args, **kwargs):
            raise NotClaimed("wrong token")

        monkeypatch.setattr(store, "submit", fail)
        resp = self._submit(signed_in_client, "t-wt", token)
        assert resp.status_code == 502
        assert "eden://error/not-claimed" in resp.text
        assert len(store.list_ideas(state="ready")) == 1

    def test_illegal_transition_feeds_readback(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        monkeypatch,
    ) -> None:
        """``IllegalTransition`` no longer short-circuits to orphan.

        It now falls through to read-back. With the underlying task
        still claimed by us, read-back surfaces a transport-flavored
        banner naming ``IllegalTransition`` rather than the raw
        wire-error string. The "we won" sub-case is covered in the
        IllegalTransitionReadback class below.
        """
        from eden_storage import IllegalTransition

        token = self._setup_claim(signed_in_client, store, "t-it")

        def fail(*args, **kwargs):
            raise IllegalTransition("not in claimed state")

        monkeypatch.setattr(store, "submit", fail)
        resp = self._submit(signed_in_client, "t-it", token)
        assert resp.status_code == 502
        # Old behavior asserted "eden://error/illegal-transition" but
        # the new read-back-fed contract surfaces the exception class
        # name in the transport banner.
        assert "IllegalTransition" in resp.text
        assert "eden://error/illegal-transition" not in resp.text

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
            raise InvalidPrecondition("variant wrong status")

        monkeypatch.setattr(store, "submit", fail)
        resp = self._submit(signed_in_client, "t-ip", token)
        assert resp.status_code == 502
        assert "eden://error/invalid-precondition" in resp.text


class TestIllegalTransitionReadback:
    """``IllegalTransition`` feeds read-back, not a definitive orphan.

    Distinguishes "we won; orchestrator already terminalized" from
    "we lost; different submission won." The chunk-9c executor
    module shipped this lens via §K-2 of the chunk-9d plan; the
    ideator gets the same fix here.
    """

    def _setup_claim(
        self, client: TestClient, store: InMemoryStore, task_id: str
    ) -> str:
        store.create_ideation_task(task_id)
        token = get_csrf(client)
        resp = client.post(
            f"/ideator/{task_id}/claim",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        return token

    def _submit(
        self, client: TestClient, task_id: str, csrf: str
    ):
        return client.post(
            f"/ideator/{task_id}/submit",
            data={
                "csrf_token": csrf,
                "status": "success",
                "slug": "feat-rb",
                "priority": "1.0",
                "parent_commits": "a" * 40,
                "content": "content",
            },
        )

    def test_completed_with_equivalent_prior_renders_success(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        monkeypatch,
    ) -> None:
        """We won: submit committed, orchestrator terminalized, retry hit IllegalTransition."""
        from eden_storage import IllegalTransition

        csrf = self._setup_claim(signed_in_client, store, "t-itb")
        original_submit = store.submit

        def fake_submit(task_id_, token, submission):
            # Commit, accept (state -> completed), then raise
            # IllegalTransition simulating "response was lost; we
            # retried and saw a state the store rejects."
            original_submit(task_id_, token, submission)
            store.accept(task_id_)
            raise IllegalTransition("simulated post-terminalization retry")

        monkeypatch.setattr(store, "submit", fake_submit)
        resp = self._submit(signed_in_client, "t-itb", csrf)
        # SUCCESS, not orphan — this is the ideator-side §K-2 fix.
        assert resp.status_code == 200
        assert "feat-rb" in resp.text or "submitted" in resp.text.lower()

    def test_completed_with_non_equivalent_prior_renders_orphan(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        monkeypatch,
    ) -> None:
        """Different submission won the race; read-back finds non-equivalent."""
        from eden_contracts import IdeationPayload, IdeationTask
        from eden_storage import IdeaSubmission, IllegalTransition

        csrf = self._setup_claim(signed_in_client, store, "t-itc")
        synthetic_task = IdeationTask(
            task_id="t-itc",
            kind="ideation",
            state="completed",
            payload=IdeationPayload(experiment_id=store.experiment_id),
            created_at="2026-04-24T11:00:00.000Z",
            updated_at="2026-04-24T13:00:00.000Z",
        )
        non_equiv_prior = IdeaSubmission(
            status="success", idea_ids=("idea-other-worker",)
        )

        def fake_submit(*a, **k):
            raise IllegalTransition("task already terminal")

        monkeypatch.setattr(store, "submit", fake_submit)
        monkeypatch.setattr(store, "read_task", lambda tid: synthetic_task)
        monkeypatch.setattr(store, "read_submission", lambda tid: non_equiv_prior)

        resp = self._submit(signed_in_client, "t-itc", csrf)
        assert resp.status_code == 502
        assert "conflicting-resubmission" in resp.text

    def test_terminal_state_with_no_submission_renders_transport_invariant(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        monkeypatch,
    ) -> None:
        """`read_submission() is None` on a terminal task is invariant violation, not conflict."""
        from eden_contracts import IdeationPayload, IdeationTask
        from eden_storage import IllegalTransition

        csrf = self._setup_claim(signed_in_client, store, "t-inv")
        synthetic_task = IdeationTask(
            task_id="t-inv",
            kind="ideation",
            state="completed",
            payload=IdeationPayload(experiment_id=store.experiment_id),
            created_at="2026-04-24T11:00:00.000Z",
            updated_at="2026-04-24T13:00:00.000Z",
        )

        def fake_submit(*a, **k):
            raise IllegalTransition("task already terminal")

        monkeypatch.setattr(store, "submit", fake_submit)
        monkeypatch.setattr(store, "read_task", lambda tid: synthetic_task)
        monkeypatch.setattr(store, "read_submission", lambda tid: None)

        resp = self._submit(signed_in_client, "t-inv", csrf)
        assert resp.status_code == 502
        assert "store invariant violation" in resp.text
        # Must NOT be classified as conflict.
        assert "conflicting-resubmission" not in resp.text

    def test_pending_after_illegal_transition_distinct_from_claimed(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        monkeypatch,
    ) -> None:
        """state==pending after IllegalTransition mentions reclaim, not generic transport."""
        from eden_storage import IllegalTransition

        csrf = self._setup_claim(signed_in_client, store, "t-pen")
        # Reclaim the task ourselves so its state is pending; the
        # patched submit will raise IllegalTransition and read-back
        # observes state==pending.
        store.reclaim("t-pen", "operator")

        def fake_submit(*a, **k):
            raise IllegalTransition("task no longer claimed")

        monkeypatch.setattr(store, "submit", fake_submit)
        resp = self._submit(signed_in_client, "t-pen", csrf)
        assert resp.status_code == 502
        # New banner explicitly mentions reclaim, distinguishing it
        # from a transport failure where the claim is still ours.
        assert "task reclaimed" in resp.text

    def test_claimed_with_other_token_renders_wrong_token(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        monkeypatch,
    ) -> None:
        """state==claimed but a different worker now holds the claim."""
        from eden_storage import IllegalTransition

        csrf = self._setup_claim(signed_in_client, store, "t-wt2")
        # Reclaim then re-claim under a different worker so the
        # task is in state==claimed with a non-matching token.
        store.reclaim("t-wt2", "operator")
        store.claim("t-wt2", store._test_worker_ids["another-worker"])

        def fake_submit(*a, **k):
            raise IllegalTransition("token does not match")

        monkeypatch.setattr(store, "submit", fake_submit)
        resp = self._submit(signed_in_client, "t-wt2", csrf)
        assert resp.status_code == 502
        assert "eden://error/not-claimed" in resp.text


class TestRetryBeforeOrphan:
    def test_transport_exception_retries_then_orphan(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        monkeypatch,
    ) -> None:
        """Raw transport exceptions (httpx-style, not DispatchError) are retried."""
        store.create_ideation_task("t-trans")
        token = get_csrf(signed_in_client)
        signed_in_client.post(
            "/ideator/t-trans/claim",
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
            "/ideator/t-trans/submit",
            data={
                "csrf_token": token,
                "status": "success",
                "slug": "feat-trans",
                "priority": "1.0",
                "parent_commits": "a" * 40,
                "content": "content",
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
            store_factory=_one_experiment_factory(store),
            experiment_id=EXPERIMENT_ID,
            experiment_config=_config(),
            worker_id=web_ui_worker_id(store),
            session_secret=SESSION_SECRET,
            claim_ttl_seconds=60,  # 1 minute
            artifacts_dir=artifacts_dir,
            secure_cookies=False,
            now=fake_now,
        )
        with TestClient(app) as c:
            c.post("/signin", follow_redirects=False)
            store.create_ideation_task("t-strand")
            csrf = get_csrf(c)
            c.post(
                "/ideator/t-strand/claim",
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


class TestDraftBufferReHydration:
    """Regression test for #2 — typed draft input survives navigation (resolved in commit c30cafa).

    Before the fix, a user who hit a validation error and then
    navigated away (back / refresh / nav click) and returned to
    GET /ideator/<task>/draft saw an empty form. The claim was
    still active in ``_CLAIMS`` but the typed values were gone.
    The fix buffers form_state on every POST that carries idea
    rows; the GET handler re-hydrates from that buffer.
    """

    def test_validation_error_then_get_re_renders_typed_input(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        store.create_ideation_task("t-rehydrate")
        token = get_csrf(signed_in_client)
        signed_in_client.post(
            "/ideator/t-rehydrate/claim",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        # Submit something that fails validation (bad priority, bad SHA).
        bad = signed_in_client.post(
            "/ideator/t-rehydrate/submit",
            data={
                "csrf_token": token,
                "status": "success",
                "slug": "feat-keep-me",
                "priority": "not-a-number",
                "parent_commits": "not-a-sha",
                "content": "## why\n\ncontent text",
            },
        )
        assert bad.status_code == 400
        # Sanity: the immediate re-render preserves input.
        assert "feat-keep-me" in bad.text

        # Simulate the user navigating away and coming back via GET.
        resp = signed_in_client.get("/ideator/t-rehydrate/draft")
        assert resp.status_code == 200
        # Typed values must come back instead of an empty form.
        assert "feat-keep-me" in resp.text
        assert "not-a-number" in resp.text
        assert "not-a-sha" in resp.text
        assert "content text" in resp.text

    def test_add_row_buffers_typed_state_for_subsequent_get(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        store.create_ideation_task("t-add-buf")
        token = get_csrf(signed_in_client)
        signed_in_client.post(
            "/ideator/t-add-buf/claim",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        # User typed one row, clicked "add row" (no-JS path).
        resp = signed_in_client.post(
            "/ideator/t-add-buf/add_row",
            data={
                "csrf_token": token,
                "slug": "first-feature",
                "priority": "2.0",
                "parent_commits": "a" * 40,
                "content": "first idea content",
            },
        )
        assert resp.status_code == 200
        # User then refreshes / navigates back to the draft URL.
        get_resp = signed_in_client.get("/ideator/t-add-buf/draft")
        assert get_resp.status_code == 200
        assert "first-feature" in get_resp.text
        assert "first idea content" in get_resp.text

    def test_buffer_cleared_after_successful_submit(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        store.create_ideation_task("t-clear-buf")
        token = get_csrf(signed_in_client)
        signed_in_client.post(
            "/ideator/t-clear-buf/claim",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        # Trigger a buffer write via add_row so the buffer is non-empty.
        signed_in_client.post(
            "/ideator/t-clear-buf/add_row",
            data={
                "csrf_token": token,
                "slug": "before-success",
                "priority": "1.0",
                "parent_commits": "a" * 40,
                "content": "before success",
            },
        )
        # Successful submit clears the buffer. The simplest probe: a
        # fresh ideation task on the same session must NOT inherit the
        # prior buffer's state when GET'd.
        ok = signed_in_client.post(
            "/ideator/t-clear-buf/submit",
            data={
                "csrf_token": token,
                "status": "success",
                "slug": "before-success",
                "priority": "1.0",
                "parent_commits": "a" * 40,
                "content": "before success",
            },
        )
        assert ok.status_code == 200
        # Re-claim the same task_id is not allowed (terminal); but
        # we can directly check the module-level buffer dict to
        # confirm the per-(session,task) entry is gone.
        from eden_web_ui.routes.ideator import _DRAFT_BUFFERS

        # Find any entry whose task_id is t-clear-buf.
        leftover = [k for k in _DRAFT_BUFFERS if k[1] == "t-clear-buf"]
        assert leftover == []
