"""Tests for the 12a-1f --artifacts-dir CLI flag + app wiring.

Covers:

- ``build_app(artifacts_dir=...)`` exposes the artifact route.
- ``build_app(artifacts_dir=None)`` still mounts the route but
  every request returns 503 (always-mounted contract).
- CLI parses ``--artifacts-dir`` correctly.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from eden_task_store_server import build_app, build_store, load_experiment_config
from eden_task_store_server.cli import parse_args
from fastapi.testclient import TestClient

FIXTURE_CONFIG = (
    Path(__file__).resolve().parents[4]
    / "tests"
    / "fixtures"
    / "experiment"
    / ".eden"
    / "config.yaml"
)


def _make_app_with_artifacts(artifacts_dir: Path | None):
    config = load_experiment_config(FIXTURE_CONFIG)
    store = build_store(
        store_url=":memory:", experiment_id="exp-1", config=config
    )
    return build_app(store=store, artifacts_dir=artifacts_dir)


def test_artifacts_dir_exposes_route(tmp_path: Path) -> None:
    art = tmp_path / "artifacts"
    art.mkdir()
    (art / "hello.md").write_bytes(b"hi")
    app = _make_app_with_artifacts(art)
    client = TestClient(app)
    resp = client.get("/_reference/experiments/exp-1/artifacts/hello.md")
    assert resp.status_code == 200
    assert resp.content == b"hi"


def test_artifacts_dir_none_returns_503() -> None:
    """Route is always mounted; without --artifacts-dir → 503."""
    app = _make_app_with_artifacts(None)
    client = TestClient(app)
    resp = client.get("/_reference/experiments/exp-1/artifacts/anything")
    assert resp.status_code == 503
    assert (
        resp.json()["type"]
        == "eden://reference-error/artifact-serving-disabled"
    )


def test_cli_parses_artifacts_dir() -> None:
    args = parse_args(
        [
            "--store-url",
            ":memory:",
            "--experiment-id",
            "exp-1",
            "--experiment-config",
            str(FIXTURE_CONFIG),
            "--artifacts-dir",
            "/var/lib/eden/artifacts",
        ]
    )
    assert args.artifacts_dir == "/var/lib/eden/artifacts"


def test_cli_artifacts_dir_default_is_none() -> None:
    args = parse_args(
        [
            "--store-url",
            ":memory:",
            "--experiment-id",
            "exp-1",
            "--experiment-config",
            str(FIXTURE_CONFIG),
        ]
    )
    assert args.artifacts_dir is None


@pytest.mark.parametrize("path", ["", "../etc/passwd"])
def test_artifacts_route_security_properties_preserved_via_build_app(
    tmp_path: Path, path: str
) -> None:
    """Sanity: build_app's route wiring preserves the eden_wire
    handler's security properties. Detailed coverage of the
    descriptor walk lives in test_artifact_route.py.
    """
    art = tmp_path / "artifacts"
    art.mkdir()
    app = _make_app_with_artifacts(art)
    client = TestClient(app)
    resp = client.get(f"/_reference/experiments/exp-1/artifacts/{path}")
    # Either malformed-path 400 or missing 404, never 200.
    assert resp.status_code in (400, 404)
