"""Helpers for the task-store-server: load config, open store, build app.

Factored out of ``cli.py`` so unit tests can exercise them without
booting uvicorn.
"""

from __future__ import annotations

import logging
from pathlib import Path

from eden_contracts import ExperimentConfig
from eden_service_common import load_experiment_config
from eden_storage import (
    InMemoryStore,
    PostgresStore,
    SqliteStore,
    Store,
    ensure_readonly_role,
)
from eden_wire import make_app
from fastapi import FastAPI

log = logging.getLogger(__name__)

# Re-exported so callers that previously imported ``load_experiment_config``
# from this module continue to work; eden_service_common is the single
# source of truth.
__all__ = [
    "build_app",
    "build_store",
    "load_experiment_config",
    "provision_readonly",
]


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
    artifacts_dir: Path | str | None = None,
) -> FastAPI:
    """Build the FastAPI app that wraps ``store`` with the §13 auth middleware.

    ``admin_token`` is the deployment's ``EDEN_ADMIN_TOKEN``; when
    ``None``, the server runs unauthenticated (test / in-process
    posture). 12a-1 wave 3 replaced the pre-12a-1 ``shared_token``
    parameter with this admin / per-worker scheme; the storage-side
    ``Store.verify_worker_credential`` handles per-worker bearers.

    ``artifacts_dir``, when non-``None``, enables the 12a-1f
    reference-only artifact-serving route at
    ``/_reference/experiments/{experiment_id}/artifacts/{path:path}``.
    The route is always mounted regardless; when ``artifacts_dir`` is
    ``None`` every request returns 503 with a closed-vocabulary
    reference-error type.
    """
    return make_app(
        store,
        admin_token=admin_token,
        subscribe_timeout=subscribe_timeout,
        artifacts_dir=artifacts_dir,
    )


def provision_readonly(store: Store, *, password: str) -> None:
    """Run :func:`ensure_readonly_role` against ``store`` if Postgres-backed.

    No-op (with a WARN log) for in-memory / SQLite backends — the
    readonly substrate is Postgres-specific per 12a-1f §D.3.b.
    Idempotent on re-call (the provisioning helper itself is
    REVOKE-then-GRANT).
    """
    if not isinstance(store, PostgresStore):
        log.warning(
            "readonly_password_set_but_backend_unsupported: backend=%s; "
            "the eden_readonly Postgres role is meaningless against "
            "non-Postgres backends. Either switch the store to Postgres "
            "or remove --readonly-password.",
            type(store).__name__,
        )
        return
    ensure_readonly_role(store._conn, password=password)
