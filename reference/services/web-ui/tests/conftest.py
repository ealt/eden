"""Shared fixtures for eden-web-ui tests."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from eden_contracts import ExperimentConfig
from eden_storage import InMemoryStore
from eden_task_store_server import load_experiment_config
from eden_web_ui import make_app
from fastapi import FastAPI
from fastapi.testclient import TestClient

EXPERIMENT_ID = "exp-web-ui"
SESSION_SECRET = "test-session-secret-padding-padding-padding"
SHARED_TOKEN = "test-bearer-do-not-leak"
WORKER_ID = "ui-w"

_FIXTURE_CONFIG = (
    Path(__file__).resolve().parents[4]
    / "tests"
    / "fixtures"
    / "experiment"
    / ".eden"
    / "config.yaml"
)


def _config() -> ExperimentConfig:
    return load_experiment_config(str(_FIXTURE_CONFIG))


def _now() -> datetime:
    """Deterministic frozen now for tests."""
    return datetime(2026, 4, 24, 12, 0, tzinfo=UTC)


@pytest.fixture
def store() -> InMemoryStore:
    cfg = _config()
    return InMemoryStore(experiment_id=EXPERIMENT_ID, metrics_schema=cfg.metrics_schema)


@pytest.fixture
def artifacts_dir(tmp_path: Path) -> Path:
    out = tmp_path / "artifacts"
    out.mkdir()
    return out


@pytest.fixture
def app(store: InMemoryStore, artifacts_dir: Path) -> FastAPI:
    return make_app(
        store=store,
        experiment_id=EXPERIMENT_ID,
        experiment_config=_config(),
        worker_id=WORKER_ID,
        session_secret=SESSION_SECRET,
        claim_ttl_seconds=3600,
        artifacts_dir=artifacts_dir,
        secure_cookies=False,
        now=_now,
    )


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def signed_in_client(client: TestClient) -> TestClient:
    """A client that has POSTed /signin and holds a fresh session cookie."""
    resp = client.post("/signin", follow_redirects=False)
    assert resp.status_code == 303
    return client


def get_csrf(client: TestClient) -> str:
    """Decode the active session cookie and return its CSRF token.

    Used by tests as the value to send for ``csrf_token`` form fields.
    """
    from eden_web_ui.sessions import SESSION_COOKIE_NAME, SessionCodec

    raw = client.cookies.get(SESSION_COOKIE_NAME)
    assert raw is not None, "client has no session cookie"
    session = SessionCodec(SESSION_SECRET).decode(raw)
    assert session is not None
    return session.csrf
