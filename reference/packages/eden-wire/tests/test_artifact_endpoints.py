"""Tests for the normative §16 artifact deposit / fetch endpoints (#166).

Covers ``POST /v0/experiments/{E}/artifacts`` and
``GET /v0/experiments/{E}/artifacts/{A}``: the opaque-URI shape, content
integrity, the streamed size cap (413), the §16.2 per-row fetch ACL
(depositor / admin / admins-group vs a different worker → 403), unknown
id → 404, and the §1.3 experiment-id header parity.
"""

from __future__ import annotations

import re

import pytest
from eden_contracts import EvaluationSchema
from eden_storage import InMemoryStore
from eden_storage.errors import NotFound
from eden_wire import StoreClient, make_app
from eden_wire.errors import Forbidden
from fastapi.testclient import TestClient

EXPERIMENT_ID = "exp-166"
ADMIN_TOKEN = "test-admin-token-166"
_URI_RE = re.compile(r"^eden://artifacts/[0-9a-f]{32}$")


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore(
        experiment_id=EXPERIMENT_ID,
        evaluation_schema=EvaluationSchema({"loss": "real"}),
    )


def _hdr(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {"X-Eden-Experiment-Id": EXPERIMENT_ID}
    if extra:
        headers.update(extra)
    return headers


def _artifacts_url(suffix: str = "") -> str:
    return f"/v0/experiments/{EXPERIMENT_ID}/artifacts{suffix}"


# ----------------------------------------------------------------------
# Auth-disabled posture (deposit / fetch / cap / 404 / mismatch)
# ----------------------------------------------------------------------


class TestDepositFetchNoAuth:
    def test_deposit_returns_201_opaque_uri(self, store: InMemoryStore) -> None:
        client = TestClient(make_app(store))
        resp = client.post(
            _artifacts_url(),
            headers=_hdr(),
            files={"file": ("bundle.tar.gz", b"payload-bytes", "application/gzip")},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert _URI_RE.match(body["artifacts_uri"])
        assert body["size_bytes"] == len(b"payload-bytes")
        assert body["content_type"] == "application/gzip"

    def test_fetch_returns_exact_bytes(self, store: InMemoryStore) -> None:
        client = TestClient(make_app(store))
        payload = b"\x00\x01binary\xffcontent"
        deposit = client.post(
            _artifacts_url(),
            headers=_hdr(),
            files={"file": ("blob", payload, "application/octet-stream")},
        )
        opaque_id = deposit.json()["artifacts_uri"].rsplit("/", 1)[-1]
        fetch = client.get(_artifacts_url(f"/{opaque_id}"), headers=_hdr())
        assert fetch.status_code == 200
        assert fetch.content == payload
        assert fetch.headers["content-type"].startswith("application/octet-stream")
        assert fetch.headers["x-content-type-options"] == "nosniff"
        assert fetch.headers["content-disposition"].startswith("attachment")

    def test_fetch_unknown_id_returns_404(self, store: InMemoryStore) -> None:
        client = TestClient(make_app(store))
        resp = client.get(_artifacts_url("/" + "0" * 32), headers=_hdr())
        assert resp.status_code == 404
        assert resp.json()["type"] == "eden://error/not-found"

    def test_over_cap_deposit_returns_413(self, store: InMemoryStore) -> None:
        client = TestClient(make_app(store, max_artifact_bytes=8))
        resp = client.post(
            _artifacts_url(),
            headers=_hdr(),
            files={"file": ("big", b"way too many bytes", "text/plain")},
        )
        assert resp.status_code == 413
        assert resp.json()["type"] == "eden://error/payload-too-large"

    def test_within_cap_deposit_succeeds(self, store: InMemoryStore) -> None:
        client = TestClient(make_app(store, max_artifact_bytes=1024))
        resp = client.post(
            _artifacts_url(),
            headers=_hdr(),
            files={"file": ("ok", b"small", "text/plain")},
        )
        assert resp.status_code == 201

    def test_experiment_id_header_mismatch_returns_400(
        self, store: InMemoryStore
    ) -> None:
        client = TestClient(make_app(store))
        resp = client.post(
            _artifacts_url(),
            headers={"X-Eden-Experiment-Id": "wrong-exp"},
            files={"file": ("x", b"x", "text/plain")},
        )
        assert resp.status_code == 400
        assert resp.json()["type"] == "eden://error/experiment-id-mismatch"

    def test_missing_file_part_returns_400(self, store: InMemoryStore) -> None:
        client = TestClient(make_app(store))
        resp = client.post(
            _artifacts_url(),
            headers=_hdr(),
            data={"notfile": "x"},
        )
        assert resp.status_code == 400
        assert resp.json()["type"] == "eden://error/bad-request"


# ----------------------------------------------------------------------
# Auth-enabled posture (§16.2 per-row ACL)
# ----------------------------------------------------------------------


def _register_worker(client: TestClient, worker_id: str) -> str:
    resp = client.post(
        f"/v0/experiments/{EXPERIMENT_ID}/workers",
        headers=_hdr({"Authorization": f"Bearer admin:{ADMIN_TOKEN}"}),
        json={"worker_id": worker_id},
    )
    assert resp.status_code == 200
    return resp.json()["registration_token"]


def _register_group(client: TestClient, group_id: str, members: list[str]) -> None:
    resp = client.post(
        f"/v0/experiments/{EXPERIMENT_ID}/groups",
        headers=_hdr({"Authorization": f"Bearer admin:{ADMIN_TOKEN}"}),
        json={"group_id": group_id, "members": members},
    )
    assert resp.status_code == 200


class TestFetchACL:
    def _deposit_as(self, client: TestClient, bearer: str) -> str:
        resp = client.post(
            _artifacts_url(),
            headers=_hdr({"Authorization": bearer}),
            files={"file": ("a", b"secret-bytes", "text/plain")},
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["artifacts_uri"].rsplit("/", 1)[-1]

    def test_depositor_can_fetch_admin_can_fetch_others_cannot(
        self, store: InMemoryStore
    ) -> None:
        client = TestClient(make_app(store, admin_token=ADMIN_TOKEN))
        alice_token = _register_worker(client, "alice")
        bob_token = _register_worker(client, "bob")
        alice = f"Bearer alice:{alice_token}"
        bob = f"Bearer bob:{bob_token}"
        admin = f"Bearer admin:{ADMIN_TOKEN}"

        opaque_id = self._deposit_as(client, alice)
        url = _artifacts_url(f"/{opaque_id}")

        # Depositor fetches its own.
        assert client.get(url, headers=_hdr({"Authorization": alice})).status_code == 200
        # Admin bearer fetches anyone's.
        assert client.get(url, headers=_hdr({"Authorization": admin})).status_code == 200
        # A different worker is refused.
        forbidden = client.get(url, headers=_hdr({"Authorization": bob}))
        assert forbidden.status_code == 403
        assert forbidden.json()["type"] == "eden://error/forbidden"

    def test_admins_group_member_can_fetch(self, store: InMemoryStore) -> None:
        client = TestClient(make_app(store, admin_token=ADMIN_TOKEN))
        alice_token = _register_worker(client, "alice")
        carol_token = _register_worker(client, "carol")
        _register_group(client, "admins", ["carol"])

        opaque_id = self._deposit_as(client, f"Bearer alice:{alice_token}")
        resp = client.get(
            _artifacts_url(f"/{opaque_id}"),
            headers=_hdr({"Authorization": f"Bearer carol:{carol_token}"}),
        )
        assert resp.status_code == 200

    def test_admin_deposit_attributed_to_admin(self, store: InMemoryStore) -> None:
        client = TestClient(make_app(store, admin_token=ADMIN_TOKEN))
        opaque_id = self._deposit_as(client, f"Bearer admin:{ADMIN_TOKEN}")
        # created_by == "admin"; a worker who is not admin-class is refused.
        worker_token = _register_worker(client, "dave")
        resp = client.get(
            _artifacts_url(f"/{opaque_id}"),
            headers=_hdr({"Authorization": f"Bearer dave:{worker_token}"}),
        )
        assert resp.status_code == 403

    def test_unauthenticated_request_rejected(self, store: InMemoryStore) -> None:
        client = TestClient(make_app(store, admin_token=ADMIN_TOKEN))
        resp = client.post(
            _artifacts_url(),
            headers=_hdr(),
            files={"file": ("a", b"x", "text/plain")},
        )
        assert resp.status_code == 401


# ----------------------------------------------------------------------
# StoreClient round-trip (deposit / fetch through the client helper)
# ----------------------------------------------------------------------


class TestStoreClientRoundtrip:
    def _store_client(self, store: InMemoryStore) -> StoreClient:
        client = TestClient(make_app(store), base_url="http://wire.test")
        return StoreClient("http://wire.test", EXPERIMENT_ID, client=client)

    def test_deposit_then_fetch(self, store: InMemoryStore) -> None:
        sc = self._store_client(store)
        payload = b"bundle-bytes\x00\xff"
        result = sc.deposit_artifact(
            payload, filename="bundle.tar.gz", content_type="application/gzip"
        )
        assert result.artifacts_uri.startswith("eden://artifacts/")
        assert result.size_bytes == len(payload)
        opaque_id = result.artifacts_uri.rsplit("/", 1)[-1]
        assert sc.fetch_artifact(opaque_id) == payload

    def test_fetch_unknown_raises_not_found(self, store: InMemoryStore) -> None:
        sc = self._store_client(store)
        with pytest.raises(NotFound):
            sc.fetch_artifact("0" * 32)

    def test_fetch_forbidden_raises(self, store: InMemoryStore) -> None:
        # Auth-enabled: a different worker's fetch maps the 403 envelope
        # back to Forbidden client-side.
        app = make_app(store, admin_token=ADMIN_TOKEN)
        client = TestClient(app, base_url="http://wire.test")
        alice = _register_worker(client, "alice")
        bob = _register_worker(client, "bob")
        alice_sc = StoreClient(
            "http://wire.test", EXPERIMENT_ID, client=client, bearer=f"alice:{alice}"
        )
        bob_sc = StoreClient(
            "http://wire.test", EXPERIMENT_ID, client=client, bearer=f"bob:{bob}"
        )
        uri = alice_sc.deposit_artifact(b"x").artifacts_uri
        opaque_id = uri.rsplit("/", 1)[-1]
        with pytest.raises(Forbidden):
            bob_sc.fetch_artifact(opaque_id)
