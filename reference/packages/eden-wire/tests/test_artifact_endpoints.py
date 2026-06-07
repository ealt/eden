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

EXPERIMENT_ID = "exp_xmwwr2gf0qzp1gc38pg58a0ks3"
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
        uri = deposit.json()["artifacts_uri"]
        fetch = client.get(_artifacts_url(), headers=_hdr(), params={"uri": uri})
        assert fetch.status_code == 200
        assert fetch.content == payload
        assert fetch.headers["content-type"].startswith("application/octet-stream")
        assert fetch.headers["x-content-type-options"] == "nosniff"
        assert fetch.headers["content-disposition"].startswith("attachment")

    def test_fetch_preserves_exact_text_content_type(
        self, store: InMemoryStore
    ) -> None:
        # §16.2: fetch returns the content_type exactly as recorded — no
        # Starlette "; charset=utf-8" mutation on a text/* type.
        client = TestClient(make_app(store))
        deposit = client.post(
            _artifacts_url(),
            headers=_hdr(),
            files={"file": ("note.md", b"# hi", "text/markdown")},
        )
        uri = deposit.json()["artifacts_uri"]
        fetch = client.get(_artifacts_url(), headers=_hdr(), params={"uri": uri})
        assert fetch.headers["content-type"] == "text/markdown"

    def test_fetch_unknown_id_returns_404(self, store: InMemoryStore) -> None:
        client = TestClient(make_app(store))
        resp = client.get(
            _artifacts_url(),
            headers=_hdr(),
            params={"uri": "eden://artifacts/" + "0" * 32},
        )
        assert resp.status_code == 404
        assert resp.json()["type"] == "eden://error/not-found"

    def test_fetch_missing_uri_returns_400(self, store: InMemoryStore) -> None:
        client = TestClient(make_app(store))
        resp = client.get(_artifacts_url(), headers=_hdr())
        assert resp.status_code == 400
        assert resp.json()["type"] == "eden://error/bad-request"

    def test_fetch_unrecognized_uri_returns_404(self, store: InMemoryStore) -> None:
        client = TestClient(make_app(store))
        resp = client.get(
            _artifacts_url(), headers=_hdr(), params={"uri": "s3://bucket/key"}
        )
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

    def test_multiple_parts_rejected(self, store: InMemoryStore) -> None:
        # §16.1: exactly one 'file' part. A second part (or a stray field)
        # makes the body ambiguous → 400, not a silent pick.
        client = TestClient(make_app(store))
        resp = client.post(
            _artifacts_url(),
            headers=_hdr(),
            files={
                "file": ("a", b"first", "text/plain"),
                "file2": ("b", b"second", "text/plain"),
            },
        )
        assert resp.status_code == 400
        assert resp.json()["type"] == "eden://error/bad-request"

    def test_malformed_multipart_returns_problem_json_400(
        self, store: InMemoryStore
    ) -> None:
        # A multipart content-type with no boundary makes Starlette's parser
        # raise; the handler must map it to problem+json bad-request, not let
        # FastAPI emit its default {"detail": ...} body.
        client = TestClient(make_app(store))
        resp = client.post(
            _artifacts_url(),
            headers=_hdr({"Content-Type": "multipart/form-data"}),
            content=b"not really multipart",
        )
        assert resp.status_code == 400
        assert "problem+json" in resp.headers.get("content-type", "")
        assert resp.json()["type"] == "eden://error/bad-request"


# ----------------------------------------------------------------------
# Auth-enabled posture (§16.2 per-row ACL)
# ----------------------------------------------------------------------


def _register_worker(client: TestClient, name: str) -> str:
    """Register a worker by display name (#128); return its `wkr_*:token` bearer."""
    resp = client.post(
        f"/v0/experiments/{EXPERIMENT_ID}/workers",
        headers=_hdr({"Authorization": f"Bearer admin:{ADMIN_TOKEN}"}),
        json={"name": name},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return f"{body['worker_id']}:{body['registration_token']}"


def _register_group(client: TestClient, name: str, member_bearers: list[str]) -> None:
    """Create a group by display name (#128) with the given members (by minted id)."""
    members = [b.split(":", 1)[0] for b in member_bearers]
    resp = client.post(
        f"/v0/experiments/{EXPERIMENT_ID}/groups",
        headers=_hdr({"Authorization": f"Bearer admin:{ADMIN_TOKEN}"}),
        json={"name": name, "members": members},
    )
    assert resp.status_code == 200, resp.text


def _fetch_by_uri(client: TestClient, uri: str, bearer: str | None = None):
    extra = {"Authorization": bearer} if bearer else None
    return client.get(_artifacts_url(), headers=_hdr(extra), params={"uri": uri})


class TestFetchACL:
    def _deposit_as(self, client: TestClient, bearer: str) -> str:
        resp = client.post(
            _artifacts_url(),
            headers=_hdr({"Authorization": bearer}),
            files={"file": ("a", b"secret-bytes", "text/plain")},
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["artifacts_uri"]

    def test_depositor_can_fetch_admin_can_fetch_others_cannot(
        self, store: InMemoryStore
    ) -> None:
        client = TestClient(make_app(store, admin_token=ADMIN_TOKEN))
        alice = f"Bearer {_register_worker(client, 'alice')}"
        bob = f"Bearer {_register_worker(client, 'bob')}"
        admin = f"Bearer admin:{ADMIN_TOKEN}"

        uri = self._deposit_as(client, alice)

        # Depositor fetches its own.
        assert _fetch_by_uri(client, uri, alice).status_code == 200
        # Admin bearer fetches anyone's.
        assert _fetch_by_uri(client, uri, admin).status_code == 200
        # A different worker is refused.
        forbidden = _fetch_by_uri(client, uri, bob)
        assert forbidden.status_code == 403
        assert forbidden.json()["type"] == "eden://error/forbidden"

    def test_admins_group_member_can_fetch(self, store: InMemoryStore) -> None:
        client = TestClient(make_app(store, admin_token=ADMIN_TOKEN))
        alice = _register_worker(client, "alice")
        carol = _register_worker(client, "carol")
        _register_group(client, "admins", [carol])

        uri = self._deposit_as(client, f"Bearer {alice}")
        resp = _fetch_by_uri(client, uri, f"Bearer {carol}")
        assert resp.status_code == 200

    def test_admin_deposit_attributed_to_admin(self, store: InMemoryStore) -> None:
        client = TestClient(make_app(store, admin_token=ADMIN_TOKEN))
        uri = self._deposit_as(client, f"Bearer admin:{ADMIN_TOKEN}")
        # created_by == "admin"; a worker who is not admin-class is refused.
        dave = _register_worker(client, "dave")
        resp = _fetch_by_uri(client, uri, f"Bearer {dave}")
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
        # fetch_artifact takes the full opaque URI verbatim.
        assert sc.fetch_artifact(result.artifacts_uri) == payload

    def test_deposit_with_injected_json_content_type_client(
        self, store: InMemoryStore
    ) -> None:
        # A caller-injected client whose default Content-Type is JSON must
        # still produce a valid multipart deposit (the helper sets the
        # boundary Content-Type explicitly).
        app = make_app(store)
        client = TestClient(
            app,
            base_url="http://wire.test",
            headers={"Content-Type": "application/json"},
        )
        sc = StoreClient("http://wire.test", EXPERIMENT_ID, client=client)
        result = sc.deposit_artifact(b"payload", content_type="text/plain")
        assert sc.fetch_artifact(result.artifacts_uri) == b"payload"

    def test_fetch_unknown_raises_not_found(self, store: InMemoryStore) -> None:
        sc = self._store_client(store)
        with pytest.raises(NotFound):
            sc.fetch_artifact("eden://artifacts/" + "0" * 32)

    def test_fetch_forbidden_raises(self, store: InMemoryStore) -> None:
        # Auth-enabled: a different worker's fetch maps the 403 envelope
        # back to Forbidden client-side.
        app = make_app(store, admin_token=ADMIN_TOKEN)
        client = TestClient(app, base_url="http://wire.test")
        alice_bearer = _register_worker(client, "alice")  # "wkr_…:token"
        bob_bearer = _register_worker(client, "bob")
        alice_sc = StoreClient(
            "http://wire.test", EXPERIMENT_ID, client=client, bearer=alice_bearer
        )
        bob_sc = StoreClient(
            "http://wire.test", EXPERIMENT_ID, client=client, bearer=bob_bearer
        )
        uri = alice_sc.deposit_artifact(b"x").artifacts_uri
        with pytest.raises(Forbidden):
            bob_sc.fetch_artifact(uri)
