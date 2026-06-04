"""Loop + CLI wiring tests for auto-checkpointing (issue #131, Wave 3).

The pure scheduler logic is covered in
``test_checkpoint_scheduler.py``; these tests pin the *wiring*:

- the loop fires the periodic checkpoint each iteration;
- a quiescent-but-running exit emits NO ``-terminated-`` archive;
- an observed ``terminated`` state DOES emit the terminal archive;
- a raising ``read_experiment_state`` does not crash the loop (plan D5);
- ``_validate_auto_checkpoint`` fails fast at startup when enabled
  without an admin token or without a usable destination dir;
- ``_build_auto_checkpoint_scheduler`` builds an admin-bearer export
  client (a worker-bearer export would 403, plan D4) and a no-op
  scheduler when disabled.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest
from eden_contracts import (
    AutoCheckpointConfig,
    EvaluationSchema,
    ExperimentConfig,
    ObjectiveSpec,
)
from eden_dispatch import ExperimentStateView, InMemoryStore, never_terminate
from eden_orchestrator.auto_checkpoint import (
    build_auto_checkpoint_scheduler,
    validate_auto_checkpoint,
)
from eden_orchestrator.checkpoint_scheduler import (
    CheckpointScheduler,
    _safe_experiment_prefix,
)
from eden_orchestrator.loop import run_orchestrator_loop
from eden_service_common import StopFlag

_EXP_ID = "exp_01kt5e4vh7h10w9fsb2pbkmt6s"


class _NoopIntegrator:
    def integrate(self, variant_id: str) -> None:
        msg = f"unexpected integrate({variant_id!r})"
        raise AssertionError(msg)


@pytest.fixture
def store() -> InMemoryStore:
    s = InMemoryStore(
        experiment_id=_EXP_ID,
        evaluation_schema=EvaluationSchema({"loss": "real"}),
    )
    for name in ("orchestrator", "ideator-1"):
        s.register_worker(name)
    return s


def _enabled_scheduler(
    destination: Path, *, interval_seconds: float = 0.0, on_terminate: bool = True
) -> CheckpointScheduler:
    def _export(stream) -> None:  # noqa: ANN001
        stream.write(b"CKPT")

    return CheckpointScheduler(
        experiment_id=_EXP_ID,
        destination=destination,
        export_fn=_export,
        enabled=True,
        interval_seconds=interval_seconds,
        retention_count=6,
        on_terminate=on_terminate,
    )


def _quiescing_policy(stop: StopFlag) -> object:
    # Quiesce immediately so the loop runs a handful of no-progress
    # iterations and exits via the quiescence budget.
    def _policy(state: ExperimentStateView) -> int:
        return 0

    return _policy


def _run(
    store: InMemoryStore,
    scheduler: CheckpointScheduler,
    *,
    max_quiescent_iterations: int = 2,
) -> None:
    stop = StopFlag()
    run_orchestrator_loop(
        store=store,
        integrator=_NoopIntegrator(),  # type: ignore[arg-type]
        ideation_policy=_quiescing_policy(stop),  # type: ignore[arg-type]
        termination_policy=never_terminate,
        terminated_by="orchestrator",
        ideation_task_prefix="ideation-",
        execution_task_prefix="execution-",
        evaluation_task_prefix="evaluate-",
        poll_interval=0.0,
        max_quiescent_iterations=max_quiescent_iterations,
        stop=stop,
        scheduler=scheduler,
    )


def _periodic(tmp_path: Path) -> list[Path]:
    prefix = _safe_experiment_prefix(_EXP_ID)
    return [
        p
        for p in tmp_path.iterdir()
        if p.name.startswith(f"{prefix}-")
        and "-terminated-" not in p.name
        and p.suffix == ".tar"
    ]


def _terminal(tmp_path: Path) -> list[Path]:
    prefix = _safe_experiment_prefix(_EXP_ID)
    return [p for p in tmp_path.iterdir() if p.name.startswith(f"{prefix}-terminated-")]


# -- loop hooks -------------------------------------------------------


def test_loop_fires_periodic(store: InMemoryStore, tmp_path: Path) -> None:
    _run(store, _enabled_scheduler(tmp_path, interval_seconds=0.0))
    assert len(_periodic(tmp_path)) >= 1


def test_loop_no_terminal_on_running_exit(
    store: InMemoryStore, tmp_path: Path
) -> None:
    # Experiment stays "running"; a healthy quiescent exit must NOT drop
    # a -terminated- archive (plan D6).
    _run(store, _enabled_scheduler(tmp_path))
    assert _terminal(tmp_path) == []


def test_loop_terminal_after_observed_termination(
    store: InMemoryStore, tmp_path: Path
) -> None:
    store.terminate_experiment(reason="done", terminated_by="admin")
    _run(store, _enabled_scheduler(tmp_path))
    assert len(_terminal(tmp_path)) == 1


def test_loop_survives_raising_state_read(
    store: InMemoryStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom() -> str:
        raise RuntimeError("simulated state-read blip")

    monkeypatch.setattr(store, "read_experiment_state", _boom)
    # Must not raise; terminal check is skipped this run (plan D5).
    _run(store, _enabled_scheduler(tmp_path))
    assert _terminal(tmp_path) == []


def test_disabled_scheduler_writes_nothing(
    store: InMemoryStore, tmp_path: Path
) -> None:
    # A disabled scheduler (the default) emits no archives even when the
    # experiment terminates. (The loop's terminal-state read is gated on
    # ``should_check_terminal``, which is false here; the iteration's own
    # state read is unrelated.)
    store.terminate_experiment(reason="done", terminated_by="admin")
    disabled = CheckpointScheduler.from_config(
        None, experiment_id=_EXP_ID, destination=None, export_fn=None
    )
    _run(store, disabled)
    assert list(tmp_path.iterdir()) == []


# -- CLI fail-fast ----------------------------------------------------


def _config(*, enabled: bool) -> ExperimentConfig:
    return ExperimentConfig(
        parallel_variants=1,
        evaluation_schema=EvaluationSchema({"loss": "real"}),
        objective=ObjectiveSpec(expr="loss", direction="minimize"),
        auto_checkpoint=AutoCheckpointConfig(enabled=enabled, interval_seconds=1.0),
    )


def _args(tmp_path: Path | None) -> argparse.Namespace:
    return argparse.Namespace(
        experiment_id=_EXP_ID,
        task_store_url="http://127.0.0.1:8080",
        auto_checkpoint_dir=str(tmp_path) if tmp_path is not None else None,
    )


def test_validate_fails_without_admin_token(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="no admin token"):
        validate_auto_checkpoint(
            args=_args(tmp_path), config=_config(enabled=True), admin_token=None
        )


def test_validate_fails_without_dest_dir() -> None:
    with pytest.raises(SystemExit, match="no destination directory"):
        validate_auto_checkpoint(
            args=_args(None), config=_config(enabled=True), admin_token="t"
        )


def test_validate_fails_when_dest_is_a_file(tmp_path: Path) -> None:
    f = tmp_path / "not-a-dir"
    f.write_text("x")
    args = argparse.Namespace(
        experiment_id=_EXP_ID,
        task_store_url="http://127.0.0.1:8080",
        auto_checkpoint_dir=str(f),
    )
    # mkdir(exist_ok=True) against an existing *file* raises
    # FileExistsError → surfaced as a clear startup error.
    with pytest.raises(SystemExit, match="auto-checkpoint-dir"):
        validate_auto_checkpoint(
            args=args, config=_config(enabled=True), admin_token="t"
        )


def test_validate_disabled_returns_none(tmp_path: Path) -> None:
    assert (
        validate_auto_checkpoint(
            args=_args(tmp_path), config=_config(enabled=False), admin_token=None
        )
        is None
    )


def test_validate_creates_dir(tmp_path: Path) -> None:
    dest = tmp_path / "checkpoints"
    args = argparse.Namespace(
        experiment_id=_EXP_ID,
        task_store_url="http://127.0.0.1:8080",
        auto_checkpoint_dir=str(dest),
    )
    resolved = validate_auto_checkpoint(
        args=args, config=_config(enabled=True), admin_token="t"
    )
    assert resolved == dest
    assert dest.is_dir()


def test_build_scheduler_disabled_has_no_client(tmp_path: Path) -> None:
    import logging

    scheduler, client = build_auto_checkpoint_scheduler(
        args=_args(tmp_path),
        config=_config(enabled=False),
        admin_token=None,
        destination=None,
        log=logging.getLogger("test"),
    )
    assert client is None
    assert scheduler.enabled is False


def test_build_scheduler_enabled_uses_admin_bearer(tmp_path: Path) -> None:
    import logging

    scheduler, client = build_auto_checkpoint_scheduler(
        args=_args(tmp_path),
        config=_config(enabled=True),
        admin_token="sekret",
        destination=tmp_path,
        log=logging.getLogger("test"),
    )
    try:
        assert scheduler.enabled is True
        assert client is not None
        # The export client authenticates as the deployment admin (plan
        # D4) — a worker bearer would 403 on the admin-gated export.
        assert client._bearer == "admin:sekret"  # noqa: SLF001
    finally:
        if client is not None:
            client.close()
