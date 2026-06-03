"""Storage-layer tests for the ``kind == "baseline"`` variant (issue #122).

Covers the ``create_variant`` precondition relaxation (a baseline MAY be
created directly in ``success``), the create-time evaluation-schema
validation on the override path, the composite ``variant.started`` +
``variant.succeeded`` emission, the ``idea_id``-optional shape, the
``integrate_variant`` baseline rejection, and untargeted baseline
evaluation-task dispatch. See ``spec/v0/02-data-model.md`` §9.4 and
``spec/v0/08-storage.md`` §1.7.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from eden_contracts import EvaluationSchema, Variant
from eden_storage import InvalidPrecondition, Store

_SEED = "a" * 40
_DT = "2026-05-01T00:00:00.000Z"
_SCHEMA = EvaluationSchema.model_validate({"score": "real"})


def _baseline(
    *,
    status: str = "starting",
    evaluation: dict[str, object] | None = None,
    completed_at: str | None = None,
    experiment_id: str = "exp-test",
) -> Variant:
    # Omit evaluation / completed_at when None: those fields carry NotNone
    # and reject an explicit null at construction.
    extra: dict[str, Any] = {}
    if evaluation is not None:
        extra["evaluation"] = evaluation
    if completed_at is not None:
        extra["completed_at"] = completed_at
    return Variant(
        variant_id="baseline",
        experiment_id=experiment_id,
        kind="baseline",
        status=status,  # type: ignore[arg-type]
        parent_commits=[_SEED],
        commit_sha=_SEED,
        started_at=_DT,
        **extra,
    )


def test_baseline_default_path_created_starting(
    make_store: Callable[..., Store],
) -> None:
    """Default-path baseline: created ``starting`` with no idea_id."""
    store = make_store(evaluation_schema=_SCHEMA)
    store.create_variant(_baseline())
    got = store.read_variant("baseline")
    assert got.kind == "baseline"
    assert got.idea_id is None
    assert got.status == "starting"
    assert got.commit_sha == _SEED
    # variant.started carries the required kind and omits idea_id (§3.3).
    started = [e for e in store.events() if e.type == "variant.started"]
    assert len(started) == 1
    assert started[0].data["kind"] == "baseline"
    assert "idea_id" not in started[0].data
    # No variant.succeeded yet — the default path reaches success via the
    # normal evaluation-acceptance flow.
    assert not [e for e in store.events() if e.type == "variant.succeeded"]


def test_baseline_override_path_created_success(
    make_store: Callable[..., Store],
) -> None:
    """Override-path baseline: directly ``success`` with config metrics."""
    store = make_store(evaluation_schema=_SCHEMA)
    store.create_variant(
        _baseline(status="success", evaluation={"score": 0.5}, completed_at=_DT)
    )
    got = store.read_variant("baseline")
    assert got.status == "success"
    assert got.evaluation == {"score": 0.5}
    # The override create emits variant.started THEN variant.succeeded
    # atomically (05-event-protocol.md §3.3).
    types = [e.type for e in store.events()]
    assert types == ["variant.started", "variant.succeeded"]


def test_baseline_override_bad_metrics_rejected(
    make_store: Callable[..., Store],
) -> None:
    """Override metrics MUST validate against evaluation_schema at create time."""
    store = make_store(evaluation_schema=_SCHEMA)
    with pytest.raises(InvalidPrecondition):
        store.create_variant(
            _baseline(
                status="success",
                evaluation={"not_a_metric": 1.0},
                completed_at=_DT,
            )
        )
    # Nothing committed.
    assert not store.events()


def test_baseline_override_missing_metrics_rejected(
    make_store: Callable[..., Store],
) -> None:
    """A baseline created directly in ``success`` MUST carry metrics."""
    store = make_store(evaluation_schema=_SCHEMA)
    with pytest.raises(InvalidPrecondition):
        store.create_variant(_baseline(status="success", completed_at=_DT))


def test_ordinary_variant_must_start_in_starting(
    make_store: Callable[..., Store],
) -> None:
    """The direct-success relaxation is baseline-only."""
    store = make_store(evaluation_schema=_SCHEMA)
    ordinary = Variant(
        variant_id="variant-1",
        experiment_id="exp-test",
        idea_id="idea-1",
        status="success",
        parent_commits=[_SEED],
        commit_sha="b" * 40,
        evaluation={"score": 0.9},
        started_at=_DT,
        completed_at=_DT,
    )
    with pytest.raises(InvalidPrecondition):
        store.create_variant(ordinary)


def test_integrate_baseline_rejected(
    make_store: Callable[..., Store],
) -> None:
    """A baseline is never integrated (06-integrator.md §2, 07 §5)."""
    store = make_store(evaluation_schema=_SCHEMA)
    store.create_variant(
        _baseline(status="success", evaluation={"score": 0.5}, completed_at=_DT)
    )
    with pytest.raises(InvalidPrecondition):
        store.integrate_variant("baseline", "c" * 40)
    # No variant_commit_sha written, no variant.integrated event.
    assert store.read_variant("baseline").variant_commit_sha is None
    assert not [e for e in store.events() if e.type == "variant.integrated"]


def test_baseline_evaluation_dispatch_is_untargeted(
    make_store: Callable[..., Store],
) -> None:
    """A baseline's evaluation task has no idea-derived target (§D.7)."""
    store = make_store(evaluation_schema=_SCHEMA)
    store.create_variant(_baseline())
    task = store.create_evaluation_task("evaluate-baseline", "baseline")
    assert task.target is None
