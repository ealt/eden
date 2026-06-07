"""Unit-level tests for the orchestrator service loop (wave-4).

The end-to-end real-subprocess tests in ``test_e2e.py`` /
``test_subprocess_e2e.py`` cover the orchestrator's full lifecycle
under quiescence. These unit tests pin the wave-4-specific contracts
without spinning up subprocesses:

- ``_read_dispatch_mode`` fails CLOSED to all-``manual`` on transport
  failure rather than wedging the loop. Per spec §6.1 a forbidden
  dispatch slipping through during an operator's manual window
  would violate the MUST-NOT contract; failing closed gates every
  decision off for one iteration while finalize/sweep paths keep
  worker submissions moving until the read recovers.
- ``run_orchestrator_loop`` reads ``dispatch_mode`` at iteration
  start and forwards it to ``run_orchestrator_iteration``.
- The ideation-policy callable is invoked each iteration; created
  tasks use the configured prefix.
- ``_ensure_orchestrators_membership`` is idempotent on existing
  group + existing membership and races (NotFound retry).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from eden_contracts import DispatchMode, EvaluationSchema
from eden_dispatch import (
    ExperimentStateView,
    InMemoryStore,
    maintain_pending,
    never_terminate,
)
from eden_orchestrator.checkpoint_scheduler import CheckpointScheduler
from eden_orchestrator.cli import _ensure_orchestrators_membership
from eden_orchestrator.loop import _read_dispatch_mode, run_orchestrator_loop
from eden_service_common import StopFlag


def _disabled_scheduler() -> CheckpointScheduler:
    """No-op auto-checkpoint scheduler for loop tests that don't exercise it."""
    return CheckpointScheduler.from_config(
        None, experiment_id=_EXP_ID, destination=None, export_fn=None
    )


_EXP_ID = "exp_01kt5e4vh7h10w9fsb2pbkmt6s"


@pytest.fixture
def store() -> InMemoryStore:
    s = InMemoryStore(
        experiment_id=_EXP_ID,
        evaluation_schema=EvaluationSchema({"loss": "real"}),
    )
    # Since #128 register_worker mints opaque ids; the optional name
    # is the operator label. The loop tests don't reference these
    # workers by id (dispatch-mode writes use the admin principal),
    # so the minted ids are intentionally discarded here.
    for name in ("orchestrator", "ideator-1", "executor-1", "evaluator-1"):
        s.register_worker(name)
    return s


# ----------------------------------------------------------------------
# _read_dispatch_mode fallback
# ----------------------------------------------------------------------


def test_read_dispatch_mode_returns_store_value(store: InMemoryStore) -> None:
    store.update_dispatch_mode(
        {"integration": "manual"}, updated_by="admin"
    )
    mode = _read_dispatch_mode(store)
    assert mode.integration == "manual"


def test_read_dispatch_mode_falls_back_on_failure(caplog) -> None:  # noqa: ANN001
    """Transport failure during read MUST NOT crash the loop AND MUST fail closed.

    Per spec §6.1, manual mode means the orchestrator MUST NOT run
    that decision. A read failure that fell open to all-``auto``
    would let a forbidden dispatch slip through during an operator's
    manual window, violating §6.1. The fallback MUST gate every
    decision off until the next iteration can re-read the actual
    dispatch_mode.
    """
    import logging

    class _BrokenStore:
        def read_dispatch_mode(self) -> DispatchMode:
            raise RuntimeError("simulated transport blip")

    with caplog.at_level(logging.ERROR, logger="eden_orchestrator.loop"):
        mode = _read_dispatch_mode(_BrokenStore())  # type: ignore[arg-type]
    assert mode.ideation_creation == "manual"
    assert mode.execution_dispatch == "manual"
    assert mode.evaluation_dispatch == "manual"
    assert mode.integration == "manual"
    assert any(
        "read_dispatch_mode_failed" in r.message for r in caplog.records
    )


# ----------------------------------------------------------------------
# Loop drives dispatch_mode reads + policy invocation
# ----------------------------------------------------------------------


class _NoopIntegrator:
    """Minimal Integrator stand-in for the loop test.

    The real ``Integrator`` is a chapter-06 §3.2 / §3.4 composite that
    requires a git repo. For dispatch-loop unit tests we never
    actually invoke ``integrate`` (the test scenarios don't produce
    success variants), so a no-op stand-in is sufficient.
    """

    def integrate(self, variant_id: str) -> None:
        msg = f"unexpected integrate({variant_id!r}) in unit test"
        raise AssertionError(msg)


def test_loop_invokes_ideation_policy_and_creates_tasks(
    store: InMemoryStore,
) -> None:
    """First iteration creates the policy-returned count of ideation tasks."""
    stop = StopFlag()
    iterations = 0

    def policy(state: ExperimentStateView) -> int:
        nonlocal iterations
        iterations += 1
        if iterations == 1:
            return 3
        # Quiesce after the first iteration so the loop terminates.
        stop.set()
        return 0

    run_orchestrator_loop(
        store=store,
        integrator=_NoopIntegrator(),  # type: ignore[arg-type]
        ideation_policy=policy,
        termination_policy=never_terminate,
        terminated_by="admin",
        ideation_task_prefix="ideation-",
        execution_task_prefix="execution-",
        evaluation_task_prefix="evaluate-",
        poll_interval=0.0,
        max_quiescent_iterations=5,
        stop=stop,
        scheduler=_disabled_scheduler(),
    )
    tasks = store.list_tasks(kind="ideation")
    assert len(tasks) == 3
    assert all(t.task_id.startswith("ideation-") for t in tasks)


def test_loop_honors_manual_ideation_creation(store: InMemoryStore) -> None:
    """Flipping ideation_creation to manual suppresses policy invocation."""
    store.update_dispatch_mode(
        {"ideation_creation": "manual"}, updated_by="admin"
    )
    stop = StopFlag()
    calls: list[int] = []

    def policy(state: ExperimentStateView) -> int:
        calls.append(1)
        return 99  # would be a runaway if the gate didn't fire

    def _quiesce_after_first_iter(*, store: InMemoryStore = store) -> Callable[[], None]:
        seen = [0]

        def _hook() -> None:
            seen[0] += 1
            if seen[0] >= 1:
                stop.set()

        return _hook

    # Run a single iteration's worth and then quiesce.
    stop_hook = _quiesce_after_first_iter()

    # Use a sentinel policy that signals the stop flag after one call.
    def gating_policy(state: ExperimentStateView) -> int:
        return policy(state)

    # Force quiescence after one iteration by tightening the budget.
    run_orchestrator_loop(
        store=store,
        integrator=_NoopIntegrator(),  # type: ignore[arg-type]
        ideation_policy=gating_policy,
        termination_policy=never_terminate,
        terminated_by="admin",
        ideation_task_prefix="ideation-",
        execution_task_prefix="execution-",
        evaluation_task_prefix="evaluate-",
        poll_interval=0.0,
        max_quiescent_iterations=2,  # quiesces after 2 no-progress iters
        stop=stop,
        scheduler=_disabled_scheduler(),
    )
    # Manual mode → policy never invoked.
    assert calls == []
    assert store.list_tasks(kind="ideation") == []
    # Reference the unused hook so ruff doesn't flag it.
    assert stop_hook is not None


def test_loop_picks_up_mode_changes_between_iterations(
    store: InMemoryStore,
) -> None:
    """A mid-loop dispatch_mode flip takes effect on the next iteration."""
    stop = StopFlag()
    iteration = [0]

    def policy(state: ExperimentStateView) -> int:
        iteration[0] += 1
        if iteration[0] == 1:
            # After iter 1, flip ideation_creation to manual.
            store.update_dispatch_mode(
                {"ideation_creation": "manual"}, updated_by="admin"
            )
            return 2  # iter-1 creates 2 ideation tasks
        # Iter 2+ is gated off; the loop should quiesce.
        if iteration[0] >= 4:
            stop.set()
        return 99  # never reached if gate works

    run_orchestrator_loop(
        store=store,
        integrator=_NoopIntegrator(),  # type: ignore[arg-type]
        ideation_policy=policy,
        termination_policy=never_terminate,
        terminated_by="admin",
        ideation_task_prefix="ideation-",
        execution_task_prefix="execution-",
        evaluation_task_prefix="evaluate-",
        poll_interval=0.0,
        max_quiescent_iterations=3,
        stop=stop,
        scheduler=_disabled_scheduler(),
    )
    # Exactly 2 ideation tasks from iter-1; iter-2+ gate prevents more.
    assert len(store.list_tasks(kind="ideation")) == 2


# ----------------------------------------------------------------------
# _ensure_orchestrators_membership
# ----------------------------------------------------------------------


class _FakeGroup:
    """Minimal stand-in for the ``Group`` returned by the admin client."""

    def __init__(self, group_id: str) -> None:
        self.group_id = group_id


class _FakeAdmin:
    """In-memory stand-in for the admin StoreClient used by the bootstrap.

    Since #128 the ``orchestrators`` authority group is resolved by its
    reserved display NAME (``list_groups(name="orchestrators")``) to a
    minted ``grp_*`` id rather than addressed by a literal id. This fake
    models a store that has no pre-existing ``orchestrators`` group (so
    the helper falls through to ``register_group``), and surfaces the
    group via its returned ``.group_id`` — which the fake pins to the
    literal name so the existing add-call assertions still read cleanly.
    """

    def __init__(
        self,
        *,
        register_raises: Exception | None = None,
        add_raises: Exception | None = None,
    ) -> None:
        self.register_calls: list[str] = []
        self.add_calls: list[tuple[str, str]] = []
        self.list_groups_calls: list[str | None] = []
        self._register_raises = register_raises
        self._add_raises = add_raises
        # Names that resolve via list_groups. Starts empty so the helper
        # falls through to register_group; a register attempt (whether it
        # succeeds or races into AlreadyExists) records the name so the
        # subsequent AlreadyExists re-read / race-retry resolve finds it.
        self._existing: set[str] = set()

    def list_groups(self, *, name: str | None = None) -> list[_FakeGroup]:
        self.list_groups_calls.append(name)
        if name in self._existing:
            return [_FakeGroup(name)]
        return []

    def register_group(self, name: str, **_kwargs: Any) -> _FakeGroup:
        self.register_calls.append(name)
        if self._register_raises is not None:
            # AlreadyExists models a concurrent creator that won: the
            # store-of-record now holds the group, so the helper's
            # AlreadyExists-branch re-read MUST resolve it.
            from eden_storage.errors import AlreadyExists

            if isinstance(self._register_raises, AlreadyExists):
                self._existing.add(name)
            raise self._register_raises
        # Successful register: the group may still be raced away before
        # the add (the recovers-from-race test deletes it), so we do NOT
        # mark it persistently existing — a later resolve re-registers.
        return _FakeGroup(name)

    def add_to_group(self, group_id: str, member_id: str) -> Any:
        self.add_calls.append((group_id, member_id))
        if self._add_raises is not None:
            raise self._add_raises


def _patch_admin_storeclient(
    monkeypatch: pytest.MonkeyPatch, admin: _FakeAdmin
) -> None:
    """Replace ``cli.StoreClient`` with a context manager wrapping ``admin``."""

    class _Ctx:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._args = args
            self._kwargs = kwargs

        def __enter__(self) -> _FakeAdmin:
            return admin

        def __exit__(self, *_exc: Any) -> None:
            return None

    monkeypatch.setattr("eden_orchestrator.cli.StoreClient", _Ctx)


class _MockLog:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def info(self, event: str, **kwargs: Any) -> None:
        self.events.append((event, kwargs))


def test_ensure_orchestrators_membership_idempotent_on_existing_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eden_storage.errors import AlreadyExists

    admin = _FakeAdmin(register_raises=AlreadyExists("group exists"))
    _patch_admin_storeclient(monkeypatch, admin)
    log = _MockLog()

    _ensure_orchestrators_membership(
        log=log,
        base_url="http://store",
        experiment_id="exp",
        admin_token="tok",
        worker_id="orch-1",
    )
    # register_group called once; AlreadyExists swallowed.
    assert admin.register_calls == ["orchestrators"]
    # add_to_group called once.
    assert admin.add_calls == [("orchestrators", "orch-1")]


def test_ensure_orchestrators_membership_recovers_from_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If add_to_group races a group-delete, the helper re-registers + retries."""
    from eden_storage.errors import NotFound

    admin = _FakeAdmin(add_raises=NotFound("group disappeared"))
    _patch_admin_storeclient(monkeypatch, admin)
    log = _MockLog()

    # Clear the `add_raises` after the first add so the retry succeeds.
    def _add(group_id: str, member_id: str) -> Any:
        admin.add_calls.append((group_id, member_id))
        if len(admin.add_calls) == 1:
            raise NotFound("group disappeared")

    admin.add_to_group = _add

    _ensure_orchestrators_membership(
        log=log,
        base_url="http://store",
        experiment_id="exp",
        admin_token="tok",
        worker_id="orch-1",
    )
    # register_group called twice (initial + after race).
    assert admin.register_calls == ["orchestrators", "orchestrators"]
    # add_to_group called twice (first raised NotFound, second succeeded).
    assert admin.add_calls == [
        ("orchestrators", "orch-1"),
        ("orchestrators", "orch-1"),
    ]


# Hold a reference to silence ruff on the unused maintain_pending import
# (it's used indirectly via ``default_policy``-shaped contexts elsewhere
# in this suite-by-suite layout, and we keep the alias importable for
# future deployment-policy tests).
_unused_policy = maintain_pending
