"""Cross-request flow tests for the evaluator module.

Covers the full claim → draft → submit happy path, validation
recovery, status=eval_error, sweeper-driven stranded-claim
recovery, and conflict via a different submission winning the
race.

Failure-recovery and orphan paths live in
``test_evaluator_partial_write.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

import pytest
from conftest import (
    EXPERIMENT_ID,
    SESSION_SECRET,
    WORKER_ID,
    _config,
    get_csrf,
    get_evaluate_submission,
    seed_evaluate_task,
)
from eden_dispatch import sweep_expired_claims
from eden_storage import EvaluateSubmission, InMemoryStore
from eden_web_ui import make_app
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


class TestHappyPath:
    def test_claim_draft_submit(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        artifacts_dir: Path,
    ) -> None:
        eval_id, trial_id, _ = seed_evaluate_task(
            store, artifacts_dir=artifacts_dir, artifact_text="rationale text"
        )
        csrf = get_csrf(signed_in_client)
        # Claim
        resp = _post_form(
            signed_in_client,
            f"/evaluator/{eval_id}/claim",
            [("csrf_token", csrf)],
        )
        assert resp.status_code == 303
        # Draft
        draft_resp = signed_in_client.get(f"/evaluator/{eval_id}/draft")
        assert draft_resp.status_code == 200
        assert "rationale text" in draft_resp.text
        assert trial_id in draft_resp.text
        # Submit
        submit_resp = _post_form(
            signed_in_client,
            f"/evaluator/{eval_id}/submit",
            [
                ("csrf_token", csrf),
                ("status", "success"),
                ("metric.score", "0.42"),
            ],
        )
        assert submit_resp.status_code == 200
        assert "submitted" in submit_resp.text
        # _CLAIMS cleared.
        assert evaluator_routes._CLAIMS == {}
        # Task is in submitted; submission has the metric.
        recorded = get_evaluate_submission(store, eval_id)
        assert recorded.status == "success"
        assert recorded.metrics == {"score": 0.42}


class TestValidationRecovery:
    def test_re_render_preserves_other_inputs(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        eval_id, _, _ = seed_evaluate_task(store)
        csrf = get_csrf(signed_in_client)
        _post_form(
            signed_in_client,
            f"/evaluator/{eval_id}/claim",
            [("csrf_token", csrf)],
        )
        # Initial submit: success but no metric.
        resp = _post_form(
            signed_in_client,
            f"/evaluator/{eval_id}/submit",
            [
                ("csrf_token", csrf),
                ("status", "success"),
                ("artifacts_uri", "https://logs.example/abc"),
            ],
        )
        assert resp.status_code == 400
        # The artifacts_uri input value is preserved on re-render.
        assert "https://logs.example/abc" in resp.text
        # Fix the metric and resubmit.
        ok = _post_form(
            signed_in_client,
            f"/evaluator/{eval_id}/submit",
            [
                ("csrf_token", csrf),
                ("status", "success"),
                ("metric.score", "0.5"),
                ("artifacts_uri", "https://logs.example/abc"),
            ],
        )
        assert ok.status_code == 200


class TestEvalErrorPath:
    def test_eval_error_with_partial_metrics(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        eval_id, _, _ = seed_evaluate_task(store)
        csrf = get_csrf(signed_in_client)
        _post_form(
            signed_in_client,
            f"/evaluator/{eval_id}/claim",
            [("csrf_token", csrf)],
        )
        resp = _post_form(
            signed_in_client,
            f"/evaluator/{eval_id}/submit",
            [
                ("csrf_token", csrf),
                ("status", "eval_error"),
                ("metric.score", "0.1"),
            ],
        )
        assert resp.status_code == 200
        recorded = get_evaluate_submission(store, eval_id)
        assert recorded.status == "eval_error"
        assert recorded.metrics == {"score": 0.1}


class TestStrandedClaimRecovery:
    def test_sweeper_reclaims_then_submit_orphans_auto(
        self,
        artifacts_dir: Path,
        store: InMemoryStore,
    ) -> None:
        # App with a claim_ttl_seconds=1 so the sweeper will pick
        # up an abandoned tab.
        app = make_app(
            store=store,
            experiment_id=EXPERIMENT_ID,
            experiment_config=_config(),
            worker_id=WORKER_ID,
            session_secret=SESSION_SECRET,
            claim_ttl_seconds=1,
            artifacts_dir=artifacts_dir,
            now=lambda: datetime(2026, 4, 24, 12, 0, tzinfo=UTC),
        )
        with TestClient(app) as client:
            client.post("/signin", follow_redirects=False)
            eval_id, _, _ = seed_evaluate_task(store)
            csrf = get_csrf(client)
            _post_form(
                client,
                f"/evaluator/{eval_id}/claim",
                [("csrf_token", csrf)],
            )
            # Run the sweeper at a time well past the TTL.
            future = datetime(2026, 4, 24, 13, 0, tzinfo=UTC)
            n = sweep_expired_claims(store, now=future)
            assert n == 1
            # Now the route's submit observes a reclaimed task.
            resp = _post_form(
                client,
                f"/evaluator/{eval_id}/submit",
                [
                    ("csrf_token", csrf),
                    ("status", "success"),
                    ("metric.score", "0.9"),
                ],
            )
            assert resp.status_code == 502
            assert "auto-recovers" in resp.text


class TestConflictPath:
    def test_different_submission_wins_race(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
    ) -> None:
        eval_id, trial_id, _ = seed_evaluate_task(store)
        csrf = get_csrf(signed_in_client)
        _post_form(
            signed_in_client,
            f"/evaluator/{eval_id}/claim",
            [("csrf_token", csrf)],
        )
        # A different worker reclaims and submits a different
        # metric value.
        store.reclaim(eval_id, "operator")
        other = store.claim(eval_id, "evaluator-other")
        store.submit(
            eval_id,
            other.token,
            EvaluateSubmission(
                status="success",
                trial_id=trial_id,
                metrics={"score": 0.123},
            ),
        )
        # Our session's submit hits WrongToken because our claim is gone.
        resp = _post_form(
            signed_in_client,
            f"/evaluator/{eval_id}/submit",
            [
                ("csrf_token", csrf),
                ("status", "success"),
                ("metric.score", "0.42"),
            ],
        )
        assert resp.status_code == 502
        # WrongToken short-circuits to recovery_kind=auto.
        assert "auto-recovers" in resp.text
