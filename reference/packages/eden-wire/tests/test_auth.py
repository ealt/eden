"""Tests for the reference-only shared-token auth middleware (§12).

Three surfaces are covered:

- Server-side middleware accepts the correct bearer token and rejects
  missing / wrong-scheme / wrong-value requests with a problem+json
  envelope under the ``eden://reference-error/unauthorized`` type.
- Server built without ``shared_token`` admits anonymous requests
  unchanged (regression guard on the default path).
- ``StoreClient(token=...)`` sets the ``Authorization`` header on
  every request; without ``token`` it sends no such header.
"""

from __future__ import annotations

import httpx
import pytest
from eden_contracts import MetricsSchema
from eden_storage import InMemoryStore
from eden_wire import StoreClient, Unauthorized, make_app
from fastapi.testclient import TestClient


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore(
        experiment_id="exp-auth",
        metrics_schema=MetricsSchema({"loss": "real"}),
    )


@pytest.fixture
def token() -> str:
    return "test-token-abcdef"


def _bare_events_url(experiment_id: str) -> str:
    return f"/v0/experiments/{experiment_id}/events"


# ----------------------------------------------------------------------
# Server middleware
# ----------------------------------------------------------------------


def test_server_admits_request_with_correct_bearer_token(
    store: InMemoryStore, token: str
) -> None:
    app = make_app(store, shared_token=token)
    client = TestClient(app)
    resp = client.get(
        _bare_events_url(store.experiment_id),
        headers={
            "X-Eden-Experiment-Id": store.experiment_id,
            "Authorization": f"Bearer {token}",
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"events": [], "cursor": 0}


def test_server_rejects_missing_authorization_header(
    store: InMemoryStore, token: str
) -> None:
    app = make_app(store, shared_token=token)
    client = TestClient(app)
    resp = client.get(
        _bare_events_url(store.experiment_id),
        headers={"X-Eden-Experiment-Id": store.experiment_id},
    )
    assert resp.status_code == 401
    body = resp.json()
    assert body["type"] == "eden://reference-error/unauthorized"
    assert body["status"] == 401


def test_server_rejects_wrong_bearer_token(store: InMemoryStore, token: str) -> None:
    app = make_app(store, shared_token=token)
    client = TestClient(app)
    resp = client.get(
        _bare_events_url(store.experiment_id),
        headers={
            "X-Eden-Experiment-Id": store.experiment_id,
            "Authorization": "Bearer WRONG",
        },
    )
    assert resp.status_code == 401
    assert resp.json()["type"] == "eden://reference-error/unauthorized"


def test_server_rejects_wrong_scheme(store: InMemoryStore, token: str) -> None:
    app = make_app(store, shared_token=token)
    client = TestClient(app)
    resp = client.get(
        _bare_events_url(store.experiment_id),
        headers={
            "X-Eden-Experiment-Id": store.experiment_id,
            "Authorization": f"Basic {token}",
        },
    )
    assert resp.status_code == 401
    assert resp.json()["type"] == "eden://reference-error/unauthorized"


def test_server_default_admits_anonymous(store: InMemoryStore) -> None:
    """Regression guard: no ``shared_token`` → no auth required."""
    app = make_app(store)
    client = TestClient(app)
    resp = client.get(
        _bare_events_url(store.experiment_id),
        headers={"X-Eden-Experiment-Id": store.experiment_id},
    )
    assert resp.status_code == 200


# ----------------------------------------------------------------------
# Client header behavior
# ----------------------------------------------------------------------


class _HeaderCapture:
    def __init__(self) -> None:
        self.seen: list[dict[str, str]] = []

    def transport(self) -> httpx.MockTransport:
        def _handler(request: httpx.Request) -> httpx.Response:
            self.seen.append(dict(request.headers))
            # Minimal valid events response.
            return httpx.Response(200, json={"events": [], "cursor": 0})

        return httpx.MockTransport(_handler)


def test_client_sends_bearer_when_token_set(token: str) -> None:
    capture = _HeaderCapture()
    with httpx.Client(transport=capture.transport(), base_url="http://unused") as http:
        client = StoreClient(
            "http://unused",
            experiment_id="exp-auth",
            token=token,
            client=http,
        )
        client.read_range()
    assert capture.seen, "client did not issue an HTTP request"
    assert capture.seen[0].get("authorization") == f"Bearer {token}"


def test_client_omits_authorization_when_no_token() -> None:
    capture = _HeaderCapture()
    with httpx.Client(transport=capture.transport(), base_url="http://unused") as http:
        client = StoreClient(
            "http://unused",
            experiment_id="exp-auth",
            client=http,
        )
        client.read_range()
    assert capture.seen
    assert "authorization" not in capture.seen[0]


# ----------------------------------------------------------------------
# Round-trip: server rejects → client raises Unauthorized
# ----------------------------------------------------------------------


def test_unauthorized_round_trip(store: InMemoryStore, token: str) -> None:
    """Real server + real client: wrong token surfaces as ``Unauthorized``."""
    app = make_app(store, shared_token=token)
    test_client = TestClient(app)

    def _handler(request: httpx.Request) -> httpx.Response:
        # Relay to the in-process app via TestClient so FastAPI exercises
        # the middleware on a real request.
        response = test_client.request(
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

    transport = httpx.MockTransport(_handler)
    with httpx.Client(transport=transport, base_url="http://unused") as http:
        client = StoreClient(
            "http://unused",
            experiment_id=store.experiment_id,
            token="WRONG-TOKEN",
            client=http,
        )
        with pytest.raises(Unauthorized):
            client.read_range()


def test_reference_error_type_not_in_normative_vocab() -> None:
    """Guard: ``Unauthorized`` is NOT registered in the normative vocab."""
    from eden_wire.errors import _EXC_BY_TYPE, _REF_EXC_BY_TYPE

    # Unauthorized lives in the reference-only table.
    assert _REF_EXC_BY_TYPE["eden://reference-error/unauthorized"] is Unauthorized
    # And nowhere in the normative table.
    normative_types = set(_EXC_BY_TYPE.keys())
    reference_types = {t for t in normative_types if t.startswith("eden://reference-error/")}
    assert reference_types == set(), (
        "reference-only types leaked into the normative vocabulary: "
        f"{reference_types}"
    )
