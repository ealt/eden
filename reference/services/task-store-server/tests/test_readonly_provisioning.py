"""Tests for the task-store-server's --readonly-password wiring.

Covers ``provision_readonly``'s behavior across backends:

- Non-Postgres backends (memory / sqlite) log a WARN and no-op.
- Postgres backend delegates to ``ensure_readonly_role``
  (detailed coverage in
  ``reference/packages/eden-storage/tests/test_postgres_readonly.py``).

The CLI flag itself is verified via ``parse_args``.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from eden_storage import SqliteStore
from eden_task_store_server import build_store, load_experiment_config
from eden_task_store_server.app import provision_readonly
from eden_task_store_server.cli import parse_args

FIXTURE_CONFIG = (
    Path(__file__).resolve().parents[4]
    / "tests"
    / "fixtures"
    / "experiment"
    / ".eden"
    / "config.yaml"
)


def _make_memory_store():
    config = load_experiment_config(FIXTURE_CONFIG)
    return build_store(
        store_url=":memory:", experiment_id="exp_0123456789abcdefghjkmnpqrs", config=config
    )


def _make_sqlite_store(tmp_path: Path):
    config = load_experiment_config(FIXTURE_CONFIG)
    return build_store(
        store_url=f"sqlite:///{tmp_path / 'eden.sqlite'}",
        experiment_id="exp_0123456789abcdefghjkmnpqrs",
        config=config,
    )


def test_memory_backend_warns_and_no_ops(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = _make_memory_store()
    with caplog.at_level(logging.WARNING):
        provision_readonly(store, password="ignored")
    assert any(
        "readonly_password_set_but_backend_unsupported" in r.getMessage()
        for r in caplog.records
    )


def test_sqlite_backend_warns_and_no_ops(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = _make_sqlite_store(tmp_path)
    assert isinstance(store, SqliteStore)
    try:
        with caplog.at_level(logging.WARNING):
            provision_readonly(store, password="ignored")
    finally:
        store.close()
    assert any(
        "readonly_password_set_but_backend_unsupported" in r.getMessage()
        for r in caplog.records
    )


def test_cli_parses_readonly_password() -> None:
    args = parse_args(
        [
            "--store-url",
            ":memory:",
            "--experiment-id",
            "exp_0123456789abcdefghjkmnpqrs",
            "--experiment-config",
            str(FIXTURE_CONFIG),
            "--readonly-password",
            "super-secret",
        ]
    )
    assert args.readonly_password == "super-secret"


def test_cli_readonly_password_default_is_none() -> None:
    args = parse_args(
        [
            "--store-url",
            ":memory:",
            "--experiment-id",
            "exp_0123456789abcdefghjkmnpqrs",
            "--experiment-config",
            str(FIXTURE_CONFIG),
        ]
    )
    assert args.readonly_password is None
