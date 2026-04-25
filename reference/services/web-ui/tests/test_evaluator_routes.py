"""Per-route validation for the evaluator module."""

from __future__ import annotations

from urllib.parse import urlencode

import pytest
from conftest import (
    get_csrf,
    get_evaluate_submission,
    seed_evaluate_task,
)
from eden_storage import InMemoryStore
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


class TestList:
    def test_list_empty(self, signed_in_client: TestClient) -> None:
        resp = signed_in_client.get("/evaluator/")
        assert resp.status_code == 200
        assert "no pending evaluate tasks" in resp.text

    def test_list_shows_pending(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        eval_id, _, _ = seed_evaluate_task(store)
        resp = signed_in_client.get("/evaluator/")
        assert resp.status_code == 200
        assert eval_id in resp.text

    def test_banner_query_param_renders(
        self, signed_in_client: TestClient
    ) -> None:
        resp = signed_in_client.get("/evaluator/?banner=hello+world")
        assert "hello world" in resp.text


class TestClaim:
    def test_claim_rejects_missing_csrf(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        eval_id, _, _ = seed_evaluate_task(store)
        resp = _post_form(
            signed_in_client, f"/evaluator/{eval_id}/claim", []
        )
        assert resp.status_code == 403

    def test_claim_rejects_wrong_csrf(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        eval_id, _, _ = seed_evaluate_task(store)
        resp = _post_form(
            signed_in_client,
            f"/evaluator/{eval_id}/claim",
            [("csrf_token", "tampered")],
        )
        assert resp.status_code == 403

    def test_claim_redirects_to_draft(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        eval_id, trial_id, _ = seed_evaluate_task(store)
        csrf = get_csrf(signed_in_client)
        resp = _post_form(
            signed_in_client,
            f"/evaluator/{eval_id}/claim",
            [("csrf_token", csrf)],
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/evaluator/{eval_id}/draft"
        # _CLAIMS contains (csrf, task_id) -> (token, trial_id)
        keys = list(evaluator_routes._CLAIMS.keys())
        assert len(keys) == 1
        _, recorded_trial = evaluator_routes._CLAIMS[keys[0]]
        assert recorded_trial == trial_id

    def test_claim_already_claimed_surfaces_banner(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        eval_id, _, _ = seed_evaluate_task(store)
        # Claim the task directly so the route's claim sees an
        # IllegalTransition.
        store.claim(eval_id, "other-w")
        csrf = get_csrf(signed_in_client)
        resp = _post_form(
            signed_in_client,
            f"/evaluator/{eval_id}/claim",
            [("csrf_token", csrf)],
        )
        assert resp.status_code == 303
        assert "/evaluator/?banner=" in resp.headers["location"]
        assert "illegal-transition" in resp.headers["location"]

    def test_claim_transport_failure_surfaces_banner(
        self,
        signed_in_client: TestClient,
        store: InMemoryStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        eval_id, _, _ = seed_evaluate_task(store)
        # Patch store.claim to simulate the StoreClient transport
        # surface raising a generic exception. The route should
        # redirect to / with a banner instead of 500-ing.
        def boom(*a, **k):
            raise RuntimeError("network glitch")

        monkeypatch.setattr(store, "claim", boom)
        csrf = get_csrf(signed_in_client)
        resp = _post_form(
            signed_in_client,
            f"/evaluator/{eval_id}/claim",
            [("csrf_token", csrf)],
        )
        assert resp.status_code == 303
        assert "task-store+transport+failure" in resp.headers["location"]

    def test_claim_unknown_task_surfaces_banner(
        self, signed_in_client: TestClient
    ) -> None:
        csrf = get_csrf(signed_in_client)
        resp = _post_form(
            signed_in_client,
            "/evaluator/unknown-task/claim",
            [("csrf_token", csrf)],
        )
        assert resp.status_code == 303
        assert "/evaluator/?banner=" in resp.headers["location"]


class TestDraftRender:
    def test_draft_without_claim_redirects(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        eval_id, _, _ = seed_evaluate_task(store)
        resp = signed_in_client.get(
            f"/evaluator/{eval_id}/draft", follow_redirects=False
        )
        assert resp.status_code == 303
        assert "claim+missing+from+session" in resp.headers["location"]

    def test_draft_renders_metric_inputs_per_schema(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        # Fixture metrics_schema has "score" : "real" — make sure
        # the right input type is generated.
        eval_id, _, _ = seed_evaluate_task(store)
        csrf = get_csrf(signed_in_client)
        _post_form(
            signed_in_client,
            f"/evaluator/{eval_id}/claim",
            [("csrf_token", csrf)],
        )
        resp = signed_in_client.get(f"/evaluator/{eval_id}/draft")
        assert resp.status_code == 200
        assert 'name="metric.score"' in resp.text
        # Real -> step="any"
        assert 'step="any"' in resp.text


class TestSubmitValidation:
    def _claim(self, signed_in_client: TestClient, eval_id: str) -> str:
        csrf = get_csrf(signed_in_client)
        _post_form(
            signed_in_client,
            f"/evaluator/{eval_id}/claim",
            [("csrf_token", csrf)],
        )
        return csrf

    def test_status_outside_allowed_set(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        eval_id, _, _ = seed_evaluate_task(store)
        csrf = self._claim(signed_in_client, eval_id)
        resp = _post_form(
            signed_in_client,
            f"/evaluator/{eval_id}/submit",
            [
                ("csrf_token", csrf),
                ("status", "garbage"),
                ("metric.score", "0.9"),
            ],
        )
        assert resp.status_code == 400
        assert "status must be one of" in resp.text

    def test_success_with_zero_metrics_rejected(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        eval_id, _, _ = seed_evaluate_task(store)
        csrf = self._claim(signed_in_client, eval_id)
        resp = _post_form(
            signed_in_client,
            f"/evaluator/{eval_id}/submit",
            [("csrf_token", csrf), ("status", "success")],
        )
        assert resp.status_code == 400
        assert "at least one metric value" in resp.text

    def test_error_with_zero_metrics_accepted(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        eval_id, trial_id, _ = seed_evaluate_task(store)
        csrf = self._claim(signed_in_client, eval_id)
        resp = _post_form(
            signed_in_client,
            f"/evaluator/{eval_id}/submit",
            [("csrf_token", csrf), ("status", "error")],
        )
        assert resp.status_code == 200
        assert "submitted" in resp.text
        recorded = get_evaluate_submission(store, eval_id)
        assert recorded.status == "error"
        assert recorded.metrics is None
        assert recorded.trial_id == trial_id

    def test_eval_error_with_zero_metrics_accepted(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        eval_id, _, _ = seed_evaluate_task(store)
        csrf = self._claim(signed_in_client, eval_id)
        resp = _post_form(
            signed_in_client,
            f"/evaluator/{eval_id}/submit",
            [("csrf_token", csrf), ("status", "eval_error")],
        )
        assert resp.status_code == 200
        recorded = get_evaluate_submission(store, eval_id)
        assert recorded.status == "eval_error"

    def test_real_metric_nan_rejected(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        eval_id, _, _ = seed_evaluate_task(store)
        csrf = self._claim(signed_in_client, eval_id)
        resp = _post_form(
            signed_in_client,
            f"/evaluator/{eval_id}/submit",
            [
                ("csrf_token", csrf),
                ("status", "success"),
                ("metric.score", "nan"),
            ],
        )
        assert resp.status_code == 400
        assert "value is not finite" in resp.text

    def test_unknown_metric_key_rejected(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        # Hand-crafted POST: the body carries a `metric.ghost` field
        # outside the experiment's metrics_schema. The route must
        # forward every `metric.*` to the parser so the unknown key
        # is rejected with a 400 + field error.
        eval_id, _, _ = seed_evaluate_task(store)
        csrf = self._claim(signed_in_client, eval_id)
        resp = _post_form(
            signed_in_client,
            f"/evaluator/{eval_id}/submit",
            [
                ("csrf_token", csrf),
                ("status", "success"),
                ("metric.score", "0.9"),
                ("metric.ghost", "1.0"),
            ],
        )
        assert resp.status_code == 400
        assert "not in schema" in resp.text
        # And submit must not have been called.
        assert store.read_submission(eval_id) is None

    def test_forged_trial_id_form_field_ignored(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        eval_id, trial_id, _ = seed_evaluate_task(store)
        csrf = self._claim(signed_in_client, eval_id)
        resp = _post_form(
            signed_in_client,
            f"/evaluator/{eval_id}/submit",
            [
                ("csrf_token", csrf),
                ("status", "success"),
                ("metric.score", "0.7"),
                ("trial_id", "trial-attacker-controls-this"),
            ],
        )
        assert resp.status_code == 200
        recorded = get_evaluate_submission(store, eval_id)
        assert recorded.trial_id == trial_id
        assert recorded.trial_id != "trial-attacker-controls-this"

    def test_success_records_metric(
        self, signed_in_client: TestClient, store: InMemoryStore
    ) -> None:
        eval_id, trial_id, _ = seed_evaluate_task(store)
        csrf = self._claim(signed_in_client, eval_id)
        resp = _post_form(
            signed_in_client,
            f"/evaluator/{eval_id}/submit",
            [
                ("csrf_token", csrf),
                ("status", "success"),
                ("metric.score", "0.875"),
                ("artifacts_uri", "https://logs.example/run/42"),
            ],
        )
        assert resp.status_code == 200
        recorded = get_evaluate_submission(store, eval_id)
        assert recorded.status == "success"
        assert recorded.trial_id == trial_id
        assert recorded.metrics == {"score": 0.875}
        assert recorded.artifacts_uri == "https://logs.example/run/42"


class TestIntegerWireForm:
    """Per-spec §1.3 wire-legal integer forms."""

    def test_integer_dot_zero_accepted(self) -> None:
        # Use a schema with an integer metric for this test.
        from eden_contracts import MetricsSchema
        from eden_web_ui.forms import parse_evaluate_form

        schema = MetricsSchema.model_validate({"count": "integer"})
        draft, errors = parse_evaluate_form(
            metrics_schema=schema,
            status_raw="success",
            metric_inputs={"count": "1.0"},
            artifacts_uri_raw="",
        )
        assert errors.by_row == {}
        assert draft is not None
        assert draft.metrics == {"count": 1}
        assert isinstance(draft.metrics["count"], int)

    def test_integer_one_point_five_rejected(self) -> None:
        from eden_contracts import MetricsSchema
        from eden_web_ui.forms import parse_evaluate_form

        schema = MetricsSchema.model_validate({"count": "integer"})
        draft, errors = parse_evaluate_form(
            metrics_schema=schema,
            status_raw="success",
            metric_inputs={"count": "1.5"},
            artifacts_uri_raw="",
        )
        assert draft is None
        assert errors.by_row.get(0, {}).get("count") == "value is not an integer"
