"""Helpers for the task-store-server: load config, open store, build app.

Factored out of ``cli.py`` so unit tests can exercise them without
booting uvicorn.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from eden_contracts import ExperimentConfig
from eden_storage import InMemoryStore, SqliteStore, Store
from eden_wire import make_app
from fastapi import FastAPI


def load_experiment_config(path: str | Path) -> ExperimentConfig:
    """Parse an experiment YAML file into an :class:`ExperimentConfig`."""
    with Path(path).open() as f:
        data = yaml.safe_load(f)
    return ExperimentConfig.model_validate(data)


def build_store(
    *,
    db_path: str,
    experiment_id: str,
    config: ExperimentConfig,
) -> Store:
    """Open a ``Store`` backend selected by ``db_path``.

    ``db_path == ':memory:'`` yields an :class:`InMemoryStore`; any
    other value is interpreted as a SQLite file path.
    """
    if db_path == ":memory:":
        return InMemoryStore(
            experiment_id=experiment_id,
            metrics_schema=config.metrics_schema,
        )
    return SqliteStore(
        experiment_id=experiment_id,
        path=db_path,
        metrics_schema=config.metrics_schema,
    )


def build_app(
    *,
    store: Store,
    shared_token: str | None = None,
    subscribe_timeout: float = 30.0,
) -> FastAPI:
    """Build the FastAPI app that wraps ``store`` with optional auth."""
    return make_app(
        store,
        shared_token=shared_token,
        subscribe_timeout=subscribe_timeout,
    )
