"""Unit tests for the task-store-server app-building helpers."""

from __future__ import annotations

from pathlib import Path

import pytest
from eden_storage import (
    FileArtifactBackend,
    GcsBackend,
    InMemoryStore,
    S3Backend,
    SqliteStore,
)
from eden_task_store_server import (
    build_app,
    build_artifact_backend,
    build_store,
    load_experiment_config,
)
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


def test_backend_factory_rejects_blob_dir_inside_artifacts_dir(tmp_path: Path) -> None:
    # Issue #166: the §16 blob dir must not overlap --artifacts-dir (served
    # unauthenticated by the legacy /_reference route → ACL bypass).
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    with pytest.raises(SystemExit, match="must not overlap"):
        build_artifact_backend(
            artifact_blob_dir=artifacts / "blobs", artifacts_dir=artifacts
        )


def test_build_app_accepts_disjoint_blob_dir(tmp_path: Path) -> None:
    config = load_experiment_config(FIXTURE_CONFIG)
    store = build_store(store_url=":memory:", experiment_id="exp-1", config=config)
    backend = build_artifact_backend(
        artifact_blob_dir=tmp_path / "blobs", artifacts_dir=tmp_path / "artifacts"
    )
    app = build_app(
        store=store,
        artifacts_dir=tmp_path / "artifacts",
        artifact_backend=backend,
    )
    assert app is not None


# ---------------------------------------------------------------------
# build_artifact_backend — the --blob-backend factory (issue #174)
# ---------------------------------------------------------------------


def test_backend_factory_file_without_dir_returns_none() -> None:
    # None → eden-wire's non-durable in-memory fallback; the CLI warns.
    assert build_artifact_backend(blob_backend="file") is None


def test_backend_factory_file_with_dir_builds_file_backend(tmp_path: Path) -> None:
    backend = build_artifact_backend(artifact_blob_dir=tmp_path / "blobs")
    assert isinstance(backend, FileArtifactBackend)


def test_backend_factory_s3_requires_bucket() -> None:
    with pytest.raises(SystemExit, match="--blob-s3-bucket is required"):
        build_artifact_backend(blob_backend="s3")


def test_backend_factory_s3_builds_s3_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # boto3.client() eagerly parses the host's ~/.aws/config (which may
    # name credential providers with extra dependencies) — point the SDK
    # at empty files + an explicit region so the test is hermetic on any
    # host. Credentials are resolved lazily at request time.
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "aws-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "aws-creds"))
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    backend = build_artifact_backend(
        blob_backend="s3", s3_bucket="my-bucket", s3_region="us-west-2"
    )
    assert isinstance(backend, S3Backend)


def test_backend_factory_gcs_requires_bucket() -> None:
    with pytest.raises(SystemExit, match="--blob-gcs-bucket is required"):
        build_artifact_backend(blob_backend="gcs")


def test_backend_factory_gcs_builds_gcs_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # storage.Client() resolves credentials eagerly; stub it so the test
    # is hermetic on hosts without GOOGLE_APPLICATION_CREDENTIALS.
    from google.cloud import storage

    class _FakeClient:
        def bucket(self, name: str) -> object:
            return object()

    monkeypatch.setattr(storage, "Client", _FakeClient)
    backend = build_artifact_backend(blob_backend="gcs", gcs_bucket="my-bucket")
    assert isinstance(backend, GcsBackend)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        # Stray per-backend flags under the wrong mode fail fast instead
        # of silently running on the wrong backend.
        ({"blob_backend": "file", "s3_bucket": "b"}, "require"),
        ({"blob_backend": "s3", "s3_bucket": "b", "gcs_bucket": "g"}, "require"),
        ({"blob_backend": "gcs", "gcs_bucket": "g", "s3_region": "r"}, "require"),
    ],
)
def test_backend_factory_rejects_stray_flags(
    kwargs: dict[str, str], match: str
) -> None:
    with pytest.raises(SystemExit, match=match):
        build_artifact_backend(**kwargs)


def test_backend_factory_rejects_blob_dir_under_cloud_mode(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="--artifact-blob-dir"):
        build_artifact_backend(
            blob_backend="s3",
            s3_bucket="b",
            artifact_blob_dir=tmp_path / "blobs",
        )
