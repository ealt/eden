"""Tests for the §13 normative auth scheme (per-worker + admin bearer).

Covers:

- Bearer parsing (`<principal>:<secret>` form; missing / malformed
  rejected with ``eden://error/unauthorized`` 401).
- Admin authentication (constant-time match of ``admin:<token>``).
- Worker authentication (Store-side ``verify_worker_credential``).
- Endpoint authorization (admin-gated vs worker-gated; 403
  ``eden://error/forbidden`` on principal-class mismatch).
- ``GET /v0/.../whoami`` returns the bearer's ``worker_id``.
- ``StoreClient(bearer=...)`` sets the right Authorization header.
- Server built without ``admin_token`` admits anonymous requests
  (regression guard on the test posture).
"""

from __future__ import annotations

import httpx
import pytest
from eden_contracts import EvaluationSchema
from eden_storage import InMemoryStore
from eden_wire import Forbidden, StoreClient, Unauthorized, make_app
from fastapi.testclient import TestClient

EXPERIMENT_ID = "exp-auth"
ADMIN_TOKEN = "test-admin-token-abcdef"


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore(
        experiment_id=EXPERIMENT_ID,
        evaluation_schema=EvaluationSchema({"loss": "real"}),
    )


def _events_url() -> str:
    return f"/v0/experiments/{EXPERIMENT_ID}/events"


def _workers_url() -> str:
    return f"/v0/experiments/{EXPERIMENT_ID}/workers"


def _whoami_url() -> str:
    return f"/v0/experiments/{EXPERIMENT_ID}/whoami"


# ----------------------------------------------------------------------
# Server-side bearer parsing + authentication
# ----------------------------------------------------------------------


def test_admin_bearer_admitted(store: InMemoryStore) -> None:
    app = make_app(store, admin_token=ADMIN_TOKEN)
    client = TestClient(app)
    resp = client.get(
        _events_url(),
        headers={
            "X-Eden-Experiment-Id": EXPERIMENT_ID,
            "Authorization": f"Bearer admin:{ADMIN_TOKEN}",
        },
    )
    assert resp.status_code == 200


def test_worker_bearer_admitted(store: InMemoryStore) -> None:
    app = make_app(store, admin_token=ADMIN_TOKEN)
    client = TestClient(app)
    # Register a worker via the admin bearer to obtain a credential.
    register = client.post(
        _workers_url(),
        headers={
            "X-Eden-Experiment-Id": EXPERIMENT_ID,
            "Authorization": f"Bearer admin:{ADMIN_TOKEN}",
        },
        json={"worker_id": "eric"},
    )
    assert register.status_code == 200
    token = register.json()["registration_token"]
    # Now hit a worker-readable endpoint with the worker bearer.
    resp = client.get(
        _events_url(),
        headers={
            "X-Eden-Experiment-Id": EXPERIMENT_ID,
            "Authorization": f"Bearer eric:{token}",
        },
    )
    assert resp.status_code == 200


def test_missing_authorization_rejected(store: InMemoryStore) -> None:
    app = make_app(store, admin_token=ADMIN_TOKEN)
    client = TestClient(app)
    resp = client.get(
        _events_url(),
        headers={"X-Eden-Experiment-Id": EXPERIMENT_ID},
    )
    assert resp.status_code == 401
    body = resp.json()
    assert body["type"] == "eden://error/unauthorized"


def test_wrong_admin_token_rejected(store: InMemoryStore) -> None:
    app = make_app(store, admin_token=ADMIN_TOKEN)
    client = TestClient(app)
    resp = client.get(
        _events_url(),
        headers={
            "X-Eden-Experiment-Id": EXPERIMENT_ID,
            "Authorization": "Bearer admin:WRONG",
        },
    )
    assert resp.status_code == 401
    assert resp.json()["type"] == "eden://error/unauthorized"


def test_wrong_worker_token_rejected(store: InMemoryStore) -> None:
    """Unknown worker_id and wrong secret both produce 401 (no oracle)."""
    app = make_app(store, admin_token=ADMIN_TOKEN)
    client = TestClient(app)
    resp = client.get(
        _events_url(),
        headers={
            "X-Eden-Experiment-Id": EXPERIMENT_ID,
            "Authorization": "Bearer ghost:nope",
        },
    )
    assert resp.status_code == 401
    assert resp.json()["type"] == "eden://error/unauthorized"


def test_bearer_without_colon_rejected(store: InMemoryStore) -> None:
    """§13.1: bearer MUST be ``<principal>:<secret>``."""
    app = make_app(store, admin_token=ADMIN_TOKEN)
    client = TestClient(app)
    resp = client.get(
        _events_url(),
        headers={
            "X-Eden-Experiment-Id": EXPERIMENT_ID,
            "Authorization": "Bearer no-colon-here",
        },
    )
    assert resp.status_code == 401


def test_basic_scheme_rejected(store: InMemoryStore) -> None:
    app = make_app(store, admin_token=ADMIN_TOKEN)
    client = TestClient(app)
    resp = client.get(
        _events_url(),
        headers={
            "X-Eden-Experiment-Id": EXPERIMENT_ID,
            "Authorization": f"Basic admin:{ADMIN_TOKEN}",
        },
    )
    assert resp.status_code == 401


# ----------------------------------------------------------------------
# Endpoint authorization (admin-gated vs worker-gated)
# ----------------------------------------------------------------------


def test_admin_route_rejects_worker_bearer(store: InMemoryStore) -> None:
    """`POST /workers` is admin-gated; a worker bearer hits 403 forbidden."""
    app = make_app(store, admin_token=ADMIN_TOKEN)
    client = TestClient(app)
    register = client.post(
        _workers_url(),
        headers={
            "X-Eden-Experiment-Id": EXPERIMENT_ID,
            "Authorization": f"Bearer admin:{ADMIN_TOKEN}",
        },
        json={"worker_id": "eric"},
    )
    token = register.json()["registration_token"]
    resp = client.post(
        _workers_url(),
        headers={
            "X-Eden-Experiment-Id": EXPERIMENT_ID,
            "Authorization": f"Bearer eric:{token}",
        },
        json={"worker_id": "alice"},
    )
    assert resp.status_code == 403
    assert resp.json()["type"] == "eden://error/forbidden"


def test_worker_route_rejects_admin_bearer(store: InMemoryStore) -> None:
    """`GET /whoami` is worker-gated per §6.4; admin bearer 403s."""
    app = make_app(store, admin_token=ADMIN_TOKEN)
    client = TestClient(app)
    resp = client.get(
        _whoami_url(),
        headers={
            "X-Eden-Experiment-Id": EXPERIMENT_ID,
            "Authorization": f"Bearer admin:{ADMIN_TOKEN}",
        },
    )
    assert resp.status_code == 403


# ----------------------------------------------------------------------
# whoami
# ----------------------------------------------------------------------


def test_whoami_returns_authenticated_worker(store: InMemoryStore) -> None:
    app = make_app(store, admin_token=ADMIN_TOKEN)
    client = TestClient(app)
    register = client.post(
        _workers_url(),
        headers={
            "X-Eden-Experiment-Id": EXPERIMENT_ID,
            "Authorization": f"Bearer admin:{ADMIN_TOKEN}",
        },
        json={"worker_id": "eric"},
    )
    token = register.json()["registration_token"]
    resp = client.get(
        _whoami_url(),
        headers={
            "X-Eden-Experiment-Id": EXPERIMENT_ID,
            "Authorization": f"Bearer eric:{token}",
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"worker_id": "eric"}


# ----------------------------------------------------------------------
# Anonymous (test-only) posture
# ----------------------------------------------------------------------


def test_default_admits_anonymous(store: InMemoryStore) -> None:
    """No ``admin_token`` → no auth required (test / in-process posture)."""
    app = make_app(store)
    client = TestClient(app)
    resp = client.get(
        _events_url(),
        headers={"X-Eden-Experiment-Id": EXPERIMENT_ID},
    )
    assert resp.status_code == 200


# ----------------------------------------------------------------------
# Client-side bearer header
# ----------------------------------------------------------------------


class _HeaderCapture:
    def __init__(self) -> None:
        self.seen: list[dict[str, str]] = []

    def transport(self) -> httpx.MockTransport:
        def _handler(request: httpx.Request) -> httpx.Response:
            self.seen.append(dict(request.headers))
            return httpx.Response(200, json={"events": [], "cursor": 0})

        return httpx.MockTransport(_handler)


def test_client_sends_bearer_when_set() -> None:
    capture = _HeaderCapture()
    bearer = f"admin:{ADMIN_TOKEN}"
    with httpx.Client(transport=capture.transport(), base_url="http://unused") as http:
        client = StoreClient(
            "http://unused",
            experiment_id=EXPERIMENT_ID,
            bearer=bearer,
            client=http,
        )
        client.read_range()
    assert capture.seen[0].get("authorization") == f"Bearer {bearer}"


def test_client_omits_authorization_when_no_bearer() -> None:
    capture = _HeaderCapture()
    with httpx.Client(transport=capture.transport(), base_url="http://unused") as http:
        client = StoreClient(
            "http://unused",
            experiment_id=EXPERIMENT_ID,
            client=http,
        )
        client.read_range()
    assert "authorization" not in capture.seen[0]


# ----------------------------------------------------------------------
# Round-trip: server rejects → client raises
# ----------------------------------------------------------------------


def _proxy_to_app(app_client: TestClient) -> httpx.MockTransport:
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


def test_unauthorized_round_trip(store: InMemoryStore) -> None:
    app = make_app(store, admin_token=ADMIN_TOKEN)
    test_client = TestClient(app)
    with httpx.Client(
        transport=_proxy_to_app(test_client), base_url="http://unused"
    ) as http:
        client = StoreClient(
            "http://unused",
            experiment_id=EXPERIMENT_ID,
            bearer="admin:WRONG",
            client=http,
        )
        with pytest.raises(Unauthorized):
            client.read_range()


def test_forbidden_round_trip(store: InMemoryStore) -> None:
    """Worker bearer hitting an admin-gated endpoint raises Forbidden."""
    app = make_app(store, admin_token=ADMIN_TOKEN)
    test_client = TestClient(app)
    register = test_client.post(
        _workers_url(),
        headers={
            "X-Eden-Experiment-Id": EXPERIMENT_ID,
            "Authorization": f"Bearer admin:{ADMIN_TOKEN}",
        },
        json={"worker_id": "eric"},
    )
    token = register.json()["registration_token"]
    with httpx.Client(
        transport=_proxy_to_app(test_client), base_url="http://unused"
    ) as http:
        client = StoreClient(
            "http://unused",
            experiment_id=EXPERIMENT_ID,
            bearer=f"eric:{token}",
            client=http,
        )
        with pytest.raises(Forbidden):
            client.register_worker("alice")


# ----------------------------------------------------------------------
# Auth-matrix coverage on task/idea/variant mutation routes (12a-1
# codex-review #2).
# ----------------------------------------------------------------------


def _register_worker(client: TestClient, worker_id: str) -> str:
    """Helper: register worker via admin bearer; return the token."""
    resp = client.post(
        _workers_url(),
        headers={
            "X-Eden-Experiment-Id": EXPERIMENT_ID,
            "Authorization": f"Bearer admin:{ADMIN_TOKEN}",
        },
        json={"worker_id": worker_id},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["registration_token"]


_TASK_MUTATIONS = [
    ("POST", "/tasks", {"task_id": "t1", "kind": "ideation", "state": "pending",
                        "payload": {"experiment_id": EXPERIMENT_ID},
                        "created_at": "2026-05-01T00:00:00Z",
                        "updated_at": "2026-05-01T00:00:00Z"}),
    ("POST", "/tasks/t1/claim", {}),
    (
        "POST",
        "/tasks/t1/submit",
        {"payload": {"kind": "ideation", "status": "success", "idea_ids": []}},
    ),
    ("POST", "/tasks/t1/accept", None),
    ("POST", "/tasks/t1/reject", {"reason": "validation_error"}),
    ("POST", "/tasks/t1/reclaim", {"cause": "operator"}),
]

_IDEA_MUTATIONS = [
    ("POST", "/ideas", {"idea_id": "p1", "experiment_id": EXPERIMENT_ID,
                        "slug": "x", "priority": 0.5, "state": "drafting",
                        "parent_commits": ["0" * 40],
                        "artifacts_uri": "file:///tmp/x",
                        "created_at": "2026-05-01T00:00:00Z",
                        "updated_at": "2026-05-01T00:00:00Z"}),
    ("POST", "/ideas/p1/mark-ready", None),
]

_VARIANT_MUTATIONS = [
    ("POST", "/variants", {"variant_id": "v1", "experiment_id": EXPERIMENT_ID,
                           "idea_id": "p1", "status": "starting",
                           "parent_commits": ["0" * 40],
                           "started_at": "2026-05-01T00:00:00Z"}),
    ("POST", "/variants/v1/declare-evaluation-error", None),
    ("POST", "/variants/v1/integrate", {"variant_commit_sha": "a" * 40}),
]


@pytest.mark.parametrize(
    ("method", "path", "body"),
    _TASK_MUTATIONS + _IDEA_MUTATIONS + _VARIANT_MUTATIONS,
)
def test_admin_bearer_rejected_on_worker_gated_mutations(
    store: InMemoryStore, method: str, path: str, body: dict | None
) -> None:
    """§13.3 — admin bearer hitting any worker-gated mutation returns 403.

    Confirms the auth-matrix dispatcher runs before the route handler
    so the test doesn't depend on the resource existing. Each route
    is exercised through ``_enforce_worker``.
    """
    app = make_app(store, admin_token=ADMIN_TOKEN)
    client = TestClient(app)
    headers = {
        "X-Eden-Experiment-Id": EXPERIMENT_ID,
        "Authorization": f"Bearer admin:{ADMIN_TOKEN}",
    }
    if body is not None:
        resp = client.request(
            method, f"/v0/experiments/{EXPERIMENT_ID}{path}", headers=headers, json=body
        )
    else:
        resp = client.request(
            method, f"/v0/experiments/{EXPERIMENT_ID}{path}", headers=headers
        )
    assert resp.status_code == 403, (
        f"{method} {path} returned {resp.status_code}: {resp.text}"
    )
    assert resp.json()["type"] == "eden://error/forbidden"


@pytest.mark.parametrize(
    ("method", "path", "body"),
    _TASK_MUTATIONS + _IDEA_MUTATIONS + _VARIANT_MUTATIONS,
)
def test_worker_bearer_admitted_through_auth_layer_on_mutations(
    store: InMemoryStore, method: str, path: str, body: dict | None
) -> None:
    """§13.3 — worker bearer passes the auth-class gate on every mutation route.

    The route handler may still reject for other reasons (404 not-found,
    409 illegal-transition, 400 bad-request) — what we're asserting here
    is that NONE of the rejections are 403 forbidden, i.e. the
    auth-class gate admitted the worker bearer. Anything that gets past
    ``_enforce_worker`` is acceptable for this test.
    """
    app = make_app(store, admin_token=ADMIN_TOKEN)
    client = TestClient(app)
    token = _register_worker(client, "eric")
    headers = {
        "X-Eden-Experiment-Id": EXPERIMENT_ID,
        "Authorization": f"Bearer eric:{token}",
    }
    if body is not None:
        resp = client.request(
            method, f"/v0/experiments/{EXPERIMENT_ID}{path}", headers=headers, json=body
        )
    else:
        resp = client.request(
            method, f"/v0/experiments/{EXPERIMENT_ID}{path}", headers=headers
        )
    # Auth-class gate admitted; the rest is up to the route handler.
    # 403 specifically would mean the gate rejected — that's the failure.
    assert resp.status_code != 403, (
        f"§13.3 violated: worker bearer rejected as 403 on worker-gated "
        f"{method} {path} (body: {resp.text})"
    )
