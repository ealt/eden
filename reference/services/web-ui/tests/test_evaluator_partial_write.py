"""Partial-write recovery tests for the evaluator module.

Pins each branch of §C-recovery from the Phase 9d plan, including
the new ``IllegalTransition`` → read-back arm (sub-cases I-a / I-b
/ I-c) that distinguishes the chunk-9d evaluator from the chunk-9c
implementer (which short-circuits ``IllegalTransition``).

Sub-cases:

- A   transport, server committed, still ``submitted`` — read-back
      finds equivalent → success page.
- A'  transport, server committed, then terminalized to ``failed``
      via a different submission — read-back finds non-equivalent
      → conflict page.
- B   transport, never committed (claim still ours) — orphan auto.
- C   read_task itself fails — orphan transport.
- D   read_submission returns None for a terminal task — orphan
      transport.
- E   ``WrongToken`` short-circuits — orphan auto, no retries.
- F   ``ConflictingResubmission`` short-circuits — orphan conflict.
- G   read_task finds task back in ``pending`` — orphan auto.
- H   ``InvalidPrecondition`` short-circuits to a form re-render.
- I-a ``IllegalTransition`` + read-back state==pending — orphan auto.
- I-b ``IllegalTransition`` + read-back finds equivalent prior
      submission — success page (the lens that flips chunk-9c's
      mis-classification to correct).
- I-c ``IllegalTransition`` + read-back finds non-equivalent
      submission — orphan conflict.
"""

from __future__ import annotations

from urllib.parse import urlencode

import pytest
from conftest import (
    get_csrf,
    seed_evaluate_task,
)
from eden_storage import (
    ConflictingResubmission,
    EvaluateSubmission,
    InMemoryStore,
    InvalidPrecondition,
    WrongToken,
)
from eden_web_ui.routes import evaluator as evaluator_routes
from fastapi.testclient import TestClient


def _post_form(client: TestClient, url: str, fields: list[tuple[str, str]]):
    body = urlencode(fields)
    return client.post(
        url,
        content=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )


@pytest.fixture(autouse=True)
def _clear_claims():
    evaluator_routes._CLAIMS.clear()
    yield
    evaluator_routes._CLAIMS.clear()


def _claim(
    client: TestClient, store: InMemoryStore, slug: str = "demo"
) -> tuple[str, str, str]:
    """Seed a task, claim it, and return (task_id, trial_id, csrf)."""
    eval_id, trial_id, _ = seed_evaluate_task(store, slug=slug)
    csrf = get_csrf(client)
    resp = _post_form(
        client, f"/evaluator/{eval_id}/claim", [("csrf_token", csrf)]
    )
    assert resp.status_code == 303
    return eval_id, trial_id, csrf


def _success_form(csrf: str, score: float = 0.9) -> list[tuple[str, str]]:
    return [
        ("csrf_token", csrf),
        ("status", "success"),
        ("metric.score", str(score)),
    ]


class TestSubCaseA:
    """Server committed, response lost; read-back equivalent → success."""

    def test_commit_and_raise_then_readback_finds_equivalent(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        eval_id, _, csrf = _claim(signed_in_client, store)
        original_submit = store.submit

        def commit_and_raise(task_id: str, token: str, sub):
            original_submit(task_id, token, sub)
            raise RuntimeError("net glitch")

        monkeypatch.setattr(store, "submit", commit_and_raise)
        resp = _post_form(
            signed_in_client, f"/evaluator/{eval_id}/submit", _success_form(csrf)
        )
        # Submit committed, retries exhausted, read-back finds
        # state==submitted with equivalent prior → success page.
        assert resp.status_code == 200
        assert "submitted" in resp.text


class TestSubCaseAPrime:
    """Server committed; then a different submission terminalized; conflict."""

    def test_commit_and_raise_then_terminalized_non_equivalent(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        eval_id, trial_id, csrf = _claim(signed_in_client, store)
        original_submit = store.submit
        calls = {"n": 0}

        def commit_then_terminalize_then_raise(task_id: str, token: str, sub):
            calls["n"] += 1
            if calls["n"] == 1:
                # First call: drive a *different* submission to a
                # terminal state via the legitimate API. We can't do
                # this with the same token, so reclaim and re-claim
                # with a different worker.
                store.reclaim(task_id, "operator")
                other = store.claim(task_id, "evaluator-other")
                # The "other" worker submits a different metric.
                original_submit(
                    task_id,
                    other.token,
                    EvaluateSubmission(
                        status="success", trial_id=trial_id, metrics={"score": 0.123}
                    ),
                )
                store.accept(task_id)
                raise RuntimeError("net glitch")
            raise RuntimeError("net glitch")

        monkeypatch.setattr(store, "submit", commit_then_terminalize_then_raise)
        resp = _post_form(
            signed_in_client,
            f"/evaluator/{eval_id}/submit",
            _success_form(csrf, score=0.42),
        )
        assert resp.status_code == 502
        assert "operator intervention" in resp.text


class TestSubCaseB:
    """Transport, never committed, claim still ours → orphan auto."""

    def test_claim_still_ours(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        eval_id, _, csrf = _claim(signed_in_client, store)

        def boom(*args, **kwargs):
            raise RuntimeError("network error")

        monkeypatch.setattr(store, "submit", boom)
        resp = _post_form(
            signed_in_client, f"/evaluator/{eval_id}/submit", _success_form(csrf)
        )
        assert resp.status_code == 502
        assert "auto-recovers" in resp.text


class TestSubCaseC:
    """read_task fails during read-back → orphan transport."""

    def test_read_task_fails(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        eval_id, _, csrf = _claim(signed_in_client, store)

        def submit_boom(*args, **kwargs):
            raise RuntimeError("net")

        # The route also calls store.read_trial / store.read_proposal
        # before the submit; those are hit before we patch read_task.
        # Patch read_task only AFTER the route's pre-submit reads run.
        # Easiest: wrap submit so the *first* call patches read_task,
        # then raises.
        original_read_task = store.read_task

        def submit_arm_then_boom(task_id: str, token: str, sub):
            calls = {"n": 0}

            def read_task_fail(*a, **k):
                calls["n"] += 1
                raise RuntimeError("read fail")

            monkeypatch.setattr(store, "read_task", read_task_fail)
            raise RuntimeError("net")

        monkeypatch.setattr(store, "submit", submit_arm_then_boom)
        resp = _post_form(
            signed_in_client, f"/evaluator/{eval_id}/submit", _success_form(csrf)
        )
        # Restore so the rest of the test infra works.
        monkeypatch.setattr(store, "read_task", original_read_task)
        assert resp.status_code == 502
        assert "indeterminate" in resp.text


class TestSubCaseD:
    """read_submission returns None on terminal task → orphan transport."""

    def test_read_submission_invariant_violation(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        eval_id, _, csrf = _claim(signed_in_client, store)

        def submit_boom(*args, **kwargs):
            raise RuntimeError("net")

        # Make read_task return a synthetic submitted state, but
        # read_submission returns None.
        from eden_contracts import EvaluatePayload, EvaluateTask, TaskClaim

        synthetic_task = EvaluateTask(
            task_id=eval_id,
            kind="evaluate",
            state="submitted",
            payload=EvaluatePayload(trial_id="trial-eval"),
            created_at="2026-04-24T11:00:00.000Z",
            updated_at="2026-04-24T12:00:00.000Z",
            claim=TaskClaim(
                token="t" * 16,
                worker_id="ui-w",
                claimed_at="2026-04-24T11:30:00.000Z",
            ),
        )

        monkeypatch.setattr(store, "submit", submit_boom)
        monkeypatch.setattr(store, "read_task", lambda tid: synthetic_task)
        monkeypatch.setattr(store, "read_submission", lambda tid: None)

        resp = _post_form(
            signed_in_client, f"/evaluator/{eval_id}/submit", _success_form(csrf)
        )
        assert resp.status_code == 502
        assert "store invariant violation" in resp.text


class TestSubCaseE:
    """WrongToken short-circuits to orphan auto with no retries."""

    def test_wrong_token_short_circuit(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        eval_id, _, csrf = _claim(signed_in_client, store)
        calls = {"n": 0}

        def raise_wrong_token(*a, **k):
            calls["n"] += 1
            raise WrongToken("token mismatch")

        monkeypatch.setattr(store, "submit", raise_wrong_token)
        resp = _post_form(
            signed_in_client, f"/evaluator/{eval_id}/submit", _success_form(csrf)
        )
        assert resp.status_code == 502
        assert "auto-recovers" in resp.text
        # No retries: only called once.
        assert calls["n"] == 1


class TestSubCaseF:
    """ConflictingResubmission short-circuits to orphan conflict."""

    def test_conflicting_resubmission_short_circuit(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        eval_id, _, csrf = _claim(signed_in_client, store)
        calls = {"n": 0}

        def raise_conflict(*a, **k):
            calls["n"] += 1
            raise ConflictingResubmission("different payload")

        monkeypatch.setattr(store, "submit", raise_conflict)
        resp = _post_form(
            signed_in_client, f"/evaluator/{eval_id}/submit", _success_form(csrf)
        )
        assert resp.status_code == 502
        assert "operator intervention" in resp.text
        assert calls["n"] == 1


class TestSubCaseG:
    """read_task finds state==pending → orphan auto, task reclaimed."""

    def test_pending_after_transport(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        eval_id, _, csrf = _claim(signed_in_client, store)

        def submit_boom(*a, **k):
            raise RuntimeError("net")

        monkeypatch.setattr(store, "submit", submit_boom)

        # Simulate the sweeper having run: reclaim the task ourselves.
        # The submit will already be patched, but we reclaim via the
        # store directly so read_task returns state==pending.
        store.reclaim(eval_id, "operator")

        resp = _post_form(
            signed_in_client, f"/evaluator/{eval_id}/submit", _success_form(csrf)
        )
        assert resp.status_code == 502
        assert "auto-recovers" in resp.text


class TestSubCaseH:
    """InvalidPrecondition → form re-render with banner, NOT orphan."""

    def test_invalid_precondition_re_renders_form(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        eval_id, _, csrf = _claim(signed_in_client, store)
        calls = {"n": 0}

        def raise_invalid(*a, **k):
            calls["n"] += 1
            raise InvalidPrecondition("metrics drift")

        monkeypatch.setattr(store, "submit", raise_invalid)
        resp = _post_form(
            signed_in_client, f"/evaluator/{eval_id}/submit", _success_form(csrf)
        )
        # Form re-render: 400, not 502, with the wire-error banner.
        assert resp.status_code == 400
        assert "eden://error/invalid-precondition" in resp.text
        # No retries.
        assert calls["n"] == 1


class TestSubCaseIa:
    """IllegalTransition + state==pending → orphan auto via read-back."""

    def test_illegal_transition_pending(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        eval_id, _, csrf = _claim(signed_in_client, store)

        # Reclaim the task so its state is pending; submit will then
        # raise IllegalTransition. The route's read-back should find
        # state==pending and render orphan auto.
        store.reclaim(eval_id, "operator")
        resp = _post_form(
            signed_in_client, f"/evaluator/{eval_id}/submit", _success_form(csrf)
        )
        assert resp.status_code == 502
        assert "auto-recovers" in resp.text


class TestSubCaseIb:
    """IllegalTransition + state==completed with our equivalent submission → success.

    This is the lens that distinguishes chunk-9d's correct read-back
    from chunk-9c's mis-classification (§K-2 of the plan).
    """

    def test_illegal_transition_terminalized_with_equivalent(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        eval_id, trial_id, csrf = _claim(signed_in_client, store)

        # Drive the task to completed under a *different* claim, but
        # with an *equivalent* submission to what the route is about
        # to send. We must do this without using our claim's token,
        # so reclaim, re-claim, and submit equivalent payload.
        equivalent = EvaluateSubmission(
            status="success", trial_id=trial_id, metrics={"score": 0.9}
        )
        store.reclaim(eval_id, "operator")
        other = store.claim(eval_id, "evaluator-other")
        store.submit(eval_id, other.token, equivalent)
        store.accept(eval_id)
        # Task is now in completed; our subsequent submit will hit
        # IllegalTransition. Read-back finds equivalent → success.
        resp = _post_form(
            signed_in_client, f"/evaluator/{eval_id}/submit", _success_form(csrf, score=0.9)
        )
        assert resp.status_code == 200
        assert "submitted" in resp.text


class TestSubCaseIc:
    """IllegalTransition + state==completed with different submission → conflict."""

    def test_illegal_transition_terminalized_non_equivalent(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        eval_id, trial_id, csrf = _claim(signed_in_client, store)
        store.reclaim(eval_id, "operator")
        other = store.claim(eval_id, "evaluator-other")
        store.submit(
            eval_id,
            other.token,
            EvaluateSubmission(
                status="success", trial_id=trial_id, metrics={"score": 0.111}
            ),
        )
        store.accept(eval_id)
        resp = _post_form(
            signed_in_client,
            f"/evaluator/{eval_id}/submit",
            _success_form(csrf, score=0.999),
        )
        assert resp.status_code == 502
        assert "operator intervention" in resp.text
