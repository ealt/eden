"""Helpers for the task-store-server: load config, open store, build app.

Factored out of ``cli.py`` so unit tests can exercise them without
booting uvicorn.
"""

from __future__ import annotations

from eden_contracts import ExperimentConfig
from eden_service_common import load_experiment_config
from eden_storage import InMemoryStore, PostgresStore, SqliteStore, Store
from eden_wire import make_app
from fastapi import FastAPI

# Re-exported so callers that previously imported ``load_experiment_config``
# from this module continue to work; eden_service_common is the single
# source of truth.
__all__ = ["build_app", "build_store", "load_experiment_config"]


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
            evaluation_schema=config.evaluation_schema,
        )
    if store_url.startswith("postgresql://") or store_url.startswith("postgres://"):
        return PostgresStore(
            experiment_id=experiment_id,
            dsn=store_url,
            evaluation_schema=config.evaluation_schema,
        )
    if store_url.startswith("sqlite:///"):
        path = store_url[len("sqlite:///") :]
    else:
        # Bare-path compatibility — interpret as SQLite filesystem path.
        path = store_url
    return SqliteStore(
        experiment_id=experiment_id,
        path=path,
        evaluation_schema=config.evaluation_schema,
    )


def build_app(
    *,
    store: Store,
    admin_token: str | None = None,
    subscribe_timeout: float = 30.0,
) -> FastAPI:
    """Build the FastAPI app that wraps ``store`` with the §13 auth middleware.

    ``admin_token`` is the deployment's ``EDEN_ADMIN_TOKEN``; when
    ``None``, the server runs unauthenticated (test / in-process
    posture). 12a-1 wave 3 replaced the pre-12a-1 ``shared_token``
    parameter with this admin / per-worker scheme; the storage-side
    ``Store.verify_worker_credential`` handles per-worker bearers.
    """
    return make_app(
        store,
        admin_token=admin_token,
        subscribe_timeout=subscribe_timeout,
    )
