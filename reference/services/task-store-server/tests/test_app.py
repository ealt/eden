"""Unit tests for the task-store-server app-building helpers."""

from __future__ import annotations

from pathlib import Path

import pytest
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
    assert config.evaluation_schema.root == {"score": "real"}
    assert config.parallel_variants >= 1


def test_build_store_memory_backend() -> None:
    config = load_experiment_config(FIXTURE_CONFIG)
    store = build_store(
        store_url=":memory:",
        experiment_id="exp_0123456789abcdefghjkmnpqrs",
        config=config,
    )
    assert isinstance(store, InMemoryStore)


def test_build_store_sqlite_backend_bare_path(tmp_path: Path) -> None:
    config = load_experiment_config(FIXTURE_CONFIG)
    db_path = str(tmp_path / "eden.sqlite")
    store = build_store(
        store_url=db_path,
        experiment_id="exp_0123456789abcdefghjkmnpqrs",
        config=config,
    )
    assert isinstance(store, SqliteStore)
    store.close()


def test_build_store_sqlite_backend_url_scheme(tmp_path: Path) -> None:
    config = load_experiment_config(FIXTURE_CONFIG)
    db_path = tmp_path / "eden.sqlite"
    store = build_store(
        store_url=f"sqlite:///{db_path}",
        experiment_id="exp_0123456789abcdefghjkmnpqrs",
        config=config,
    )
    assert isinstance(store, SqliteStore)
    store.close()


def test_build_app_no_admin_token_admits_anonymous() -> None:
    config = load_experiment_config(FIXTURE_CONFIG)
    store = build_store(
        store_url=":memory:", experiment_id="exp_0123456789abcdefghjkmnpqrs", config=config
    )
    app = build_app(store=store)
    client = TestClient(app)
    resp = client.get(
        "/v0/experiments/exp_0123456789abcdefghjkmnpqrs/events",
        headers={"X-Eden-Experiment-Id": "exp_0123456789abcdefghjkmnpqrs"},
    )
    assert resp.status_code == 200


def test_build_app_with_admin_token_rejects_unauthenticated() -> None:
    config = load_experiment_config(FIXTURE_CONFIG)
    store = build_store(
        store_url=":memory:", experiment_id="exp_0123456789abcdefghjkmnpqrs", config=config
    )
    app = build_app(store=store, admin_token="secret")
    client = TestClient(app)
    resp = client.get(
        "/v0/experiments/exp_0123456789abcdefghjkmnpqrs/events",
        headers={"X-Eden-Experiment-Id": "exp_0123456789abcdefghjkmnpqrs"},
    )
    assert resp.status_code == 401
    assert resp.json()["type"] == "eden://error/unauthorized"


def test_build_app_with_admin_token_admits_admin_bearer() -> None:
    config = load_experiment_config(FIXTURE_CONFIG)
    store = build_store(
        store_url=":memory:", experiment_id="exp_0123456789abcdefghjkmnpqrs", config=config
    )
    app = build_app(store=store, admin_token="secret")
    client = TestClient(app)
    resp = client.get(
        "/v0/experiments/exp_0123456789abcdefghjkmnpqrs/events",
        headers={
            "X-Eden-Experiment-Id": "exp_0123456789abcdefghjkmnpqrs",
            "Authorization": "Bearer admin:secret",
        },
    )
    assert resp.status_code == 200


def test_build_app_rejects_blob_dir_inside_artifacts_dir(tmp_path: Path) -> None:
    # Issue #166: the §16 blob dir must not overlap --artifacts-dir (served
    # unauthenticated by the legacy /_reference route → ACL bypass).
    config = load_experiment_config(FIXTURE_CONFIG)
    store = build_store(store_url=":memory:", experiment_id="exp-1", config=config)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    with pytest.raises(SystemExit, match="must not overlap"):
        build_app(store=store, artifacts_dir=artifacts, artifact_blob_dir=artifacts / "blobs")


def test_build_app_accepts_disjoint_blob_dir(tmp_path: Path) -> None:
    config = load_experiment_config(FIXTURE_CONFIG)
    store = build_store(store_url=":memory:", experiment_id="exp-1", config=config)
    app = build_app(
        store=store,
        artifacts_dir=tmp_path / "artifacts",
        artifact_blob_dir=tmp_path / "blobs",
    )
    assert app is not None
