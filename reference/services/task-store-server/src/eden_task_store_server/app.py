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
    ArtifactBackend,
    FileArtifactBackend,
    GcsBackend,
    InMemoryStore,
    PostgresStore,
    S3Backend,
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
    "build_artifact_backend",
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


def _reject_blob_dir_overlap(
    blob_dir: Path | str, artifacts_dir: Path | str | None
) -> None:
    """Reject a §16 blob dir that overlaps the legacy ``--artifacts-dir``.

    The legacy ``--artifacts-dir`` is served by the unauthenticated
    ``/_reference/.../artifacts/{path}`` route. If the server-private §16
    blob dir is equal to / nested in / a parent of it, a worker who learns
    an opaque id could fetch deposited bytes through the reference path,
    bypassing the §16.2 depositor/admin ACL. Fail fast at startup.
    """
    if artifacts_dir is None:
        return
    blob = Path(blob_dir).resolve()
    arts = Path(artifacts_dir).resolve()
    if blob == arts or blob.is_relative_to(arts) or arts.is_relative_to(blob):
        raise SystemExit(
            f"--artifact-blob-dir ({blob}) must not overlap --artifacts-dir "
            f"({arts}): the legacy /_reference artifact route serves the "
            "latter unauthenticated, which would bypass the §16.2 fetch ACL "
            "(issue #166)."
        )


def build_artifact_backend(
    *,
    blob_backend: str = "file",
    artifact_blob_dir: Path | str | None = None,
    artifacts_dir: Path | str | None = None,
    s3_bucket: str = "",
    s3_region: str = "",
    s3_prefix: str = "",
    s3_endpoint_url: str = "",
    gcs_bucket: str = "",
    gcs_prefix: str = "",
) -> ArtifactBackend | None:
    """Build the §16 artifact blob backend from CLI-shaped config (issue #174).

    ``blob_backend`` selects among the reference backends:

    - ``file`` (default) — ``FileArtifactBackend`` rooted at
      ``artifact_blob_dir``. Returns ``None`` when the dir is unset, in
      which case eden-wire falls back to the NON-DURABLE in-memory
      backend (the CLI warns loudly). ``artifacts_dir`` is consulted
      only for the overlap rejection (the legacy ``/_reference`` route
      serves it unauthenticated; a nested blob dir would bypass the
      §16.2 fetch ACL).
    - ``s3`` — ``S3Backend`` against ``s3_bucket`` (required).
      Credentials come from boto3's default chain (IRSA / instance
      profile / ``AWS_ACCESS_KEY_ID`` env), never from argv.
    - ``gcs`` — ``GcsBackend`` against ``gcs_bucket`` (required).
      Credentials come from the GCP default chain (Workload Identity /
      ``GOOGLE_APPLICATION_CREDENTIALS`` env).

    Per-backend flags are rejected under any other mode, so a values
    typo (e.g. an S3 bucket with ``--blob-backend file``) fails at
    startup instead of silently running on the wrong backend.
    """
    s3_flags = {
        "--blob-s3-bucket": s3_bucket,
        "--blob-s3-region": s3_region,
        "--blob-s3-prefix": s3_prefix,
        "--blob-s3-endpoint-url": s3_endpoint_url,
    }
    gcs_flags = {"--blob-gcs-bucket": gcs_bucket, "--blob-gcs-prefix": gcs_prefix}
    if blob_backend != "s3" and any(s3_flags.values()):
        stray = ", ".join(name for name, value in s3_flags.items() if value)
        raise SystemExit(f"{stray} require(s) --blob-backend s3 (got {blob_backend!r}).")
    if blob_backend != "gcs" and any(gcs_flags.values()):
        stray = ", ".join(name for name, value in gcs_flags.items() if value)
        raise SystemExit(f"{stray} require(s) --blob-backend gcs (got {blob_backend!r}).")
    if blob_backend != "file" and artifact_blob_dir is not None:
        raise SystemExit(
            "--artifact-blob-dir is the file backend's root and is meaningless "
            f"with --blob-backend {blob_backend}; remove it."
        )
    if blob_backend == "s3":
        if not s3_bucket:
            raise SystemExit("--blob-s3-bucket is required with --blob-backend s3.")
        return S3Backend(
            bucket=s3_bucket,
            region=s3_region or None,
            prefix=s3_prefix,
            endpoint_url=s3_endpoint_url or None,
        )
    if blob_backend == "gcs":
        if not gcs_bucket:
            raise SystemExit("--blob-gcs-bucket is required with --blob-backend gcs.")
        return GcsBackend(bucket=gcs_bucket, prefix=gcs_prefix)
    if blob_backend != "file":
        raise SystemExit(f"unknown --blob-backend {blob_backend!r} (file|s3|gcs).")
    if artifact_blob_dir is None:
        return None
    _reject_blob_dir_overlap(artifact_blob_dir, artifacts_dir)
    return FileArtifactBackend(artifact_blob_dir)


def build_app(
    *,
    store: Store,
    admin_token: str | None = None,
    subscribe_timeout: float = 30.0,
    artifacts_dir: Path | str | None = None,
    artifact_backend: ArtifactBackend | None = None,
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
    if artifact_backend is not None:
        # Issue #166 / #174: the §16 blob backend, built by
        # build_artifact_backend (which owns the per-mode validation and
        # the blob-dir/artifacts-dir overlap rejection). Without it the
        # deposit endpoint falls back to a non-durable in-memory backend
        # — see the warning the CLI logs.
        make_app_kwargs["artifact_backend"] = artifact_backend
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
