"""Unit tests for `run_multi_experiment_loop`.

Drives the multi-experiment loop against fake stores + a fake control-
plane to verify:

- Each held experiment receives exactly one `run_orchestrator_iteration`
  call per outer-loop tick.
- An experiment whose lease drops between ticks is torn down (its
  Store.close is called) and no further iteration runs for it.
- §5.5 release-after-drain fires when an experiment transitions to
  `terminated` AND all success variants have a `variant_commit_sha`.
- §5.1 lease-ownership invariant: an experiment we do NOT hold a
  lease for produces zero `run_orchestrator_iteration` calls.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from eden_contracts import (
    BaselineConfig,
    DispatchMode,
    EvaluationSchema,
    Experiment,
    ExperimentConfig,
    ObjectiveSpec,
    Variant,
)
from eden_control_plane import ControlPlaneClient
from eden_orchestrator.lease_manager import LeaseManager
from eden_orchestrator.multi_loop import (
    ExperimentRuntime,
    run_multi_experiment_loop,
)
from eden_service_common import StopFlag

BASE_URL = "http://control-plane.test"
WORKER_ID = "auto-orchestrator-1"

# These tests exercise lease / iteration / drain logic, not baseline
# creation, so suppress the baseline (baseline.enabled: false) — keeps the
# mocked runtimes free of an ensure_baseline_variant side effect.
_CONFIG = ExperimentConfig(
    parallel_variants=1,
    evaluation_schema=EvaluationSchema({"score": "real"}),
    objective=ObjectiveSpec(expr="score", direction="maximize"),
    baseline=BaselineConfig(enabled=False),
)


def _lease_payload(
    *,
    experiment_id: str,
    lease_id: str = "lease-001",
    holder_instance: str = "uuid-aaaa",
) -> dict[str, Any]:
    # Use a wall-clock-future expires_at so the codex-round-2-introduced
    # expiry-aware `LeaseManager.is_held()` treats the seeded lease as
    # active. The fixed-timestamp shape from earlier waves would now
    # mark every seeded lease as expired, masking the iteration logic
    # under test.
    from datetime import UTC, datetime, timedelta

    acquired = datetime.now(UTC)
    expires = acquired + timedelta(seconds=300)
    return {
        "lease_id": lease_id,
        "experiment_id": experiment_id,
        "holder": WORKER_ID,
        "holder_instance": holder_instance,
        "acquired_at": acquired.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "renewed_at": acquired.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
    }


def _registry_payload(experiment_id: str) -> dict[str, Any]:
    return {
        "experiment_id": experiment_id,
        "config_uri": f"https://x.test/{experiment_id}.yaml",
        "created_at": "2026-05-19T12:00:00.000Z",
        "last_known_state": "running",
        "lease": None,
    }


def _control_plane_client_for(
    experiments_response: dict[str, Any],
    *,
    acquire_responses: dict[str, httpx.Response] | None = None,
    renew_responses: dict[str, httpx.Response] | None = None,
    release_responses: dict[str, httpx.Response] | None = None,
) -> ControlPlaneClient:
    acquire = acquire_responses or {}
    renew = renew_responses or {}
    release = release_responses or {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v0/control/experiments" and request.method == "GET":
            return httpx.Response(200, json=experiments_response)
        if request.method == "POST" and path.endswith("/leases"):
            experiment_id = path.split("/")[-2]
            return acquire.get(
                experiment_id,
                httpx.Response(201, json=_lease_payload(experiment_id=experiment_id)),
            )
        if request.method == "POST" and "/renew" in path:
            lease_id = path.split("/")[-2]
            return renew.get(
                lease_id,
                httpx.Response(
                    200, json=_lease_payload(experiment_id="any", lease_id=lease_id)
                ),
            )
        if request.method == "POST" and "/release" in path:
            lease_id = path.split("/")[-2]
            return release.get(lease_id, httpx.Response(200, json={}))
        return httpx.Response(404, json={"type": "eden://error/not-found"})

    transport = httpx.MockTransport(handler)
    inner = httpx.Client(transport=transport, base_url=BASE_URL)
    return ControlPlaneClient(BASE_URL, bearer="admin:T", client=inner)


# ---------------------------------------------------------------------
# Fake Store (just enough for run_orchestrator_iteration to be called)
# ---------------------------------------------------------------------


class FakeStore:
    """Minimal Store stub that records each call from the multi-loop.

    Records calls to the entry points the multi_loop driver makes
    (`read_dispatch_mode`, `read_experiment`, `list_variants`, plus
    everything `run_orchestrator_iteration` consults). The actual
    `run_orchestrator_iteration` is intercepted at module level so
    we only assert that it would have been called against this store.
    """

    def __init__(self, *, experiment_id: str = "exp-1") -> None:
        self.experiment_id = experiment_id
        self.read_count = 0
        self.list_variants_count = 0
        self.closed = False
        self._state = "running"
        self._variants: list[Variant] = []
        self._dispatch_mode = DispatchMode(
            termination="auto",
            ideation_creation="auto",
            execution_dispatch="auto",
            evaluation_dispatch="auto",
            integration="auto",
        )

    def read_dispatch_mode(self) -> DispatchMode:
        return self._dispatch_mode

    def read_experiment(self) -> Experiment:
        return Experiment.model_validate(
            {
                "experiment_id": self.experiment_id,
                "state": self._state,
                "created_at": "2026-05-19T12:00:00Z",
            }
        )

    def list_variants(self, *, status: str | None = None) -> list[Variant]:
        self.list_variants_count += 1
        if status is None:
            return list(self._variants)
        return [v for v in self._variants if v.status == status]

    def set_state(self, state: str) -> None:
        self._state = state

    def set_variants(self, variants: list[Variant]) -> None:
        self._variants = variants

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def patched_iteration(monkeypatch: pytest.MonkeyPatch) -> list[FakeStore]:
    """Patch `run_orchestrator_iteration` to record the store it sees."""
    calls: list[FakeStore] = []

    def fake_iteration(store: Any, **_kwargs: Any) -> bool:
        calls.append(store)
        return False

    monkeypatch.setattr(
        "eden_orchestrator.multi_loop.run_orchestrator_iteration",
        fake_iteration,
    )
    monkeypatch.setattr(
        "eden_orchestrator.multi_loop.sweep_expired_claims",
        lambda *args, **kwargs: 0,
    )
    return calls


def _make_runtime(experiment_id: str) -> ExperimentRuntime:
    store = FakeStore(experiment_id=experiment_id)

    class FakeIntegrator:
        def integrate(self, _variant_id: str) -> None:
            pass

    return ExperimentRuntime(
        experiment_id=experiment_id,
        store=store,  # type: ignore[arg-type]
        integrator=FakeIntegrator(),  # type: ignore[arg-type]
        ideation_factory=lambda: "ideation-id",
        execution_factory=lambda: "execution-id",
        evaluation_factory=lambda: "evaluation-id",
    )


def _stop_after_n_iterations(n: int) -> StopFlag:
    """Build a StopFlag that asserts itself after `n` `wait()` returns."""
    flag = StopFlag()
    original_wait = flag.wait
    counter = {"i": 0}

    def wait(timeout: float | None = None) -> bool:  # noqa: D401
        counter["i"] += 1
        if counter["i"] >= n:
            flag.set()
            return True
        return original_wait(0)

    flag.wait = wait
    return flag


# ---------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------


def test_loop_runs_iteration_for_each_held_experiment(
    patched_iteration: list[FakeStore],
) -> None:
    cp = _control_plane_client_for(
        experiments_response={
            "experiments": [_registry_payload("exp-1"), _registry_payload("exp-2")]
        }
    )
    manager = LeaseManager(
        cp, worker_id=WORKER_ID, holder_instance="uuid-aaaa"
    )
    runtimes = {"exp-1": _make_runtime("exp-1"), "exp-2": _make_runtime("exp-2")}
    stop = _stop_after_n_iterations(1)

    run_multi_experiment_loop(
        manager=manager,
        factory=lambda eid: runtimes[eid],
        terminated_by=WORKER_ID,
        ideation_policy=lambda *_a, **_kw: 0,
        termination_policy=lambda *_a, **_kw: None,  # type: ignore[arg-type]
        poll_interval=0.0,
        stop=stop,
        config=_CONFIG,
    )

    seen_experiments = {s.experiment_id for s in patched_iteration}
    assert seen_experiments == {"exp-1", "exp-2"}


def test_loop_tears_down_runtime_when_lease_drops(
    patched_iteration: list[FakeStore],
) -> None:
    """After a lease drop, the per-experiment Store.close() must fire."""
    # First tick: list_experiments returns exp-1 + acquire succeeds.
    # Second tick: list_experiments returns empty (experiment unregistered),
    # so the manager's renew on lease-001 returns 410 lease-not-held →
    # held set empties → runtime torn down.

    tick = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v0/control/experiments" and request.method == "GET":
            tick["n"] += 1
            if tick["n"] == 1:
                return httpx.Response(
                    200, json={"experiments": [_registry_payload("exp-1")]}
                )
            return httpx.Response(200, json={"experiments": []})
        if path.endswith("/leases") and request.method == "POST":
            return httpx.Response(
                201, json=_lease_payload(experiment_id="exp-1")
            )
        if "/renew" in path and request.method == "POST":
            return httpx.Response(
                410,
                headers={"content-type": "application/problem+json"},
                content=json.dumps(
                    {
                        "type": "eden://error/lease-not-held",
                        "title": "lease-not-held",
                        "status": 410,
                    }
                ).encode(),
            )
        return httpx.Response(404, json={"type": "eden://error/not-found"})

    transport = httpx.MockTransport(handler)
    inner = httpx.Client(transport=transport, base_url=BASE_URL)
    cp = ControlPlaneClient(BASE_URL, bearer="admin:T", client=inner)

    manager = LeaseManager(
        cp, worker_id=WORKER_ID, holder_instance="uuid-aaaa"
    )
    runtime = _make_runtime("exp-1")
    stop = _stop_after_n_iterations(2)

    run_multi_experiment_loop(
        manager=manager,
        factory=lambda eid: runtime,
        terminated_by=WORKER_ID,
        ideation_policy=lambda *_a, **_kw: 0,
        termination_policy=lambda *_a, **_kw: None,  # type: ignore[arg-type]
        poll_interval=0.0,
        stop=stop,
        config=_CONFIG,
    )

    # After two iterations: lease dropped on the second renew, the
    # runtime was torn down (Store.close called), and we observed
    # exactly one iteration call (the first tick's run).
    fake_store: FakeStore = runtime.store  # type: ignore[assignment]
    assert fake_store.closed is True
    assert len(patched_iteration) == 1


def test_release_after_drain_fires_on_terminated_experiment(
    patched_iteration: list[FakeStore],
) -> None:
    """An experiment in terminated state with no pending success-without-SHA
    triggers mark_drained_terminated → release + drained-terminated skip."""
    cp = _control_plane_client_for(
        experiments_response={"experiments": [_registry_payload("exp-1")]}
    )
    manager = LeaseManager(
        cp, worker_id=WORKER_ID, holder_instance="uuid-aaaa"
    )
    runtime = _make_runtime("exp-1")
    store: FakeStore = runtime.store  # type: ignore[assignment]
    store.set_state("terminated")
    # No variants → drain is trivially complete.
    store.set_variants([])

    stop = _stop_after_n_iterations(1)

    run_multi_experiment_loop(
        manager=manager,
        factory=lambda _eid: runtime,
        terminated_by=WORKER_ID,
        ideation_policy=lambda *_a, **_kw: 0,
        termination_policy=lambda *_a, **_kw: None,  # type: ignore[arg-type]
        poll_interval=0.0,
        stop=stop,
        config=_CONFIG,
    )

    assert "exp-1" in manager.drained_terminated()
    assert manager.held_experiments() == []


def test_release_after_drain_holds_until_pending_success_has_sha(
    patched_iteration: list[FakeStore],
) -> None:
    """Terminated + success variant WITHOUT variant_commit_sha → keep lease."""
    cp = _control_plane_client_for(
        experiments_response={"experiments": [_registry_payload("exp-1")]}
    )
    manager = LeaseManager(
        cp, worker_id=WORKER_ID, holder_instance="uuid-aaaa"
    )
    runtime = _make_runtime("exp-1")
    store: FakeStore = runtime.store  # type: ignore[assignment]
    store.set_state("terminated")
    pending_success = Variant.model_validate(
        {
            "variant_id": "v-1",
            "idea_id": "i-1",
            "experiment_id": "exp-1",
            "parent_commits": ["a" * 40],
            "commit_sha": "b" * 40,
            "branch": "work/v-1",
            "status": "success",
            "started_at": "2026-05-19T12:00:00Z",
        }
    )
    store.set_variants([pending_success])

    stop = _stop_after_n_iterations(1)

    run_multi_experiment_loop(
        manager=manager,
        factory=lambda _eid: runtime,
        terminated_by=WORKER_ID,
        ideation_policy=lambda *_a, **_kw: 0,
        termination_policy=lambda *_a, **_kw: None,  # type: ignore[arg-type]
        poll_interval=0.0,
        stop=stop,
        config=_CONFIG,
    )

    # Lease is still held because the drain has not completed.
    assert "exp-1" not in manager.drained_terminated()
    assert "exp-1" in manager.held_experiments()


def test_loop_skips_experiment_we_dont_hold_lease_for(
    patched_iteration: list[FakeStore],
) -> None:
    """§5.1 lease-ownership: only held experiments get iterated.

    Pre-seed the LeaseManager with NO held leases but supply a factory
    that would build a runtime if called. After one iteration the
    factory MUST NOT have been called for any experiment whose acquire
    fails with `lease-held-by-other`.
    """
    cp = _control_plane_client_for(
        experiments_response={"experiments": [_registry_payload("exp-1")]},
        acquire_responses={
            "exp-1": httpx.Response(
                409,
                headers={"content-type": "application/problem+json"},
                content=json.dumps(
                    {
                        "type": "eden://error/lease-held-by-other",
                        "title": "x",
                        "status": 409,
                    }
                ).encode(),
            )
        },
    )
    manager = LeaseManager(
        cp, worker_id=WORKER_ID, holder_instance="uuid-mine"
    )
    factory_calls: list[str] = []

    def factory(experiment_id: str) -> ExperimentRuntime:
        factory_calls.append(experiment_id)
        return _make_runtime(experiment_id)

    stop = _stop_after_n_iterations(1)

    run_multi_experiment_loop(
        manager=manager,
        factory=factory,
        terminated_by=WORKER_ID,
        ideation_policy=lambda *_a, **_kw: 0,
        termination_policy=lambda *_a, **_kw: None,  # type: ignore[arg-type]
        poll_interval=0.0,
        stop=stop,
        config=_CONFIG,
    )

    assert factory_calls == []
    assert patched_iteration == []
    assert manager.held_experiments() == []


def test_factory_failure_releases_lease(
    patched_iteration: list[FakeStore],
) -> None:
    """Codex round 4 MAJOR 1: factory failure → release lease, not blackhole.

    When the per-experiment runtime factory raises (typically: the
    per-experiment task-store credential bootstrap failed), the
    orchestrator MUST release the lease so another replica can
    attempt — NOT silently hold the lease through the renew cadence
    while being unable to do any task-store work.
    """
    release_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v0/control/experiments" and request.method == "GET":
            return httpx.Response(
                200,
                json={"experiments": [_registry_payload("exp-1")]},
            )
        if request.method == "POST" and path.endswith("/leases"):
            return httpx.Response(
                201, json=_lease_payload(experiment_id="exp-1")
            )
        if request.method == "POST" and "/release" in path:
            release_calls.append(path)
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"type": "eden://error/not-found"})

    transport = httpx.MockTransport(handler)
    inner = httpx.Client(transport=transport, base_url=BASE_URL)
    cp = ControlPlaneClient(BASE_URL, bearer="admin:T", client=inner)
    manager = LeaseManager(
        cp, worker_id=WORKER_ID, holder_instance="uuid-aaaa"
    )

    def factory(_experiment_id: str) -> ExperimentRuntime:
        raise RuntimeError("per-experiment bootstrap failed")

    stop = _stop_after_n_iterations(1)

    run_multi_experiment_loop(
        manager=manager,
        factory=factory,
        terminated_by=WORKER_ID,
        ideation_policy=lambda *_a, **_kw: 0,
        termination_policy=lambda *_a, **_kw: None,  # type: ignore[arg-type]
        poll_interval=0.0,
        stop=stop,
        config=_CONFIG,
    )

    # The lease was released (NOT held to expiry) AND not added to
    # the drained-terminated skip set.
    assert manager.held_experiments() == []
    assert "exp-1" not in manager.drained_terminated()
    assert len(release_calls) == 1
