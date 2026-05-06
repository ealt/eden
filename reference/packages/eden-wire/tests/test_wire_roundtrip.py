"""End-to-end round trip: StoreClient → FastAPI server → InMemoryStore.

Every test here drives an in-process HTTP stack: FastAPI's TestClient
wraps the ASGI app with an httpx-compatible transport so every request
crosses the full serialization path without opening a real socket.
StoreClient is constructed against that same TestClient so its
request plumbing is identical to a subprocess-over-loopback run.

Tests that need to simulate transport-indeterminate failures use
``httpx.MockTransport`` to interpose on the single integrate call,
leaving the rest of the requests (the pre-seed workflow, the
reconciliation read-back) going through the real ASGI stack.
"""

from __future__ import annotations

from typing import Any

import pytest
from eden_contracts import EvaluationSchema, Idea, Variant
from eden_storage import InMemoryStore
from eden_storage.errors import (
    AlreadyExists,
    ConflictingResubmission,
    IllegalTransition,
    InvalidPrecondition,
    NotFound,
    WrongToken,
)
from eden_storage.submissions import (
    EvaluationSubmission,
    IdeaSubmission,
    VariantSubmission,
)
from eden_wire import StoreClient, make_app
from eden_wire.client import IndeterminateIntegration
from eden_wire.errors import ExperimentIdMismatch
from fastapi.testclient import TestClient
from httpx import Client, MockTransport, Request, Response

EXPERIMENT_ID = "exp-wire"
SHORT_SUBSCRIBE_TIMEOUT = 0.2  # keep idle long-polls fast in tests


@pytest.fixture
def store() -> InMemoryStore:
    schema = EvaluationSchema.model_validate({"loss": "real", "acc": "real"})
    return InMemoryStore(EXPERIMENT_ID, evaluation_schema=schema)


@pytest.fixture
def app(store: InMemoryStore) -> Any:
    return make_app(
        store,
        subscribe_timeout=SHORT_SUBSCRIBE_TIMEOUT,
        subscribe_poll_interval=0.02,
    )


@pytest.fixture
def client(app: Any) -> Client:
    return TestClient(app, base_url="http://wire.test")


@pytest.fixture
def store_client(client: Client) -> StoreClient:
    return StoreClient("http://wire.test", EXPERIMENT_ID, client=client)


def _make_idea_body(idea_id: str) -> dict[str, Any]:
    return {
        "idea_id": idea_id,
        "experiment_id": EXPERIMENT_ID,
        "state": "drafting",
        "slug": idea_id,
        "parent_commits": ["a" * 40],
        "rationale": "test",
        "priority": 1.0,
        "artifacts_uri": "file:///tmp/artifacts",
        "created_at": "2026-04-24T00:00:00Z",
        "updated_at": "2026-04-24T00:00:00Z",
    }


def _make_variant_body(variant_id: str, idea_id: str) -> dict[str, Any]:
    return {
        "variant_id": variant_id,
        "experiment_id": EXPERIMENT_ID,
        "idea_id": idea_id,
        "branch": f"work/{variant_id}",
        "parent_commits": ["a" * 40],
        "status": "starting",
        "started_at": "2026-04-24T00:00:00Z",
    }


def _run_variant_to_success(
    store_client: StoreClient,
    *,
    task_prefix: str,
    idea_id: str,
    variant_id: str,
) -> None:
    """Drive a variant to ``status="success"`` via the real Store API.

    This is the seed routine for integrate_variant tests. It goes
    through the full plan → implement → evaluate → accept pipeline
    over HTTP, matching what the real dispatch driver would do.
    No internal-state mutation; no ``model_copy(update=...)``.
    """
    # Plan phase — produces a ready idea.
    store_client.create_ideation_task(f"{task_prefix}-plan")
    claim = store_client.claim(f"{task_prefix}-plan", "ideator")
    store_client.create_idea(Idea.model_validate(_make_idea_body(idea_id)))
    store_client.mark_idea_ready(idea_id)
    store_client.submit(
        f"{task_prefix}-plan",
        claim.token,
        IdeaSubmission(status="success", idea_ids=(idea_id,)),
    )
    store_client.accept(f"{task_prefix}-plan")

    # Implement phase — produces a variant with commit_sha.
    store_client.create_execution_task(f"{task_prefix}-impl", idea_id)
    claim = store_client.claim(f"{task_prefix}-impl", "executor")
    store_client.create_variant(Variant.model_validate(_make_variant_body(variant_id, idea_id)))
    store_client.submit(
        f"{task_prefix}-impl",
        claim.token,
        VariantSubmission(status="success", variant_id=variant_id, commit_sha="b" * 40),
    )
    store_client.accept(f"{task_prefix}-impl")

    # Evaluate phase — transitions variant to success with metrics.
    store_client.create_evaluation_task(f"{task_prefix}-eval", variant_id)
    claim = store_client.claim(f"{task_prefix}-eval", "evaluator")
    store_client.submit(
        f"{task_prefix}-eval",
        claim.token,
        EvaluationSubmission(
            status="success",
            variant_id=variant_id,
            evaluation={"loss": 0.1, "acc": 0.9},
            artifacts_uri="file:///tmp/artifacts",
        ),
    )
    store_client.accept(f"{task_prefix}-eval")


class TestFullExperiment:
    """Plan → implement → evaluate → integrate over HTTP."""

    def test_plan_implement_evaluate_integrate(
        self, store_client: StoreClient
    ) -> None:
        _run_variant_to_success(
            store_client, task_prefix="t1", idea_id="prop-1", variant_id="variant-1"
        )
        variant_commit_sha = "c" * 40
        store_client.integrate_variant("variant-1", variant_commit_sha)

        variants = store_client.list_variants(status="success")
        assert len(variants) == 1
        assert variants[0].variant_commit_sha == variant_commit_sha


class TestErrorEnvelopeRoundtrip:
    """Every StorageError maps to a problem+json body and raises the same class."""

    def test_not_found(self, store_client: StoreClient) -> None:
        with pytest.raises(NotFound):
            store_client.read_task("missing")

    def test_already_exists(self, store_client: StoreClient) -> None:
        store_client.create_ideation_task("dup")
        with pytest.raises(AlreadyExists):
            store_client.create_ideation_task("dup")

    def test_illegal_transition(self, store_client: StoreClient) -> None:
        store_client.create_ideation_task("p1")
        store_client.claim("p1", "w1")
        with pytest.raises(IllegalTransition):
            store_client.claim("p1", "w2")

    def test_wrong_token(self, store_client: StoreClient) -> None:
        store_client.create_ideation_task("p2")
        store_client.claim("p2", "w1")
        with pytest.raises(WrongToken):
            store_client.submit(
                "p2", "not-the-token", IdeaSubmission(status="success")
            )

    def test_conflicting_resubmission(self, store_client: StoreClient) -> None:
        store_client.create_ideation_task("p3")
        claim = store_client.claim("p3", "w1")
        store_client.create_idea(Idea.model_validate(_make_idea_body("pr-a")))
        store_client.mark_idea_ready("pr-a")
        store_client.create_idea(Idea.model_validate(_make_idea_body("pr-b")))
        store_client.mark_idea_ready("pr-b")
        store_client.submit(
            "p3", claim.token, IdeaSubmission(status="success", idea_ids=("pr-a",))
        )
        with pytest.raises(ConflictingResubmission):
            store_client.submit(
                "p3", claim.token, IdeaSubmission(status="success", idea_ids=("pr-b",))
            )

    def test_invalid_precondition(self, store_client: StoreClient) -> None:
        """An integrate call against a non-success variant raises InvalidPrecondition."""
        store_client.create_idea(Idea.model_validate(_make_idea_body("prop-x")))
        store_client.mark_idea_ready("prop-x")
        store_client.create_variant(
            Variant.model_validate(_make_variant_body("t-starting", "prop-x"))
        )
        # Variant is still "starting"; integrate must refuse.
        with pytest.raises(InvalidPrecondition):
            store_client.integrate_variant("t-starting", "c" * 40)


class TestExperimentIdHeader:
    """§1.3 — header-vs-path mismatch is rejected."""

    def test_missing_header(self, client: Client) -> None:
        resp = client.get(f"/v0/experiments/{EXPERIMENT_ID}/tasks")
        assert resp.status_code == 400
        assert resp.json()["type"] == "eden://error/experiment-id-mismatch"

    def test_header_mismatch(self, client: Client) -> None:
        resp = client.get(
            f"/v0/experiments/{EXPERIMENT_ID}/tasks",
            headers={"X-Eden-Experiment-Id": "wrong"},
        )
        assert resp.status_code == 400
        assert resp.json()["type"] == "eden://error/experiment-id-mismatch"


class TestResponseCodes:
    """Spec §2.4, §3, §4, §5: success status codes and bodies."""

    def test_submit_returns_200(self, client: Client, store_client: StoreClient) -> None:
        store_client.create_ideation_task("sc1")
        claim = store_client.claim("sc1", "w")
        resp = client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/tasks/sc1/submit",
            headers={"X-Eden-Experiment-Id": EXPERIMENT_ID},
            json={"token": claim.token, "payload": {"kind": "ideation", "status": "success"}},
        )
        assert resp.status_code == 200

    def test_integrate_returns_200(
        self, client: Client, store_client: StoreClient
    ) -> None:
        _run_variant_to_success(
            store_client, task_prefix="sc2", idea_id="sc2-prop", variant_id="sc2-variant"
        )
        resp = client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/variants/sc2-variant/integrate",
            headers={"X-Eden-Experiment-Id": EXPERIMENT_ID},
            json={"variant_commit_sha": "c" * 40},
        )
        assert resp.status_code == 200

    def test_create_idea_returns_entity(
        self, client: Client
    ) -> None:
        resp = client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/ideas",
            headers={"X-Eden-Experiment-Id": EXPERIMENT_ID},
            json=_make_idea_body("body-prop"),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["idea_id"] == "body-prop"
        assert body["state"] == "drafting"

    def test_create_variant_returns_entity(
        self, client: Client, store_client: StoreClient
    ) -> None:
        store_client.create_idea(
            Idea.model_validate(_make_idea_body("ct-prop"))
        )
        store_client.mark_idea_ready("ct-prop")
        resp = client.post(
            f"/v0/experiments/{EXPERIMENT_ID}/variants",
            headers={"X-Eden-Experiment-Id": EXPERIMENT_ID},
            json=_make_variant_body("ct-variant", "ct-prop"),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["variant_id"] == "ct-variant"
        assert body["status"] == "starting"


class TestIntegrateIdempotency:
    """§5 same-value idempotency; different-SHA divergence."""

    def test_same_sha_idempotent(
        self, store: InMemoryStore, store_client: StoreClient
    ) -> None:
        _run_variant_to_success(
            store_client, task_prefix="idem", idea_id="idem-prop", variant_id="idem-t"
        )
        sha = "c" * 40
        store_client.integrate_variant("idem-t", sha)
        store_client.integrate_variant("idem-t", sha)  # no-op
        integrated_events = [e for e in store.events() if e.type == "variant.integrated"]
        assert len(integrated_events) == 1

    def test_different_sha_rejected(
        self, store_client: StoreClient
    ) -> None:
        _run_variant_to_success(
            store_client, task_prefix="div", idea_id="div-prop", variant_id="div-t"
        )
        store_client.integrate_variant("div-t", "c" * 40)
        with pytest.raises(InvalidPrecondition):
            store_client.integrate_variant("div-t", "d" * 40)


class TestSubscribe:
    """§6.2 long-poll and §6.1 non-blocking polling."""

    def test_events_returns_immediately_when_present(
        self, store_client: StoreClient
    ) -> None:
        store_client.create_ideation_task("ev1")
        events = store_client.read_range()
        assert len(events) >= 1

    def test_subscribe_returns_immediately_when_events_available(
        self, store_client: StoreClient
    ) -> None:
        store_client.create_ideation_task("ev2")
        events = store_client.subscribe(cursor=0, timeout=SHORT_SUBSCRIBE_TIMEOUT)
        assert len(events) >= 1

    def test_subscribe_times_out_on_idle(
        self, store_client: StoreClient
    ) -> None:
        """When no events are available, subscribe returns empty after the
        configured timeout rather than hanging forever."""
        store_client.create_ideation_task("ev3")
        initial = store_client.read_range()
        # All events consumed; a follow-up subscribe should long-poll then
        # return an empty batch after the timeout.
        tail = store_client.subscribe(
            cursor=len(initial), timeout=SHORT_SUBSCRIBE_TIMEOUT
        )
        assert tail == []

    def test_replay_from_zero(self, store_client: StoreClient) -> None:
        store_client.create_ideation_task("r1")
        store_client.create_ideation_task("r2")
        events = store_client.replay()
        assert len(events) == 2
        assert all(e.type == "task.created" for e in events)


def _transport_that_loses(
    app: Any, *, lose_path_substring: str, after_commit: bool
) -> MockTransport:
    """A composite transport that defers to the ASGI app except for one
    specific request path, which it handles specially.

    - ``after_commit=True``: the "server" commits the write first by
      issuing the equivalent request through a ``TestClient`` (which
      runs the ASGI app fully), then raises ``httpx.ConnectError`` on
      the original — simulating the response being lost after durable
      commit.
    - ``after_commit=False``: raises ``httpx.ConnectError`` without
      touching the app — simulates "request never arrived" or
      "server aborted before commit."

    All other requests pass through a real ``TestClient`` wrapping the
    same ASGI app. ``TestClient`` handles the sync-over-async bridge
    internally so we never have to nest event loops ourselves.
    """
    import httpx

    passthrough = TestClient(app, base_url="http://wire.test")

    def _relay(request: Request) -> Response:
        result = passthrough.request(
            request.method,
            str(request.url),
            content=request.content,
            headers=dict(request.headers),
        )
        return Response(
            status_code=result.status_code,
            headers=dict(result.headers),
            content=result.content,
        )

    def handler(request: Request) -> Response:
        if lose_path_substring in str(request.url) and request.method == "POST":
            if after_commit:
                response = _relay(request)
                assert 200 <= response.status_code < 300, (
                    f"simulated server commit expected 2xx, got {response.status_code}"
                )
            raise httpx.ConnectError("simulated transport loss")
        return _relay(request)

    return MockTransport(handler)


class TestIndeterminateIntegration:
    """§5 three-outcome reconciliation on transport-indeterminate failures.

    The tests do not monkeypatch StoreClient; they install a custom
    httpx transport that loses the single ``integrate`` response (with
    or without the server having already committed) and lets every
    other request — including the reconciliation read-back — cross
    the real ASGI boundary.
    """

    def _make_client(
        self, store: InMemoryStore, *, after_commit: bool
    ) -> tuple[StoreClient, Client]:
        app = make_app(
            store,
            subscribe_timeout=SHORT_SUBSCRIBE_TIMEOUT,
            subscribe_poll_interval=0.02,
        )
        seed_client = TestClient(app, base_url="http://wire.test")
        seed_store_client = StoreClient(
            "http://wire.test", EXPERIMENT_ID, client=seed_client
        )
        return seed_store_client, seed_client, app  # type: ignore[return-value]

    def test_confirmed_success_after_response_lost(
        self, store: InMemoryStore
    ) -> None:
        """Server commits; response is lost; client read-back observes the
        expected SHA → return success (no exception, no compensation)."""
        app = make_app(
            store,
            subscribe_timeout=SHORT_SUBSCRIBE_TIMEOUT,
            subscribe_poll_interval=0.02,
        )
        seed = TestClient(app, base_url="http://wire.test")
        seed_client = StoreClient("http://wire.test", EXPERIMENT_ID, client=seed)
        _run_variant_to_success(
            seed_client, task_prefix="cs", idea_id="cs-prop", variant_id="cs-t"
        )

        flaky = Client(
            transport=_transport_that_loses(
                app,
                lose_path_substring="/variants/cs-t/integrate",
                after_commit=True,
            ),
            base_url="http://wire.test",
        )
        with StoreClient("http://wire.test", EXPERIMENT_ID, client=flaky) as flaky_sc:
            flaky_sc.integrate_variant("cs-t", "c" * 40)  # MUST NOT raise
        assert store.read_variant("cs-t").variant_commit_sha == "c" * 40

    def test_indeterminate_when_no_sha(self, store: InMemoryStore) -> None:
        """Server never committed; read-back shows variant_commit_sha=None →
        IndeterminateIntegration (must NOT compensate)."""
        app = make_app(
            store,
            subscribe_timeout=SHORT_SUBSCRIBE_TIMEOUT,
            subscribe_poll_interval=0.02,
        )
        seed = TestClient(app, base_url="http://wire.test")
        seed_client = StoreClient("http://wire.test", EXPERIMENT_ID, client=seed)
        _run_variant_to_success(
            seed_client, task_prefix="ind", idea_id="ind-prop", variant_id="ind-t"
        )

        flaky = Client(
            transport=_transport_that_loses(
                app,
                lose_path_substring="/variants/ind-t/integrate",
                after_commit=False,
            ),
            base_url="http://wire.test",
        )
        with (
            StoreClient("http://wire.test", EXPERIMENT_ID, client=flaky) as flaky_sc,
            pytest.raises(IndeterminateIntegration),
        ):
            flaky_sc.integrate_variant("ind-t", "c" * 40)
        assert store.read_variant("ind-t").variant_commit_sha is None

    def test_confirmed_divergence(self, store: InMemoryStore) -> None:
        """Server committed a *different* SHA previously; transport fails on
        the new attempt; read-back surfaces the different SHA →
        InvalidPrecondition, no compensation."""
        app = make_app(
            store,
            subscribe_timeout=SHORT_SUBSCRIBE_TIMEOUT,
            subscribe_poll_interval=0.02,
        )
        seed = TestClient(app, base_url="http://wire.test")
        seed_client = StoreClient("http://wire.test", EXPERIMENT_ID, client=seed)
        _run_variant_to_success(
            seed_client, task_prefix="dv", idea_id="dv-prop", variant_id="dv-t"
        )
        seed_client.integrate_variant("dv-t", "d" * 40)  # pre-commit a different SHA

        flaky = Client(
            transport=_transport_that_loses(
                app,
                lose_path_substring="/variants/dv-t/integrate",
                after_commit=False,
            ),
            base_url="http://wire.test",
        )
        with (
            StoreClient("http://wire.test", EXPERIMENT_ID, client=flaky) as flaky_sc,
            pytest.raises(InvalidPrecondition),
        ):
            flaky_sc.integrate_variant("dv-t", "c" * 40)
        # The pre-existing SHA is untouched; the client did not compensate.
        assert store.read_variant("dv-t").variant_commit_sha == "d" * 40


def test_experiment_id_mismatch_error_type() -> None:
    """ExperimentIdMismatch is exposed at the wire binding entry."""
    assert ExperimentIdMismatch.__name__ == "ExperimentIdMismatch"
