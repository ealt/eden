"""Orchestrator baseline-variant tests (issue #122).

Covers ``ensure_baseline_variant`` (default / override / disabled /
no-seed / idempotent / drift) and the load-bearing §8.1 / §D.5 carve:
a successful ``kind == "baseline"`` variant MUST NOT block the
multi-experiment drain-terminated check (``02-data-model.md`` §2.5).
These run against an in-memory store — no subprocesses — so the
termination-deadlock guard is deterministic.
"""

from __future__ import annotations

from typing import Any

import pytest
from eden_contracts import (
    BaselineConfig,
    EvaluationSchema,
    ExperimentConfig,
    ObjectiveSpec,
    Variant,
)
from eden_dispatch import InMemoryStore
from eden_orchestrator.baseline import BASELINE_VARIANT_ID, ensure_baseline_variant
from eden_orchestrator.multi_loop import _experiment_is_drained_terminated

_SEED = "a" * 40
_DT = "2026-05-01T00:00:00.000Z"


def _config(baseline: BaselineConfig | None = None) -> ExperimentConfig:
    # baseline carries NotNone; omit the kwarg when None (an absent block is
    # default-on, which is what we want for the no-arg case).
    extra: dict[str, Any] = {"baseline": baseline} if baseline is not None else {}
    return ExperimentConfig(
        parallel_variants=1,
        evaluation_schema=EvaluationSchema({"score": "real"}),
        objective=ObjectiveSpec(expr="score", direction="maximize"),
        **extra,
    )


def _store(*, base_commit_sha: str | None = _SEED) -> InMemoryStore:
    store = InMemoryStore(
        experiment_id="exp-baseline",
        evaluation_schema=EvaluationSchema({"score": "real"}),
        base_commit_sha=base_commit_sha,
    )
    for wid in ("orchestrator", "evaluator-1"):
        store.register_worker(wid)
    return store


def test_ensure_baseline_default_path_creates_starting() -> None:
    store = _store()
    ensure_baseline_variant(store=store, config=_config(), experiment_id="exp-baseline")
    variant = store.read_variant(BASELINE_VARIANT_ID)
    assert variant.kind == "baseline"
    assert variant.status == "starting"
    assert variant.idea_id is None
    assert variant.commit_sha == _SEED


def test_ensure_baseline_override_path_creates_success() -> None:
    store = _store()
    ensure_baseline_variant(
        store=store,
        config=_config(BaselineConfig(metrics={"score": 0.5})),
        experiment_id="exp-baseline",
    )
    variant = store.read_variant(BASELINE_VARIANT_ID)
    assert variant.status == "success"
    assert variant.evaluation == {"score": 0.5}


def test_ensure_baseline_disabled_creates_nothing() -> None:
    store = _store()
    ensure_baseline_variant(
        store=store,
        config=_config(BaselineConfig(enabled=False)),
        experiment_id="exp-baseline",
    )
    assert store.list_variants() == []


def test_ensure_baseline_no_base_commit_sha_skips() -> None:
    store = _store(base_commit_sha=None)
    # No crash, no baseline (legacy experiment with no recorded seed).
    ensure_baseline_variant(store=store, config=_config(), experiment_id="exp-baseline")
    assert store.list_variants() == []


def test_ensure_baseline_idempotent() -> None:
    store = _store()
    ensure_baseline_variant(store=store, config=_config(), experiment_id="exp-baseline")
    events_after_first = len(store.events())
    # Second call is a verified-read-back no-op: no new variant, no new event.
    ensure_baseline_variant(store=store, config=_config(), experiment_id="exp-baseline")
    assert len(store.list_variants()) == 1
    assert len(store.events()) == events_after_first


def test_ensure_baseline_drift_raises() -> None:
    store = _store()
    # A non-baseline row squatting the deterministic id is seed/config drift.
    store.create_variant(
        Variant(
            variant_id=BASELINE_VARIANT_ID,
            experiment_id="exp-baseline",
            idea_id="idea-1",
            status="starting",
            parent_commits=[_SEED],
            started_at=_DT,
        )
    )
    with pytest.raises(RuntimeError, match="drift"):
        ensure_baseline_variant(
            store=store, config=_config(), experiment_id="exp-baseline"
        )


def test_successful_baseline_does_not_block_drain() -> None:
    """§8.1 / §D.5: a successful baseline must not wedge termination."""
    store = _store()
    # Override-path baseline reaches `success` immediately, with no
    # variant_commit_sha (it is never integrated).
    ensure_baseline_variant(
        store=store,
        config=_config(BaselineConfig(metrics={"score": 0.5})),
        experiment_id="exp-baseline",
    )
    store.terminate_experiment(reason="done", terminated_by="orchestrator")
    # The baseline is `success` without a variant_commit_sha, but the drain
    # check excludes it — the experiment is drained-terminated. Without the
    # §D.5 carve this would return False forever (the deadlock §8.1 warns of).
    assert _experiment_is_drained_terminated(store) is True
