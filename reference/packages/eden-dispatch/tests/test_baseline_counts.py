"""A ``kind == "baseline"`` variant is invisible to stock policy counts.

The seed baseline (``spec/v0/02-data-model.md`` §9.4) is the experiment
seed elevated to a variant — not an executor-produced candidate. It MUST
NOT consume the ``parallel_variants`` in-flight budget
(``running_variant_count``) nor the ``max_variants`` attempt budget
(``attempted_variant_count``); plan §D.6. This test asserts both counts
exclude the baseline while an ordinary starting variant is still counted.
"""

from __future__ import annotations

from eden_contracts import EvaluationSchema, Variant
from eden_dispatch import build_experiment_state_view
from eden_storage import InMemoryStore

_SEED = "a" * 40
_DT = "2026-05-01T00:00:00.000Z"
# Valid opaque experiment id (issue #128 grammar: ^exp_[Crockford]{26}$).
_EXP = "exp_0123456789abcdefghjkmnpqrs"


def _store() -> InMemoryStore:
    store = InMemoryStore(
        experiment_id=_EXP,
        evaluation_schema=EvaluationSchema({"score": "real"}),
    )
    for wid in ("orchestrator", "executor-1", "evaluator-1"):
        store.register_worker(wid)
    return store


def test_baseline_excluded_from_running_and_attempted_counts() -> None:
    store = _store()
    # A default-path baseline (starting) plus one ordinary starting variant.
    store.create_variant(
        Variant(
            variant_id="baseline",
            experiment_id=_EXP,
            kind="baseline",
            status="starting",
            parent_commits=[_SEED],
            commit_sha=_SEED,
            started_at=_DT,
        )
    )
    store.create_variant(
        Variant(
            variant_id="variant-1",
            experiment_id=_EXP,
            idea_id="idea-1",
            status="starting",
            parent_commits=[_SEED],
            started_at=_DT,
        )
    )
    view = build_experiment_state_view(store)
    # Only the ordinary variant counts toward the in-flight + attempt budgets.
    assert view.running_variant_count == 1
    assert view.attempted_variant_count == 1
