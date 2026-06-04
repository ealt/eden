"""Helpers for the task-store-server: load config, open store, build app.

Factored out of ``cli.py`` so unit tests can exercise them without
booting uvicorn.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from eden_contracts import ExperimentConfig
from eden_git import GitError, GitRepo
from eden_service_common import load_experiment_config
from eden_storage import (
    FileArtifactBackend,
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
    "build_tree_resolver",
    "load_experiment_config",
    "provision_readonly",
]


def build_tree_resolver(repo_path: str | Path) -> Callable[[str], str | None]:
    """Return a tree-of-commit resolver backed by the bare repo at ``repo_path``.

    Wired into ``_StoreBase`` to enforce the
    ``spec/v0/03-roles.md`` §3.3 non-no-op variant invariant. Returns
    ``None`` for SHAs that don't resolve (graceful degradation when a
    parent_commit names a SHA absent from the local repo) and never
    raises (the Store treats raises as "resolver unavailable for this
    SHA" and falls back to the SHA-equality fast path).

    The resolver is intentionally I/O-free against any remote: it
    queries only the local bare repo. The Store's
    ``_validate_non_no_op_variant`` runs inside the per-operation
    Store transaction (holding the SQLite / Postgres write lock), so
    a network fetch here would block every other task-store request
    behind one submit on a slow / unreachable Forgejo. Population of
    the local clone is the responsibility of a separate refresh path
    (operator-driven or a future background helper) — this resolver
    is the server-side, defense-in-depth backstop; the canonical
    enforcement point for the §3.3 rule is the executor's pre-submit
    check against its own clone (see ``_is_no_op_variant`` in the
    executor host's subprocess_mode).
    """
    repo = GitRepo(Path(repo_path))

    def _resolve(sha: str) -> str | None:
        try:
            return repo.commit_tree_sha(sha)
        except (GitError, OSError):
            return None

    return _resolve


def build_store(
    *,
    store_url: str,
    experiment_id: str,
    config: ExperimentConfig,
    repo_path: str | Path | None = None,
    base_commit_sha: str | None = None,
) -> Store:
    """Open a ``Store`` backend selected by URL scheme.

    ``store_url`` dispatch:

    * ``:memory:`` → :class:`InMemoryStore` (non-durable).
    * ``sqlite:///<path>`` → :class:`SqliteStore` at ``<path>``.
    * ``postgresql://…`` (or ``postgres://…``) → :class:`PostgresStore`.
    * Bare path (no scheme) → :class:`SqliteStore` for compatibility
      with pre-Phase-10 callers.

    ``repo_path``: when set, wires a tree-of-commit resolver into the
    store so it can enforce the spec/v0/03-roles.md §3.3 non-no-op
    variant invariant via real git tree comparison (the SHA-equality
    fast path runs regardless). The resolver only reads the local
    bare repo — it does not fetch from any remote (see
    :func:`build_tree_resolver` for the rationale).
    """
    tree_resolver = (
        build_tree_resolver(repo_path) if repo_path is not None else None
    )
    if store_url == ":memory:":
        return InMemoryStore(
            experiment_id=experiment_id,
            evaluation_schema=config.evaluation_schema,
            tree_resolver=tree_resolver,
            base_commit_sha=base_commit_sha,
        )
    if store_url.startswith("postgresql://") or store_url.startswith("postgres://"):
        return PostgresStore(
            experiment_id=experiment_id,
            dsn=store_url,
            evaluation_schema=config.evaluation_schema,
            tree_resolver=tree_resolver,
            base_commit_sha=base_commit_sha,
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
        tree_resolver=tree_resolver,
        base_commit_sha=base_commit_sha,
    )


def build_app(
    *,
    store: Store,
    admin_token: str | None = None,
    subscribe_timeout: float = 30.0,
    artifacts_dir: Path | str | None = None,
    artifact_blob_dir: Path | str | None = None,
    max_artifact_bytes: int | None = None,
    checkpoint_experiment_config: str | None = None,
    checkpoint_repo_path: Path | str | None = None,
    checkpoint_import_credentials_dir: Path | str | None = None,
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

    ``checkpoint_experiment_config`` (12b) — the experiment-config YAML
    text, served verbatim into every checkpoint archive's
    ``experiment-config.yaml`` entry per chapter 10 §3. When ``None``,
    the route emits an empty placeholder (the wave-4 in-process
    posture; receiver-side resume requires a non-empty config).

    ``checkpoint_repo_path`` (12b) — path to a bare git repo whose
    refs/objects flow into every checkpoint archive's ``repo.bundle``
    entry via ``git bundle create --all`` per chapter 10 §3. When
    ``None``, the route emits an empty placeholder. Test deployments
    that don't have a paired bare repo leave this unset.

    ``checkpoint_import_credentials_dir`` (issue #150) — directory the
    checkpoint-import handler persists freshly-minted worker bearers
    into per ``10-checkpoints.md`` §8 step 4. The reference Compose
    deployment bind-mounts this into the worker hosts' per-host
    credentials volumes so bearers are in place at host startup.
    When ``None`` (in-process / TestClient default), tokens are still
    minted by the import (§8 is normative) but the wire surface only
    warns; operators must reissue manually via the admin endpoint.
    """
    make_app_kwargs: dict[str, Any] = {}
    if max_artifact_bytes is not None:
        # Issue #166: the §16.1 deposit size cap. When unset, make_app's
        # DEFAULT_MAX_ARTIFACT_BYTES applies.
        make_app_kwargs["max_artifact_bytes"] = max_artifact_bytes
    if artifact_blob_dir is not None:
        # Issue #166: the §16 blob backend's server-PRIVATE writable root,
        # distinct from --artifacts-dir (which backs the read-only legacy
        # /_reference serve route). Without it the deposit endpoint falls
        # back to a non-durable in-memory backend — see the warning the
        # CLI logs.
        make_app_kwargs["artifact_backend"] = FileArtifactBackend(artifact_blob_dir)
    return make_app(
        store,
        admin_token=admin_token,
        subscribe_timeout=subscribe_timeout,
        artifacts_dir=artifacts_dir,
        checkpoint_experiment_config=checkpoint_experiment_config,
        checkpoint_repo_path=checkpoint_repo_path,
        checkpoint_import_credentials_dir=checkpoint_import_credentials_dir,
        **make_app_kwargs,
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
