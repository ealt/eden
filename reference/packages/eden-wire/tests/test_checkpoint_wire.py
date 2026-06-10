"""Wire-level tests for the wave-4 checkpoint endpoints.

Covers:

- ``GET /v0/experiments/{E}`` (§14.3): admin-gated; returns the full
  Experiment object including ``imported_from``.
- ``POST /v0/experiments/{E}/checkpoint`` (§14.1): admin-gated;
  streams an archive with the canonical media type.
- ``POST /v0/checkpoints/import`` (§14.2): admin-gated; §1.3
  carve-out (X-Eden-Experiment-Id optional); collision / mismatch /
  spec-version error vocabulary.
- StoreClient.export_checkpoint / import_checkpoint / read_experiment
  end-to-end through TestClient.
"""

from __future__ import annotations

import io
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest
from eden_checkpoint import (
    CHECKPOINT_MEDIA_TYPE,
    ExperimentIdConflict,
    extract_checkpoint,
)
from eden_checkpoint.repo_bundle import list_bundle_refs
from eden_storage import InMemoryStore
from eden_wire import StoreClient, make_app
from eden_wire.client import IndeterminateImport
from fastapi.testclient import TestClient

EXPERIMENT_ID = "exp_7j97s4xv0hvpf4tn7knn3rbbwp"
ADMIN_TOKEN = "test-admin-token-checkpoint"


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore(experiment_id=EXPERIMENT_ID)


@pytest.fixture
def fresh_store_factory() -> Any:
    """Return a callable that creates a NEW InMemoryStore on demand.

    Used for tests that need a separate receiver-side Store for the
    import side of a round-trip.
    """

    def _make(experiment_id: str = EXPERIMENT_ID) -> InMemoryStore:
        return InMemoryStore(experiment_id=experiment_id)

    return _make


def _admin_headers(experiment_id: str = EXPERIMENT_ID) -> dict[str, str]:
    return {
        "X-Eden-Experiment-Id": experiment_id,
        "Authorization": f"Bearer admin:{ADMIN_TOKEN}",
    }


def _register_worker(client: TestClient, name: str) -> tuple[str, str]:
    """Register a worker by name; return (minted ``wkr_*`` id, token)."""
    resp = client.post(
        f"/v0/experiments/{EXPERIMENT_ID}/workers",
        headers=_admin_headers(),
        json={"name": name},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return body["worker_id"], body["registration_token"]


def _register_group(
    client: TestClient, name: str, members: list[str] | None = None
) -> str:
    """Register a group by name; return the minted ``grp_*`` id."""
    body: dict[str, Any] = {"name": name}
    if members:
        body["members"] = members
    resp = client.post(
        f"/v0/experiments/{EXPERIMENT_ID}/groups",
        headers=_admin_headers(),
        json=body,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["group_id"]


def _make_admin_worker_client(store: InMemoryStore) -> tuple[TestClient, str]:
    """Build a TestClient with auth enabled; return (client, admin_bearer).

    The checkpoint endpoints are admin-gated on the literal ``admin``
    principal per chapter 7 §14 (bootstrap-class); the test uses the
    deployment-admin bearer directly rather than the `admins` group.
    """
    app = make_app(store, admin_token=ADMIN_TOKEN)
    client = TestClient(app)
    return client, f"admin:{ADMIN_TOKEN}"


# ----------------------------------------------------------------------
# GET /v0/experiments/{E}
# ----------------------------------------------------------------------


def test_read_experiment_returns_full_object(store: InMemoryStore) -> None:
    client, admin_bearer = _make_admin_worker_client(store)
    resp = client.get(
        f"/v0/experiments/{EXPERIMENT_ID}",
        headers={
            "X-Eden-Experiment-Id": EXPERIMENT_ID,
            "Authorization": f"Bearer {admin_bearer}",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["experiment_id"] == EXPERIMENT_ID
    assert body["state"] == "running"
    assert "created_at" in body
    assert body["imported_from"] is None


def test_read_experiment_worker_bearer_accepted(store: InMemoryStore) -> None:
    """Worker bearer reads the full experiment per chapter 7 §14.3 (either-auth).

    Post-wave-5 amendment: GET /v0/experiments/{E} is either-auth so
    the orchestrator's per-iteration policy view can read
    `created_at` over its worker bearer. The recovery-probe flow uses
    admin; both legitimately need read access.
    """
    app = make_app(store, admin_token=ADMIN_TOKEN)
    client = TestClient(app)
    worker_id, token = _register_worker(client, "non-admin")
    resp = client.get(
        f"/v0/experiments/{EXPERIMENT_ID}",
        headers={
            "X-Eden-Experiment-Id": EXPERIMENT_ID,
            "Authorization": f"Bearer {worker_id}:{token}",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["experiment_id"] == EXPERIMENT_ID


# ----------------------------------------------------------------------
# POST /v0/experiments/{E}/checkpoint
# ----------------------------------------------------------------------


def test_export_returns_checkpoint_media_type(
    store: InMemoryStore, tmp_path: Path
) -> None:
    client, admin_bearer = _make_admin_worker_client(store)
    resp = client.post(
        f"/v0/experiments/{EXPERIMENT_ID}/checkpoint",
        headers={
            "X-Eden-Experiment-Id": EXPERIMENT_ID,
            "Authorization": f"Bearer {admin_bearer}",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == CHECKPOINT_MEDIA_TYPE
    archive_bytes = resp.content
    assert archive_bytes
    # The bytes parse as a valid archive whose manifest names our id.
    reader = extract_checkpoint(io.BytesIO(archive_bytes), tmp_path)
    assert reader.manifest.experiment_id == EXPERIMENT_ID


def test_export_rejects_worker_bearer(store: InMemoryStore) -> None:
    """Export is admin-gated; worker bearer → 403."""
    app = make_app(store, admin_token=ADMIN_TOKEN)
    client = TestClient(app)
    worker_id, token = _register_worker(client, "non-admin-2")
    resp = client.post(
        f"/v0/experiments/{EXPERIMENT_ID}/checkpoint",
        headers={
            "X-Eden-Experiment-Id": EXPERIMENT_ID,
            "Authorization": f"Bearer {worker_id}:{token}",
        },
    )
    assert resp.status_code == 403


# ----------------------------------------------------------------------
# POST /v0/checkpoints/import
# ----------------------------------------------------------------------


def test_import_round_trip_through_wire(
    store: InMemoryStore, fresh_store_factory: Any, tmp_path: Path
) -> None:
    client, admin_bearer = _make_admin_worker_client(store)
    export = client.post(
        f"/v0/experiments/{EXPERIMENT_ID}/checkpoint",
        headers={
            "X-Eden-Experiment-Id": EXPERIMENT_ID,
            "Authorization": f"Bearer {admin_bearer}",
        },
    )
    archive_bytes = export.content

    # Receiver: a fresh InMemoryStore with the same experiment_id.
    receiver = fresh_store_factory()
    receiver_app = make_app(receiver, admin_token=ADMIN_TOKEN)
    receiver_client = TestClient(receiver_app)

    resp = receiver_client.post(
        "/v0/checkpoints/import",
        content=archive_bytes,
        headers={
            "Content-Type": CHECKPOINT_MEDIA_TYPE,
            "Authorization": f"Bearer admin:{ADMIN_TOKEN}",
        },
    )
    # Chapter 7 §14.2 mandates 201 Created on a successful import.
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["experiment_id"] == EXPERIMENT_ID
    # The receiver's experiment now carries imported_from.
    assert receiver.read_experiment().imported_from is not None


def test_import_rejects_collision(
    store: InMemoryStore, fresh_store_factory: Any
) -> None:
    """Second import into an already-imported store → ExperimentIdConflict (409)."""
    client, admin_bearer = _make_admin_worker_client(store)
    export = client.post(
        f"/v0/experiments/{EXPERIMENT_ID}/checkpoint",
        headers={
            "X-Eden-Experiment-Id": EXPERIMENT_ID,
            "Authorization": f"Bearer {admin_bearer}",
        },
    )
    archive_bytes = export.content

    receiver = fresh_store_factory()
    receiver_app = make_app(receiver, admin_token=ADMIN_TOKEN)
    receiver_client = TestClient(receiver_app)

    first = receiver_client.post(
        "/v0/checkpoints/import",
        content=archive_bytes,
        headers={
            "Content-Type": CHECKPOINT_MEDIA_TYPE,
            "Authorization": f"Bearer admin:{ADMIN_TOKEN}",
        },
    )
    assert first.status_code == 201

    second = receiver_client.post(
        "/v0/checkpoints/import",
        content=archive_bytes,
        headers={
            "Content-Type": CHECKPOINT_MEDIA_TYPE,
            "Authorization": f"Bearer admin:{ADMIN_TOKEN}",
        },
    )
    assert second.status_code == 409
    assert second.json()["type"] == "eden://error/experiment-id-conflict"


def test_import_rejects_mismatched_header(
    store: InMemoryStore, fresh_store_factory: Any
) -> None:
    """§1.3 carve-out: header MUST match the post-rewrite experiment_id."""
    client, admin_bearer = _make_admin_worker_client(store)
    export = client.post(
        f"/v0/experiments/{EXPERIMENT_ID}/checkpoint",
        headers={
            "X-Eden-Experiment-Id": EXPERIMENT_ID,
            "Authorization": f"Bearer {admin_bearer}",
        },
    )
    archive_bytes = export.content

    receiver = fresh_store_factory()
    receiver_app = make_app(receiver, admin_token=ADMIN_TOKEN)
    receiver_client = TestClient(receiver_app)

    resp = receiver_client.post(
        "/v0/checkpoints/import",
        content=archive_bytes,
        headers={
            "Content-Type": CHECKPOINT_MEDIA_TYPE,
            "Authorization": f"Bearer admin:{ADMIN_TOKEN}",
            "X-Eden-Experiment-Id": "exp-wrong-id",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["type"] == "eden://error/experiment-id-mismatch"


def test_import_header_optional_per_carveout(
    store: InMemoryStore, fresh_store_factory: Any
) -> None:
    """Header absent on /v0/checkpoints/import → 201 per §14.2 + §1.3 carve-out."""
    client, admin_bearer = _make_admin_worker_client(store)
    export = client.post(
        f"/v0/experiments/{EXPERIMENT_ID}/checkpoint",
        headers={
            "X-Eden-Experiment-Id": EXPERIMENT_ID,
            "Authorization": f"Bearer {admin_bearer}",
        },
    )
    archive_bytes = export.content

    receiver = fresh_store_factory()
    receiver_app = make_app(receiver, admin_token=ADMIN_TOKEN)
    receiver_client = TestClient(receiver_app)

    resp = receiver_client.post(
        "/v0/checkpoints/import",
        content=archive_bytes,
        headers={
            "Content-Type": CHECKPOINT_MEDIA_TYPE,
            "Authorization": f"Bearer admin:{ADMIN_TOKEN}",
            # NOTE: no X-Eden-Experiment-Id.
        },
    )
    assert resp.status_code == 201


def test_import_rejects_empty_body(
    fresh_store_factory: Any,
) -> None:
    receiver = fresh_store_factory()
    receiver_app = make_app(receiver, admin_token=ADMIN_TOKEN)
    receiver_client = TestClient(receiver_app)

    resp = receiver_client.post(
        "/v0/checkpoints/import",
        content=b"",
        headers={
            "Content-Type": CHECKPOINT_MEDIA_TYPE,
            "Authorization": f"Bearer admin:{ADMIN_TOKEN}",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["type"] == "eden://error/bad-request"


def test_import_rejects_corrupt_archive(
    fresh_store_factory: Any,
) -> None:
    receiver = fresh_store_factory()
    receiver_app = make_app(receiver, admin_token=ADMIN_TOKEN)
    receiver_client = TestClient(receiver_app)

    resp = receiver_client.post(
        "/v0/checkpoints/import",
        content=b"this is not a tar",
        headers={
            "Content-Type": CHECKPOINT_MEDIA_TYPE,
            "Authorization": f"Bearer admin:{ADMIN_TOKEN}",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["type"] == "eden://error/checkpoint-invalid"


# ----------------------------------------------------------------------
# Issue #150 — receiver-side auto-reissue on checkpoint import
# ----------------------------------------------------------------------


def _export_with_workers(
    store: InMemoryStore, names: list[str]
) -> tuple[bytes, list[str]]:
    """Register workers by name, export, and return (archive, minted ids)."""
    client, admin_bearer = _make_admin_worker_client(store)
    minted_ids: list[str] = []
    for name in names:
        worker_id, _ = _register_worker(client, name)
        minted_ids.append(worker_id)
    resp = client.post(
        f"/v0/experiments/{EXPERIMENT_ID}/checkpoint",
        headers={
            "X-Eden-Experiment-Id": EXPERIMENT_ID,
            "Authorization": f"Bearer {admin_bearer}",
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.content, minted_ids


def test_import_persists_reissued_credentials_to_dir(
    store: InMemoryStore, fresh_store_factory: Any, tmp_path: Path
) -> None:
    """When `checkpoint_import_credentials_dir` is set, the wire layer
    writes one `<worker_id>.token` per imported worker (mode 0600), and
    each persisted bearer authenticates against the receiver via /whoami.
    """
    archive_bytes, minted_ids = _export_with_workers(store, ["wkr-a", "wkr-b"])

    creds_dir = tmp_path / "host-creds"
    receiver = fresh_store_factory()
    receiver_app = make_app(
        receiver,
        admin_token=ADMIN_TOKEN,
        checkpoint_import_credentials_dir=creds_dir,
    )
    receiver_client = TestClient(receiver_app)
    resp = receiver_client.post(
        "/v0/checkpoints/import",
        content=archive_bytes,
        headers={
            "Content-Type": CHECKPOINT_MEDIA_TYPE,
            "Authorization": f"Bearer admin:{ADMIN_TOKEN}",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["experiment_id"] == EXPERIMENT_ID
    warnings = body["warnings"]
    assert any("credentials reissued and persisted" in w for w in warnings)

    # Files are present, owner-only, and the tokens verify against the
    # receiver store directly.
    for worker_id in minted_ids:
        path = creds_dir / f"{worker_id}.token"
        assert path.is_file()
        # 0o777 mask matches the spec/13.5 0o600 expectation; we
        # don't pin uid/gid which would be CI-environment-dependent.
        assert (path.stat().st_mode & 0o777) == 0o600
        token = path.read_text()
        assert receiver.verify_worker_credential(worker_id, token) is True


def test_import_warns_when_no_credentials_dir(
    store: InMemoryStore, fresh_store_factory: Any
) -> None:
    """When the credentials dir is unset, the import still mints fresh
    credentials but surfaces a warning that they were NOT persisted.
    """
    archive_bytes, _ = _export_with_workers(store, ["wkr-c"])

    receiver = fresh_store_factory()
    receiver_app = make_app(receiver, admin_token=ADMIN_TOKEN)
    receiver_client = TestClient(receiver_app)
    resp = receiver_client.post(
        "/v0/checkpoints/import",
        content=archive_bytes,
        headers={
            "Content-Type": CHECKPOINT_MEDIA_TYPE,
            "Authorization": f"Bearer admin:{ADMIN_TOKEN}",
        },
    )
    assert resp.status_code == 201, resp.text
    warnings = resp.json()["warnings"]
    # Tokens were minted but NOT persisted — warning calls that out.
    assert any(
        "not persisted" in w.lower()
        or "NOT persisted" in w
        or "no checkpoint_import_credentials_dir" in w
        for w in warnings
    ), warnings


def test_import_then_worker_bootstrap_reuses_persisted_bearer(
    store: InMemoryStore, fresh_store_factory: Any, tmp_path: Path
) -> None:
    """End-to-end issue #150 flow: import drops `<worker_id>.token` files
    into the persistence dir; a worker host pointed at that dir picks up
    the bearer via :func:`bootstrap_worker_credential` without needing
    the admin token (the pre-persisted bearer verifies via /whoami).
    """
    from eden_service_common.auth import bootstrap_worker_credential

    archive_bytes, minted_ids = _export_with_workers(store, ["wkr-bootstrap"])
    (bootstrap_id,) = minted_ids

    creds_dir = tmp_path / "shared-creds"
    receiver = fresh_store_factory()
    receiver_app = make_app(
        receiver,
        admin_token=ADMIN_TOKEN,
        checkpoint_import_credentials_dir=creds_dir,
    )
    receiver_client = TestClient(receiver_app)
    resp = receiver_client.post(
        "/v0/checkpoints/import",
        content=archive_bytes,
        headers={
            "Content-Type": CHECKPOINT_MEDIA_TYPE,
            "Authorization": f"Bearer admin:{ADMIN_TOKEN}",
        },
    )
    assert resp.status_code == 201, resp.text

    # The worker host's bootstrap helper builds its own httpx.Client; we
    # have to route those through the receiver app. The cleanest way is
    # to patch ``httpx.Client.__init__`` to default the transport to our
    # proxy, mirroring the test_service_auth fixture's pattern.
    import httpx as _httpx

    proxy = _proxy_to_app(receiver_client)
    real_init = _httpx.Client.__init__

    def _patched(self: Any, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("transport", proxy)
        real_init(self, *args, **kwargs)

    _httpx.Client.__init__ = _patched
    try:
        cred = bootstrap_worker_credential(
            base_url="http://unused",
            experiment_id=EXPERIMENT_ID,
            worker_id=bootstrap_id,
            credentials_dir=creds_dir,
            # No admin token — the pre-persisted bearer should verify
            # via /whoami without falling through to the reissue
            # branch (which would require admin auth).
            admin_token=None,
        )
    finally:
        _httpx.Client.__init__ = real_init

    assert cred.worker_id == bootstrap_id
    # The bearer we got from bootstrap MUST be the one we persisted
    # during the import — no rotation, no reissue round-trip.
    persisted_token = (creds_dir / f"{bootstrap_id}.token").read_text()
    assert cred.token == persisted_token


def test_import_no_workers_no_credentials_warning(
    store: InMemoryStore, fresh_store_factory: Any, tmp_path: Path
) -> None:
    """A worker-less checkpoint emits no credential-reissue warning and
    leaves the credentials directory empty (and not even created)."""
    # Export without registering any workers.
    client, admin_bearer = _make_admin_worker_client(store)
    resp = client.post(
        f"/v0/experiments/{EXPERIMENT_ID}/checkpoint",
        headers={
            "X-Eden-Experiment-Id": EXPERIMENT_ID,
            "Authorization": f"Bearer {admin_bearer}",
        },
    )
    archive_bytes = resp.content

    creds_dir = tmp_path / "host-creds-empty"
    receiver = fresh_store_factory()
    receiver_app = make_app(
        receiver,
        admin_token=ADMIN_TOKEN,
        checkpoint_import_credentials_dir=creds_dir,
    )
    receiver_client = TestClient(receiver_app)
    resp = receiver_client.post(
        "/v0/checkpoints/import",
        content=archive_bytes,
        headers={
            "Content-Type": CHECKPOINT_MEDIA_TYPE,
            "Authorization": f"Bearer admin:{ADMIN_TOKEN}",
        },
    )
    assert resp.status_code == 201, resp.text
    warnings = resp.json()["warnings"]
    assert not any("credentials" in w.lower() for w in warnings), warnings
    # No workers → no files written → directory is not created.
    assert not creds_dir.exists() or not any(creds_dir.iterdir())


# ----------------------------------------------------------------------
# StoreClient round-trip through TestClient
# ----------------------------------------------------------------------


def test_storeclient_read_experiment_returns_imported_from(
    store: InMemoryStore, fresh_store_factory: Any
) -> None:
    client, admin_bearer = _make_admin_worker_client(store)
    export = client.post(
        f"/v0/experiments/{EXPERIMENT_ID}/checkpoint",
        headers={
            "X-Eden-Experiment-Id": EXPERIMENT_ID,
            "Authorization": f"Bearer {admin_bearer}",
        },
    )
    archive_bytes = export.content

    receiver = fresh_store_factory()
    receiver_app = make_app(receiver, admin_token=ADMIN_TOKEN)
    receiver_client = TestClient(receiver_app)
    receiver_client.post(
        "/v0/checkpoints/import",
        content=archive_bytes,
        headers={
            "Content-Type": CHECKPOINT_MEDIA_TYPE,
            "Authorization": f"Bearer admin:{ADMIN_TOKEN}",
        },
    )

    # Drive StoreClient.read_experiment against the receiver via the
    # standard MockTransport proxy pattern.
    sc = StoreClient(
        "http://unused",
        EXPERIMENT_ID,
        bearer=f"admin:{ADMIN_TOKEN}",
        client=httpx.Client(
            transport=_proxy_to_app(receiver_client), base_url="http://unused"
        ),
    )
    exp = sc.read_experiment()
    assert exp.experiment_id == EXPERIMENT_ID
    assert exp.imported_from is not None
    sc.close()


def test_storeclient_export_import_round_trip(
    store: InMemoryStore, fresh_store_factory: Any
) -> None:
    """End-to-end StoreClient.export_checkpoint → import_checkpoint."""
    sender_client, admin_bearer = _make_admin_worker_client(store)
    sender_sc = StoreClient(
        "http://unused",
        EXPERIMENT_ID,
        bearer=admin_bearer,
        client=httpx.Client(
            transport=_proxy_to_app(sender_client), base_url="http://unused"
        ),
    )

    archive = io.BytesIO()
    manifest = sender_sc.export_checkpoint(archive)
    assert manifest.experiment_id == EXPERIMENT_ID
    sender_sc.close()

    # Receiver side.
    receiver = fresh_store_factory()
    receiver_app = make_app(receiver, admin_token=ADMIN_TOKEN)
    receiver_client = TestClient(receiver_app)
    receiver_sc = StoreClient(
        "http://unused",
        EXPERIMENT_ID,
        bearer=f"admin:{ADMIN_TOKEN}",
        client=httpx.Client(
            transport=_proxy_to_app(receiver_client), base_url="http://unused"
        ),
    )
    archive.seek(0)
    result = receiver_sc.import_checkpoint(archive)
    assert result["experiment_id"] == EXPERIMENT_ID
    # The receiver's import_checkpoint surfaces a reissue-required
    # warning when workers came over.
    receiver_sc.close()


def _proxy_to_app(app_client: TestClient) -> httpx.MockTransport:
    """Route httpx requests through a FastAPI ``TestClient``.

    Mirrors the existing helper in test_lifecycle_wire / test_reassign_dispatch_wire.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        response = app_client.request(
            request.method,
            request.url.raw_path.decode("ascii"),
            headers=dict(request.headers),
            content=request.content,
        )
        return httpx.Response(
            response.status_code,
            headers=dict(response.headers),
            content=response.content,
        )

    return httpx.MockTransport(_handler)


# ----------------------------------------------------------------------
# Codex round-1 #5: 3-outcome recovery-probe ladder on import
# ----------------------------------------------------------------------


def _flaky_transport_dropping_import_post(
    app_client: TestClient,
) -> httpx.MockTransport:
    """MockTransport that raises ConnectError on POST /v0/checkpoints/import.

    All other requests (including the recovery-probe GET) proxy
    transparently to ``app_client``. Lets us simulate "POST landed
    server-side but the response was dropped".
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        if (
            request.method == "POST"
            and request.url.path == "/v0/checkpoints/import"
        ):
            # Server-side: REPLAY the POST against TestClient so the
            # import actually commits, then raise to simulate the
            # dropped 201 response. This matches the "we committed,
            # response lost" case.
            app_client.request(
                request.method,
                request.url.raw_path.decode("ascii"),
                headers=dict(request.headers),
                content=request.content,
            )
            raise httpx.ConnectError("simulated transport failure")
        response = app_client.request(
            request.method,
            request.url.raw_path.decode("ascii"),
            headers=dict(request.headers),
            content=request.content,
        )
        return httpx.Response(
            response.status_code,
            headers=dict(response.headers),
            content=response.content,
        )

    return httpx.MockTransport(_handler)


def test_import_recovery_probe_confirmed_success(
    store: InMemoryStore, fresh_store_factory: Any
) -> None:
    """Transport blip after commit + matching imported_from → synthesized success."""
    sender_client, _admin = _make_admin_worker_client(store)
    archive_bytes = sender_client.post(
        f"/v0/experiments/{EXPERIMENT_ID}/checkpoint",
        headers={
            "X-Eden-Experiment-Id": EXPERIMENT_ID,
            "Authorization": f"Bearer admin:{ADMIN_TOKEN}",
        },
    ).content

    receiver = fresh_store_factory()
    receiver_app = make_app(receiver, admin_token=ADMIN_TOKEN)
    receiver_client = TestClient(receiver_app)
    flaky_http = httpx.Client(
        transport=_flaky_transport_dropping_import_post(receiver_client),
        base_url="http://unused",
    )
    sc = StoreClient(
        "http://unused",
        EXPERIMENT_ID,
        bearer=f"admin:{ADMIN_TOKEN}",
        client=flaky_http,
    )
    # The POST raises transport error; the probe ladder reads back
    # imported_from matching the local manifest → confirmed success.
    result = sc.import_checkpoint(io.BytesIO(archive_bytes))
    assert result["experiment_id"] == EXPERIMENT_ID
    assert "recovered from transport-indeterminate" in result["warnings"][0]
    sc.close()


def test_import_recovery_probe_confirmed_divergence(
    store: InMemoryStore, fresh_store_factory: Any
) -> None:
    """Uncommitted POST + receiver has different import → ExperimentIdConflict."""
    sender_client, _admin = _make_admin_worker_client(store)
    first_archive = sender_client.post(
        f"/v0/experiments/{EXPERIMENT_ID}/checkpoint",
        headers={
            "X-Eden-Experiment-Id": EXPERIMENT_ID,
            "Authorization": f"Bearer admin:{ADMIN_TOKEN}",
        },
    ).content
    # Land a SUCCESSFUL import on the receiver first (so imported_from
    # is set but won't match our future probe's manifest).
    receiver = fresh_store_factory()
    receiver_app = make_app(receiver, admin_token=ADMIN_TOKEN)
    receiver_client = TestClient(receiver_app)
    first_imp = receiver_client.post(
        "/v0/checkpoints/import",
        content=first_archive,
        headers={
            "Content-Type": CHECKPOINT_MEDIA_TYPE,
            "Authorization": f"Bearer admin:{ADMIN_TOKEN}",
        },
    )
    assert first_imp.status_code == 201

    # Build a DIFFERENT archive (re-export from sender → different
    # exported_at timestamp) to drive the divergence branch.
    import time

    time.sleep(0.01)
    second_archive = sender_client.post(
        f"/v0/experiments/{EXPERIMENT_ID}/checkpoint",
        headers={
            "X-Eden-Experiment-Id": EXPERIMENT_ID,
            "Authorization": f"Bearer admin:{ADMIN_TOKEN}",
        },
    ).content

    # Build a transport that ALWAYS raises on POST /import (does NOT
    # replay). Then the receiver's state still reflects first_archive,
    # not second_archive — probe shows divergence.
    def _flaky(request: httpx.Request) -> httpx.Response:
        if (
            request.method == "POST"
            and request.url.path == "/v0/checkpoints/import"
        ):
            raise httpx.ConnectError("simulated transport failure (no replay)")
        response = receiver_client.request(
            request.method,
            request.url.raw_path.decode("ascii"),
            headers=dict(request.headers),
            content=request.content,
        )
        return httpx.Response(
            response.status_code,
            headers=dict(response.headers),
            content=response.content,
        )

    flaky_http = httpx.Client(
        transport=httpx.MockTransport(_flaky), base_url="http://unused"
    )
    sc = StoreClient(
        "http://unused",
        EXPERIMENT_ID,
        bearer=f"admin:{ADMIN_TOKEN}",
        client=flaky_http,
    )
    with pytest.raises(ExperimentIdConflict):
        sc.import_checkpoint(io.BytesIO(second_archive))
    sc.close()


def test_import_recovery_probe_indeterminate_when_readback_fails(
    store: InMemoryStore,
) -> None:
    """Transport blip on POST + probe also fails → IndeterminateImport."""
    sender_client, _admin = _make_admin_worker_client(store)
    archive_bytes = sender_client.post(
        f"/v0/experiments/{EXPERIMENT_ID}/checkpoint",
        headers={
            "X-Eden-Experiment-Id": EXPERIMENT_ID,
            "Authorization": f"Bearer admin:{ADMIN_TOKEN}",
        },
    ).content

    # Transport raises on every request — POST + probe both fail.
    def _all_fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated total transport failure")

    flaky_http = httpx.Client(
        transport=httpx.MockTransport(_all_fail), base_url="http://unused"
    )
    sc = StoreClient(
        "http://unused",
        EXPERIMENT_ID,
        bearer=f"admin:{ADMIN_TOKEN}",
        client=flaky_http,
    )
    with pytest.raises(IndeterminateImport):
        sc.import_checkpoint(io.BytesIO(archive_bytes))
    sc.close()




# ----------------------------------------------------------------------
# Export repo refresh + bundling (issue #294)
# ----------------------------------------------------------------------

_git_available = shutil.which("git") is not None


def _init_repo_with_commit(path: Path) -> str:
    """Create a non-bare repo, make one commit, return the commit SHA."""
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "-c", "user.email=t@t.invalid",
         "-c", "user.name=t", "commit", "--allow-empty", "-m", "seed"],
        check=True,
        capture_output=True,
    )
    rc = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return rc.stdout.strip()


@pytest.mark.skipif(not _git_available, reason="git not installed")
def test_export_runs_repo_refresh_before_bundling(
    store: InMemoryStore, tmp_path: Path
) -> None:
    """The refresh callable runs per export, before the bundle is cut.

    The refresh here plays the role of the Compose deployment's
    fetch-from-Forgejo: it adds a ref to the local repo. That ref must
    appear in the exported bundle — proving the route refreshed first
    and bundled second (issue #294).
    """
    repo_path = tmp_path / "repo"
    sha = _init_repo_with_commit(repo_path)
    refresh_calls: list[int] = []

    def _refresh() -> None:
        refresh_calls.append(1)
        subprocess.run(
            ["git", "-C", str(repo_path), "branch", "added-by-refresh", sha],
            check=True,
            capture_output=True,
        )

    app = make_app(
        store,
        admin_token=ADMIN_TOKEN,
        checkpoint_repo_path=repo_path,
        checkpoint_repo_refresh=_refresh,
    )
    client = TestClient(app)
    resp = client.post(
        f"/v0/experiments/{EXPERIMENT_ID}/checkpoint",
        headers=_admin_headers(),
    )
    assert resp.status_code == 200, resp.text
    assert refresh_calls == [1]
    extract_dir = tmp_path / "extract"
    extract_dir.mkdir()
    reader = extract_checkpoint(io.BytesIO(resp.content), extract_dir)
    bundle_path = reader.root / reader.manifest.files.repo_bundle
    refs = list_bundle_refs(bundle_path)
    assert "refs/heads/added-by-refresh" in refs, refs


def test_export_refresh_failure_maps_to_503(
    store: InMemoryStore, tmp_path: Path
) -> None:
    """A failed remote sync fails the export loudly (no stale bundle)."""

    def _refresh() -> None:
        raise RuntimeError("forgejo unreachable")

    app = make_app(
        store,
        admin_token=ADMIN_TOKEN,
        checkpoint_repo_path=tmp_path / "repo",
        checkpoint_repo_refresh=_refresh,
    )
    client = TestClient(app)
    resp = client.post(
        f"/v0/experiments/{EXPERIMENT_ID}/checkpoint",
        headers=_admin_headers(),
    )
    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["type"] == "eden://reference-error/checkpoint-repo-unavailable"
    assert "forgejo unreachable" in body["detail"]


def test_export_without_repo_path_skips_refresh(
    store: InMemoryStore, tmp_path: Path
) -> None:
    """No repo path → empty-bundle placeholder; the refresh never runs."""
    refresh_calls: list[int] = []

    app = make_app(
        store,
        admin_token=ADMIN_TOKEN,
        checkpoint_repo_refresh=lambda: refresh_calls.append(1),
    )
    client = TestClient(app)
    resp = client.post(
        f"/v0/experiments/{EXPERIMENT_ID}/checkpoint",
        headers=_admin_headers(),
    )
    assert resp.status_code == 200, resp.text
    assert refresh_calls == []
    extract_dir = tmp_path / "extract"
    extract_dir.mkdir()
    reader = extract_checkpoint(io.BytesIO(resp.content), extract_dir)
    bundle_path = reader.root / reader.manifest.files.repo_bundle
    assert bundle_path.stat().st_size == 0
