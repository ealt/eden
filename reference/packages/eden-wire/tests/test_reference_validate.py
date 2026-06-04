"""Wire-level smoke tests for the two ``/_reference/`` validate routes.

``GET .../validate-terminal`` and ``POST .../validate/evaluation`` move
into ``eden_wire.routers.reference`` under F-3 (issue #115). They had no
direct wire-level coverage before (the store methods are exercised by the
schema-parity suite, but not the HTTP routes), so this file adds one
happy-path + one bad-input test per route.
"""

from __future__ import annotations

import pytest
from eden_contracts import EvaluationSchema
from eden_storage import InMemoryStore
from eden_wire import StoreClient, make_app
from fastapi.testclient import TestClient

EXPERIMENT_ID = "exp_zp0q3v6xsnk0jf9hfb54m73626"


@pytest.fixture
def client() -> TestClient:
    schema = EvaluationSchema.model_validate({"loss": "real", "acc": "real"})
    store = InMemoryStore(EXPERIMENT_ID, evaluation_schema=schema)
    return TestClient(make_app(store), base_url="http://wire.test")


@pytest.fixture
def store_client(client: TestClient) -> StoreClient:
    return StoreClient("http://wire.test", EXPERIMENT_ID, client=client)


def _ref_url(suffix: str) -> str:
    return f"/_reference/experiments/{EXPERIMENT_ID}/{suffix}"


def _headers() -> dict[str, str]:
    return {"X-Eden-Experiment-Id": EXPERIMENT_ID}


class TestValidateTerminal:
    def test_existing_non_submitted_task_accepts(
        self, client: TestClient, store_client: StoreClient
    ) -> None:
        # A freshly-created task is not in `submitted` state, so
        # validate_terminal short-circuits to ("accept", None).
        store_client.create_ideation_task("vt-1")
        resp = client.get(_ref_url("tasks/vt-1/validate-terminal"), headers=_headers())
        assert resp.status_code == 200
        body = resp.json()
        assert body["decision"] == "accept"
        assert body.get("reason") is None

    def test_missing_task_is_not_found(self, client: TestClient) -> None:
        resp = client.get(_ref_url("tasks/nope/validate-terminal"), headers=_headers())
        assert resp.status_code == 404
        assert resp.json()["type"] == "eden://error/not-found"


class TestValidateEvaluation:
    def test_schema_valid_evaluation_returns_204(self, client: TestClient) -> None:
        resp = client.post(
            _ref_url("validate/evaluation"),
            json={"evaluation": {"loss": 0.5, "acc": 0.9}},
            headers=_headers(),
        )
        assert resp.status_code == 204

    def test_off_schema_key_is_invalid_precondition(self, client: TestClient) -> None:
        resp = client.post(
            _ref_url("validate/evaluation"),
            json={"evaluation": {"not_a_metric": 1.0}},
            headers=_headers(),
        )
        assert resp.status_code == 409
        assert resp.json()["type"] == "eden://error/invalid-precondition"


def test_reference_routes_require_experiment_header(client: TestClient) -> None:
    """The §1.3 header invariant applies to the reference helpers too.

    The header param defaults to ``None``, so a missing header reaches
    ``check_experiment`` and surfaces as the mismatch envelope (400),
    not FastAPI's own 422 missing-parameter error.
    """
    resp = client.get(_ref_url("tasks/vt-1/validate-terminal"))
    assert resp.status_code == 400
    assert resp.json()["type"] == "eden://error/experiment-id-mismatch"
