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
from pathlib import Path
from typing import Any

import httpx
import pytest
from eden_checkpoint import (
    CHECKPOINT_MEDIA_TYPE,
    ExperimentIdConflict,
    extract_checkpoint,
)
from eden_storage import InMemoryStore
from eden_wire import StoreClient, make_app
from eden_wire.client import IndeterminateImport
from fastapi.testclient import TestClient

EXPERIMENT_ID = "exp-checkpoint-wire"
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


def _register_worker(client: TestClient, worker_id: str) -> str:
    resp = client.post(
        f"/v0/experiments/{EXPERIMENT_ID}/workers",
        headers=_admin_headers(),
        json={"worker_id": worker_id},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["registration_token"]


def _register_group(
    client: TestClient, group_id: str, members: list[str] | None = None
) -> None:
    body: dict[str, Any] = {"group_id": group_id}
    if members:
        body["members"] = members
    resp = client.post(
        f"/v0/experiments/{EXPERIMENT_ID}/groups",
        headers=_admin_headers(),
        json=body,
    )
    assert resp.status_code == 200, resp.text


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
    token = _register_worker(client, "non-admin")
    resp = client.get(
        f"/v0/experiments/{EXPERIMENT_ID}",
        headers={
            "X-Eden-Experiment-Id": EXPERIMENT_ID,
            "Authorization": f"Bearer non-admin:{token}",
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
    token = _register_worker(client, "non-admin-2")
    resp = client.post(
        f"/v0/experiments/{EXPERIMENT_ID}/checkpoint",
        headers={
            "X-Eden-Experiment-Id": EXPERIMENT_ID,
            "Authorization": f"Bearer non-admin-2:{token}",
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


