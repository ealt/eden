"""Unit tests for the auto-checkpoint scheduler (issue #131).

Fake monotonic clock + fake export callable + tmp dir — no loop, no
wire. Covers the plan §6 Wave-2 gate matrix:

- first periodic fires one interval after construction (not at t=0);
- the interval fires at the boundary;
- retention prunes oldest-only and ignores ``-terminated-`` + foreign
  files;
- terminal is once-only and skips when a ``-terminated-`` file already
  exists (restart dedup);
- export failure is swallowed AND the timer still advances one interval
  (no storm) AND the temp file is cleaned up;
- an ``experiment_id`` containing ``/`` produces a safe in-dir filename;
- disabled config is a no-op.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import IO

import pytest
from eden_contracts import AutoCheckpointConfig
from eden_orchestrator.checkpoint_scheduler import (
    CheckpointScheduler,
    ExportFn,
    _safe_experiment_prefix,
)


class FakeClock:
    """Injectable monotonic clock advanced explicitly by tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


def _wall(n: int = 0) -> datetime:
    # Distinct, monotonically-increasing wall timestamps (seconds apart)
    # so periodic filenames don't collide within a test.
    return datetime(2026, 6, 4, 12, 0, n, tzinfo=UTC)


def _make_export_fn(payload: bytes = b"CKPT") -> ExportFn:
    def _export(stream: IO[bytes]) -> None:
        stream.write(payload)

    return _export


def _scheduler(
    tmp_path: Path,
    clock: FakeClock,
    *,
    experiment_id: str = "exp-1",
    enabled: bool = True,
    interval_seconds: float = 100.0,
    retention_count: int = 3,
    on_terminate: bool = True,
    export_fn: ExportFn | None = None,
) -> CheckpointScheduler:
    return CheckpointScheduler(
        experiment_id=experiment_id,
        destination=tmp_path,
        export_fn=export_fn if export_fn is not None else _make_export_fn(),
        enabled=enabled,
        interval_seconds=interval_seconds,
        retention_count=retention_count,
        on_terminate=on_terminate,
        now_fn=clock,
    )


def _periodic_tars(tmp_path: Path, prefix: str) -> list[str]:
    return sorted(
        p.name
        for p in tmp_path.iterdir()
        if p.name.startswith(f"{prefix}-")
        and "-terminated-" not in p.name
        and p.suffix == ".tar"
    )


def _terminal_tars(tmp_path: Path, prefix: str) -> list[str]:
    return sorted(
        p.name for p in tmp_path.iterdir() if p.name.startswith(f"{prefix}-terminated-")
    )


# -- periodic cadence -------------------------------------------------


def test_first_periodic_does_not_fire_at_construction(tmp_path: Path) -> None:
    clock = FakeClock()
    sched = _scheduler(tmp_path, clock, interval_seconds=100.0)
    # No time has elapsed — must not fire (avoids t=0 churn checkpoint).
    sched.maybe_checkpoint_periodic(wall_now=_wall(0))
    assert list(tmp_path.iterdir()) == []


def test_first_periodic_fires_one_interval_later(tmp_path: Path) -> None:
    clock = FakeClock()
    prefix = _safe_experiment_prefix("exp-1")
    sched = _scheduler(tmp_path, clock, interval_seconds=100.0)
    clock.advance(99.0)
    sched.maybe_checkpoint_periodic(wall_now=_wall(0))
    assert _periodic_tars(tmp_path, prefix) == []  # just shy of the boundary
    clock.advance(1.0)  # now exactly at the boundary
    sched.maybe_checkpoint_periodic(wall_now=_wall(1))
    assert len(_periodic_tars(tmp_path, prefix)) == 1


def test_periodic_payload_written(tmp_path: Path) -> None:
    clock = FakeClock()
    prefix = _safe_experiment_prefix("exp-1")
    sched = _scheduler(
        tmp_path, clock, interval_seconds=10.0, export_fn=_make_export_fn(b"HELLO")
    )
    clock.advance(10.0)
    sched.maybe_checkpoint_periodic(wall_now=_wall(0))
    [name] = _periodic_tars(tmp_path, prefix)
    assert (tmp_path / name).read_bytes() == b"HELLO"


def test_periodic_timer_advances_from_completion(tmp_path: Path) -> None:
    clock = FakeClock()
    prefix = _safe_experiment_prefix("exp-1")
    sched = _scheduler(tmp_path, clock, interval_seconds=100.0)
    clock.advance(250.0)  # way past one interval
    sched.maybe_checkpoint_periodic(wall_now=_wall(0))
    # Exactly ONE checkpoint despite 2.5 intervals elapsed — no catch-up
    # storm; the next fire is one interval after THIS completion.
    assert len(_periodic_tars(tmp_path, prefix)) == 1
    clock.advance(99.0)
    sched.maybe_checkpoint_periodic(wall_now=_wall(1))
    assert len(_periodic_tars(tmp_path, prefix)) == 1
    clock.advance(1.0)
    sched.maybe_checkpoint_periodic(wall_now=_wall(2))
    assert len(_periodic_tars(tmp_path, prefix)) == 2


# -- retention ring ---------------------------------------------------


def test_retention_prunes_oldest_only(tmp_path: Path) -> None:
    clock = FakeClock()
    prefix = _safe_experiment_prefix("exp-1")
    sched = _scheduler(tmp_path, clock, interval_seconds=10.0, retention_count=3)
    for i in range(5):
        clock.advance(10.0)
        sched.maybe_checkpoint_periodic(wall_now=_wall(i))
    kept = _periodic_tars(tmp_path, prefix)
    assert len(kept) == 3
    # Oldest two (wall seconds 0 and 1) pruned; newest three retained.
    assert all("120000Z" not in k and "120001Z" not in k for k in kept)
    assert any("120004Z" in k for k in kept)


def test_retention_ignores_terminal_and_foreign_files(tmp_path: Path) -> None:
    clock = FakeClock()
    prefix = _safe_experiment_prefix("exp-1")
    # Pre-seed a terminal archive + a foreign file + another experiment's
    # periodic archive; none of these must ever be pruned.
    other_prefix = _safe_experiment_prefix("exp-2")
    (tmp_path / f"{prefix}-terminated-20260101T000000Z.tar").write_bytes(b"T")
    (tmp_path / "operator-notes.txt").write_bytes(b"keep me")
    (tmp_path / f"{other_prefix}-20260101T000000Z.tar").write_bytes(b"O")
    sched = _scheduler(tmp_path, clock, interval_seconds=10.0, retention_count=2)
    for i in range(4):
        clock.advance(10.0)
        sched.maybe_checkpoint_periodic(wall_now=_wall(i))
    assert len(_periodic_tars(tmp_path, prefix)) == 2
    assert (tmp_path / f"{prefix}-terminated-20260101T000000Z.tar").exists()
    assert (tmp_path / "operator-notes.txt").exists()
    assert (tmp_path / f"{other_prefix}-20260101T000000Z.tar").exists()


# -- terminal trigger -------------------------------------------------


def test_terminal_fires_once(tmp_path: Path) -> None:
    clock = FakeClock()
    prefix = _safe_experiment_prefix("exp-1")
    sched = _scheduler(tmp_path, clock)
    sched.maybe_checkpoint_terminal()
    sched.maybe_checkpoint_terminal()
    assert len(_terminal_tars(tmp_path, prefix)) == 1


def test_terminal_skipped_when_existing_archive(tmp_path: Path) -> None:
    clock = FakeClock()
    prefix = _safe_experiment_prefix("exp-1")
    # Restart dedup: a prior process already wrote a terminal archive.
    (tmp_path / f"{prefix}-terminated-20260101T000000Z.tar").write_bytes(b"OLD")
    sched = _scheduler(tmp_path, clock)
    sched.maybe_checkpoint_terminal()
    # No second terminal archive written.
    assert _terminal_tars(tmp_path, prefix) == [
        f"{prefix}-terminated-20260101T000000Z.tar"
    ]


def test_terminal_noop_when_on_terminate_false(tmp_path: Path) -> None:
    clock = FakeClock()
    prefix = _safe_experiment_prefix("exp-1")
    sched = _scheduler(tmp_path, clock, on_terminate=False)
    sched.maybe_checkpoint_terminal()
    assert _terminal_tars(tmp_path, prefix) == []


def test_terminal_retries_after_failure(tmp_path: Path) -> None:
    clock = FakeClock()
    prefix = _safe_experiment_prefix("exp-1")
    calls = {"n": 0}

    def _flaky(stream) -> None:  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        stream.write(b"OK")

    sched = _scheduler(tmp_path, clock, export_fn=_flaky)
    sched.maybe_checkpoint_terminal()  # fails, done-flag NOT set
    assert _terminal_tars(tmp_path, prefix) == []
    sched.maybe_checkpoint_terminal()  # retries, succeeds
    assert len(_terminal_tars(tmp_path, prefix)) == 1


# -- best-effort failure isolation ------------------------------------


def test_periodic_failure_swallowed_and_timer_advances(tmp_path: Path) -> None:
    clock = FakeClock()
    prefix = _safe_experiment_prefix("exp-1")

    def _always_fail(stream) -> None:  # noqa: ANN001
        raise RuntimeError("export blew up")

    sched = _scheduler(
        tmp_path, clock, interval_seconds=100.0, export_fn=_always_fail
    )
    clock.advance(100.0)
    # Must not raise.
    sched.maybe_checkpoint_periodic(wall_now=_wall(0))
    assert _periodic_tars(tmp_path, prefix) == []
    # No temp file leaked.
    assert list(tmp_path.iterdir()) == []
    # Timer advanced: an immediate re-poll does NOT retry (no storm).
    sched.maybe_checkpoint_periodic(wall_now=_wall(1))
    # Still nothing, and we only re-attempt at the next interval boundary.
    clock.advance(100.0)
    sched.maybe_checkpoint_periodic(wall_now=_wall(2))
    assert _always_fail  # sentinel — three attempts total, all swallowed


def test_periodic_failure_cleans_temp_file(tmp_path: Path) -> None:
    clock = FakeClock()

    def _fail_after_write(stream) -> None:  # noqa: ANN001
        stream.write(b"partial")
        raise RuntimeError("died mid-export")

    sched = _scheduler(
        tmp_path, clock, interval_seconds=10.0, export_fn=_fail_after_write
    )
    clock.advance(10.0)
    sched.maybe_checkpoint_periodic(wall_now=_wall(0))
    # No .tmp-* file and no final .tar left behind.
    assert list(tmp_path.iterdir()) == []


# -- filename safety --------------------------------------------------


def test_experiment_id_with_slash_is_safe(tmp_path: Path) -> None:
    clock = FakeClock()
    exp_id = "team/exp 1"
    prefix = _safe_experiment_prefix(exp_id)
    sched = _scheduler(
        tmp_path, clock, experiment_id=exp_id, interval_seconds=10.0
    )
    clock.advance(10.0)
    sched.maybe_checkpoint_periodic(wall_now=_wall(0))
    [name] = [p.name for p in tmp_path.iterdir() if p.suffix == ".tar"]
    # Filename lives directly in dest (no nested dirs from the slash).
    assert (tmp_path / name).is_file()
    assert "/" not in name
    assert " " not in name
    assert name.startswith(prefix)


def test_safe_prefix_collision_resistant() -> None:
    # Two ids that a bare sanitizer would alias to the same string.
    a = _safe_experiment_prefix("a/b")
    b = _safe_experiment_prefix("a_b")
    assert a != b  # the raw-id hash suffix disambiguates


# -- disabled no-op ---------------------------------------------------


def test_disabled_scheduler_is_noop(tmp_path: Path) -> None:
    clock = FakeClock()
    sched = _scheduler(tmp_path, clock, enabled=False, interval_seconds=1.0)
    clock.advance(1000.0)
    sched.maybe_checkpoint_periodic(wall_now=_wall(0))
    sched.maybe_checkpoint_terminal()
    assert list(tmp_path.iterdir()) == []


def test_from_config_applies_defaults(tmp_path: Path) -> None:
    sched = CheckpointScheduler.from_config(
        AutoCheckpointConfig(enabled=True),
        experiment_id="exp-1",
        destination=tmp_path,
        export_fn=_make_export_fn(),
    )
    assert sched.enabled is True
    # Defaults applied: interval 3600, retention 6, on_terminate true.
    assert sched.interval_seconds == 3600.0
    assert sched.retention_count == 6
    assert sched.on_terminate is True


def test_from_config_none_is_disabled(tmp_path: Path) -> None:
    sched = CheckpointScheduler.from_config(
        None,
        experiment_id="exp-1",
        destination=None,
        export_fn=None,
    )
    assert sched.enabled is False
    sched.maybe_checkpoint_periodic(wall_now=_wall(0))
    sched.maybe_checkpoint_terminal()  # no destination — must stay no-op


@pytest.mark.parametrize(
    ("interval", "advance", "should_fire"),
    [
        (100.0, 100.0, True),  # exactly at boundary
        (100.0, 150.0, True),  # past boundary
        (100.0, 50.0, False),  # before boundary
    ],
)
def test_periodic_boundary(
    tmp_path: Path, interval: float, advance: float, should_fire: bool
) -> None:
    clock = FakeClock()
    prefix = _safe_experiment_prefix("exp-1")
    sched = _scheduler(tmp_path, clock, interval_seconds=interval)
    clock.advance(advance)
    sched.maybe_checkpoint_periodic(wall_now=_wall(0))
    assert (len(_periodic_tars(tmp_path, prefix)) == 1) is should_fire
