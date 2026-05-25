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
    """§13.3 + §3.7 — worker bearer in the right group passes the auth-class gate.

    The route handler may still reject for other reasons (404 not-found,
    409 illegal-transition, 400 bad-request) — what we're asserting here
    is that NONE of the rejections are 403 forbidden, i.e. the
    auth-class gate admitted the worker bearer. Anything that gets past
    the gate is acceptable for this test.

    Post-12a-2 §3.7, several routes are group-gated (``POST /tasks``,
    accept / reject, ``integrate_variant``); the worker MUST be a
    member of ``admins`` AND ``orchestrators`` to pass every gate.
    """
    app = make_app(store, admin_token=ADMIN_TOKEN)
    client = TestClient(app)
    token = _register_worker(client, "eric")
    admin_headers = {
        "X-Eden-Experiment-Id": EXPERIMENT_ID,
        "Authorization": f"Bearer admin:{ADMIN_TOKEN}",
    }
    # Bootstrap the §3.7 authority groups and put eric in both.
    for group_id in ("admins", "orchestrators"):
        reg = client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/groups",
            headers=admin_headers,
            json={"group_id": group_id, "members": ["eric"]},
        )
        assert reg.status_code == 200, reg.text
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


# ----------------------------------------------------------------------
# StoreClient bearer / worker_id preflight + new Store-protocol methods
# (codex round-3 #1, #2)
# ----------------------------------------------------------------------


def test_storeclient_claim_rejects_mismatched_worker_id(store: InMemoryStore) -> None:
    """Codex round-3 #2 — claim by a worker_id that disagrees with the bearer 400s client-side.

    Per chapter 04 §3.3 the authenticated identity is the load-bearing
    one. A caller passing a mismatched ``worker_id`` would otherwise
    be silently re-bound to the bearer's identity by the server; the
    client must surface the disagreement at the wire edge.
    """
    app = make_app(store, admin_token=ADMIN_TOKEN)
    test_client = TestClient(app)
    # Register two workers, then build a StoreClient authenticated as
    # the first but ask it to claim on behalf of the second.
    register = test_client.post(
        _workers_url(),
        headers={
            "X-Eden-Experiment-Id": EXPERIMENT_ID,
            "Authorization": f"Bearer admin:{ADMIN_TOKEN}",
        },
        json={"worker_id": "eric"},
    )
    eric_token = register.json()["registration_token"]
    with httpx.Client(
        transport=_proxy_to_app(test_client), base_url="http://unused"
    ) as http:
        client = StoreClient(
            "http://unused",
            experiment_id=EXPERIMENT_ID,
            bearer=f"eric:{eric_token}",
            client=http,
        )
        with pytest.raises(ValueError, match="disagrees with bearer principal"):
            client.claim("t-mismatch", "alice")


def test_storeclient_submit_rejects_mismatched_worker_id(store: InMemoryStore) -> None:
    """Codex round-3 #2 — submit by a worker_id that disagrees with the bearer 400s client-side."""
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
    eric_token = register.json()["registration_token"]
    from eden_storage import IdeaSubmission

    with httpx.Client(
        transport=_proxy_to_app(test_client), base_url="http://unused"
    ) as http:
        client = StoreClient(
            "http://unused",
            experiment_id=EXPERIMENT_ID,
            bearer=f"eric:{eric_token}",
            client=http,
        )
        with pytest.raises(ValueError, match="disagrees with bearer principal"):
            client.submit(
                "t-mismatch",
                "alice",
                IdeaSubmission(status="success", idea_ids=()),
            )


def test_storeclient_claim_with_no_bearer_skips_preflight(
    store: InMemoryStore,
) -> None:
    """Auth-disabled mode (no bearer) passes through; the preflight is a no-op."""
    # Server with admin_token=None admits anonymous requests; the
    # caller-supplied worker_id is ignored at the wire and the server
    # collapses the principal to the ``anonymous`` sentinel. The
    # client preflight short-circuits on no-bearer (no
    # ValueError); the server then rejects ``anonymous`` via the
    # §3.5 registration ladder.
    store.create_ideation_task("t-noauth")
    app = make_app(store, admin_token=None)
    test_client = TestClient(app)
    with httpx.Client(
        transport=_proxy_to_app(test_client), base_url="http://unused"
    ) as http:
        client = StoreClient(
            "http://unused",
            experiment_id=EXPERIMENT_ID,
            client=http,
        )
        from eden_storage import WorkerNotRegistered

        with pytest.raises(WorkerNotRegistered):
            client.claim("t-noauth", "eric")


def test_storeclient_verify_worker_credential_wire(store: InMemoryStore) -> None:
    """Codex round-3 #1 / round-4 #B — verify_worker_credential resolves via /whoami over the wire.

    Drives the verify path through the test's injected mock-transport
    httpx.Client. After round-4 #B, the verify call MUST reuse the
    parent client's transport (without that, the call would try to
    reach the real network and the mock-transport client would never
    see the request).
    """
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
        # Verify is called from a fresh admin-bearer client; the
        # candidate (worker) bearer is supplied as args.
        admin = StoreClient(
            "http://unused",
            experiment_id=EXPERIMENT_ID,
            bearer=f"admin:{ADMIN_TOKEN}",
            client=http,
        )
        # Correct credential → True.
        assert admin.verify_worker_credential("eric", token) is True
        # Wrong secret → False.
        assert admin.verify_worker_credential("eric", "wrong-secret") is False
        # Unknown worker → False.
        assert admin.verify_worker_credential("ghost", token) is False


def test_storeclient_resolve_worker_in_group_walks_groups(
    store: InMemoryStore,
) -> None:
    """Codex round-3 #1 — resolve_worker_in_group walks the group DAG."""
    # Seed via the in-process store, then resolve through the wire client.
    store.register_worker("eric")
    store.register_group("team-a", members=["eric"])
    store.register_group("humans", members=["team-a"])
    app = make_app(store, admin_token=ADMIN_TOKEN)
    test_client = TestClient(app)
    with httpx.Client(
        transport=_proxy_to_app(test_client), base_url="http://unused"
    ) as http:
        client = StoreClient(
            "http://unused",
            experiment_id=EXPERIMENT_ID,
            bearer=f"admin:{ADMIN_TOKEN}",
            client=http,
        )
        assert client.resolve_worker_in_group("eric", "humans") is True
        assert client.resolve_worker_in_group("alice", "humans") is False
        assert client.resolve_worker_in_group("eric", "non-existent") is False


def test_storeclient_verify_propagates_transport_failures(
    store: InMemoryStore,
) -> None:
    """Codex round-5 R5-A — transport failures MUST NOT collapse to False.

    A bootstrap flow conflating "transport hiccup" with "credential is
    bad" would reissue on a transient network blip; verify_worker_credential
    only returns False on a confirmed-bad-credential outcome (401).
    """
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

    def _exploding_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated transport failure")

    with httpx.Client(
        transport=httpx.MockTransport(_exploding_handler),
        base_url="http://unused",
    ) as http:
        client = StoreClient(
            "http://unused",
            experiment_id=EXPERIMENT_ID,
            bearer=f"admin:{ADMIN_TOKEN}",
            client=http,
        )
        with pytest.raises(httpx.ConnectError):
            client.verify_worker_credential("eric", token)


def test_storeclient_resolve_in_group_only_swallows_not_found(
    store: InMemoryStore,
) -> None:
    """Codex round-5 R5-B — only NotFound on a member is swallowed.

    Auth failures, transport errors, etc. propagate so callers can
    distinguish "we don't know" from "confirmed not a member".
    """
    _ = store  # fixture only ensures InMemoryStore is constructable

    def _exploding_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated read timeout")

    with httpx.Client(
        transport=httpx.MockTransport(_exploding_handler),
        base_url="http://unused",
    ) as http:
        client = StoreClient(
            "http://unused",
            experiment_id=EXPERIMENT_ID,
            bearer=f"admin:{ADMIN_TOKEN}",
            client=http,
        )
        with pytest.raises(httpx.ReadTimeout):
            client.resolve_worker_in_group("eric", "humans")


def test_storeclient_resolve_in_group_skips_dangling_member(
    store: InMemoryStore,
) -> None:
    """§7.1 — a dangling group reference is a NotFound that the walk skips."""
    # Seed eric directly in humans; humans references a non-existent
    # group "ghost" as another member. The walk MUST skip "ghost"
    # and still find eric via the direct membership.
    store.register_worker("eric")
    store.register_group("humans", members=["eric", "ghost"])
    app = make_app(store, admin_token=ADMIN_TOKEN)
    test_client = TestClient(app)
    with httpx.Client(
        transport=_proxy_to_app(test_client), base_url="http://unused"
    ) as http:
        client = StoreClient(
            "http://unused",
            experiment_id=EXPERIMENT_ID,
            bearer=f"admin:{ADMIN_TOKEN}",
            client=http,
        )
        assert client.resolve_worker_in_group("eric", "humans") is True


def test_storeclient_verify_wrong_worker_id_response_raises(
    store: InMemoryStore,
) -> None:
    """Codex round-7 R7-1 — 200-but-mismatched-worker_id raises, not False.

    The §6.7 "registry rebuilt with same id, different identity"
    recovery branch (or a proxy / server bug) surfaces as a 200 body
    whose worker_id disagrees with the candidate. Conflating it with
    confirmed-bad-credential would trigger an unwarranted reissue;
    the caller must see the mismatch and surface to the operator.
    """
    # Build a mock transport that returns 200 OK from /whoami with a
    # worker_id that disagrees with the candidate.
    def _wrong_id_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/whoami"):
            return httpx.Response(
                200,
                json={"worker_id": "someone-else"},
                headers={"content-type": "application/json"},
            )
        return httpx.Response(404)

    _ = store  # fixture only; no in-process store needed
    with httpx.Client(
        transport=httpx.MockTransport(_wrong_id_handler),
        base_url="http://unused",
    ) as http:
        client = StoreClient(
            "http://unused",
            experiment_id=EXPERIMENT_ID,
            bearer=f"admin:{ADMIN_TOKEN}",
            client=http,
        )
        with pytest.raises(RuntimeError, match="returned worker_id="):
            client.verify_worker_credential("eric", "any-token")
