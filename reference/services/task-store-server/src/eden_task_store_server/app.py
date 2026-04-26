"""Helpers for the task-store-server: load config, open store, build app.

Factored out of ``cli.py`` so unit tests can exercise them without
booting uvicorn.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from eden_contracts import ExperimentConfig
from eden_storage import InMemoryStore, PostgresStore, SqliteStore, Store
from eden_wire import make_app
from fastapi import FastAPI


def load_experiment_config(path: str | Path) -> ExperimentConfig:
    """Parse an experiment YAML file into an :class:`ExperimentConfig`."""
    with Path(path).open() as f:
        data = yaml.safe_load(f)
    return ExperimentConfig.model_validate(data)


def build_store(
    *,
    store_url: str,
    experiment_id: str,
    config: ExperimentConfig,
) -> Store:
    """Open a ``Store`` backend selected by URL scheme.

    ``store_url`` dispatch:

    * ``:memory:`` → :class:`InMemoryStore` (non-durable).
    * ``sqlite:///<path>`` → :class:`SqliteStore` at ``<path>``.
    * ``postgresql://…`` (or ``postgres://…``) → :class:`PostgresStore`.
    * Bare path (no scheme) → :class:`SqliteStore` for compatibility
      with pre-Phase-10 callers.
    """
    if store_url == ":memory:":
        return InMemoryStore(
            experiment_id=experiment_id,
            metrics_schema=config.metrics_schema,
        )
    if store_url.startswith("postgresql://") or store_url.startswith("postgres://"):
        return PostgresStore(
            experiment_id=experiment_id,
            dsn=store_url,
            metrics_schema=config.metrics_schema,
        )
    if store_url.startswith("sqlite:///"):
        path = store_url[len("sqlite:///") :]
    else:
        # Bare-path compatibility — interpret as SQLite filesystem path.
        path = store_url
    return SqliteStore(
        experiment_id=experiment_id,
        path=path,
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
