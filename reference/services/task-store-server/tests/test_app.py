"""Unit tests for the task-store-server app-building helpers."""

from __future__ import annotations

from pathlib import Path

from eden_storage import InMemoryStore, SqliteStore
from eden_task_store_server import build_app, build_store, load_experiment_config
from fastapi.testclient import TestClient

FIXTURE_CONFIG = (
    Path(__file__).resolve().parents[4]
    / "tests"
    / "fixtures"
    / "experiment"
    / ".eden"
    / "config.yaml"
)


def test_load_experiment_config_parses_fixture() -> None:
    config = load_experiment_config(FIXTURE_CONFIG)
    assert config.metrics_schema.root == {"score": "real"}
    assert config.parallel_trials >= 1


def test_build_store_memory_backend() -> None:
    config = load_experiment_config(FIXTURE_CONFIG)
    store = build_store(
        db_path=":memory:",
        experiment_id="exp-1",
        config=config,
    )
    assert isinstance(store, InMemoryStore)


def test_build_store_sqlite_backend(tmp_path: Path) -> None:
    config = load_experiment_config(FIXTURE_CONFIG)
    db_path = str(tmp_path / "eden.sqlite")
    store = build_store(
        db_path=db_path,
        experiment_id="exp-1",
        config=config,
    )
    assert isinstance(store, SqliteStore)
    store.close()


def test_build_app_no_shared_token_admits_anonymous() -> None:
    config = load_experiment_config(FIXTURE_CONFIG)
    store = build_store(db_path=":memory:", experiment_id="exp-1", config=config)
    app = build_app(store=store)
    client = TestClient(app)
    resp = client.get(
        "/v0/experiments/exp-1/events",
        headers={"X-Eden-Experiment-Id": "exp-1"},
    )
    assert resp.status_code == 200


def test_build_app_with_shared_token_rejects_unauthenticated() -> None:
    config = load_experiment_config(FIXTURE_CONFIG)
    store = build_store(db_path=":memory:", experiment_id="exp-1", config=config)
    app = build_app(store=store, shared_token="secret")
    client = TestClient(app)
    resp = client.get(
        "/v0/experiments/exp-1/events",
        headers={"X-Eden-Experiment-Id": "exp-1"},
    )
    assert resp.status_code == 401
    assert resp.json()["type"] == "eden://reference-error/unauthorized"


def test_build_app_with_shared_token_admits_authenticated() -> None:
    config = load_experiment_config(FIXTURE_CONFIG)
    store = build_store(db_path=":memory:", experiment_id="exp-1", config=config)
    app = build_app(store=store, shared_token="secret")
    client = TestClient(app)
    resp = client.get(
        "/v0/experiments/exp-1/events",
        headers={
            "X-Eden-Experiment-Id": "exp-1",
            "Authorization": "Bearer secret",
        },
    )
    assert resp.status_code == 200
