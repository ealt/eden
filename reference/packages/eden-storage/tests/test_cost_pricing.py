"""Token→dollar derivation and the rollup buckets it feeds (issue #343).

Two things are under test and both are honesty properties rather than
arithmetic ones:

- **A derived figure never passes for a reported one.** Reported wins,
  derived lands in its own field, and every bucket carries a ``basis``.
- **A missing rate is a gap, never a zero.** Including the subtle one:
  cache writes whose TTL tier was never reported can't be priced,
  because 5-minute and 1-hour writes bill differently.

The arithmetic tests use round rates so a wrong unit (per-token instead
of per-million) is visible at a glance rather than hidden in a decimal.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from eden_storage import (
    CostEntry,
    ModelRates,
    ModelUsage,
    PriceTable,
    derive_cost,
    load_price_table,
    summarize,
)

EXPERIMENT_ID = "exp_zp0q3v6xsnk0jf9hfb54m73626"

# $3/Mtok input, $15/Mtok output, $3.75 5m cache write, $6 1h cache
# write, $0.30 cache read — the Anthropic rate *shape*, round enough to
# check by eye. Not real prices; the point is the per-class structure.
RATES = ModelRates(
    input=3.0,
    output=15.0,
    cache_write_5m=3.75,
    cache_write_1h=6.0,
    cache_read=0.30,
)


def _table(**overrides: Any) -> PriceTable:
    fields: dict[str, Any] = {
        "source": "test rates (not real prices)",
        "as_of": "2026-07-30",
        "unit": "usd_per_mtok",
        "rates": {"m1": RATES.model_dump()},
    }
    fields.update(overrides)
    return PriceTable.model_validate(fields)


def _entry(entry_id: str = "e1", **overrides: Any) -> CostEntry:
    fields: dict[str, Any] = {
        "entry_id": entry_id,
        "experiment_id": EXPERIMENT_ID,
        "role": "executor",
        "source": "worker-reported",
        "task_id": "execution-1",
    }
    fields.update(overrides)
    return CostEntry.model_validate(fields)


# ----------------------------------------------------------------------
# The rate table is configuration with provenance
# ----------------------------------------------------------------------


def test_table_requires_source_and_as_of() -> None:
    """Rates without provenance can't be audited, so they aren't accepted."""
    with pytest.raises(ValueError, match="as_of"):
        PriceTable.model_validate({"source": "somewhere"})
    with pytest.raises(ValueError, match="source"):
        PriceTable.model_validate({"as_of": "2026-07-30"})


def test_per_token_unit_is_rejected() -> None:
    """A table authored per-token would be off by 1e6 and look plausible."""
    with pytest.raises(ValueError, match="unit"):
        PriceTable.model_validate(
            {"source": "s", "as_of": "2026-07-30", "unit": "usd_per_token"}
        )


def test_unfilled_template_is_not_usable() -> None:
    """The shipped example must not price anything until it's filled in."""
    # tests/ -> eden-storage -> packages -> reference
    reference_root = Path(__file__).resolve().parents[3]
    example = load_price_table(
        reference_root / "pricing" / "price-table.example.json"
    )
    assert example.as_of == "unset"
    assert not example.is_usable()
    # Every rate is null on purpose — no shipped number can be right for
    # every deployment (list vs negotiated, direct vs Bedrock, region).
    for rates in example.rates.values():
        assert rates.model_dump(exclude_none=True) == {}


def test_unfilled_table_derives_nothing_and_says_why() -> None:
    table = _table(as_of="unset")
    derived = derive_cost(_entry(input_tokens=1000), table)
    assert derived.basis == "unpriced"
    assert derived.total_cost_usd is None
    assert any("price table" in gap for gap in derived.gaps)


def test_malformed_table_raises_rather_than_silently_skipping(
    tmp_path: Path,
) -> None:
    """An operator's typo must not read as 'no table supplied'."""
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        load_price_table(bad)


def test_alias_resolves_a_provider_prefixed_label(tmp_path: Path) -> None:
    """The same model arrives under different labels per provider."""
    table = _table(aliases={"amazon-bedrock/us.m1-v1": "m1"})
    entry = _entry(model="amazon-bedrock/us.m1-v1", input_tokens=1_000_000)
    derived = derive_cost(entry, table)
    assert derived.basis == "derived"
    assert derived.total_cost_usd == pytest.approx(3.0)


# ----------------------------------------------------------------------
# Reported vs derived
# ----------------------------------------------------------------------


def test_reported_figure_wins_over_derivation() -> None:
    """The provider's own accounting beats our arithmetic."""
    entry = _entry(model="m1", total_cost_usd=0.5, input_tokens=1_000_000)
    derived = derive_cost(entry, _table())
    assert derived.basis == "reported"
    assert derived.total_cost_usd == pytest.approx(0.5)
    assert derived.priced_classes == ()


def test_no_table_leaves_an_unreported_entry_unpriced() -> None:
    derived = derive_cost(_entry(model="m1", input_tokens=1000), None)
    assert derived.basis == "unpriced"
    assert derived.total_cost_usd is None


# ----------------------------------------------------------------------
# Per-class arithmetic, including the cache tiers
# ----------------------------------------------------------------------


def test_each_token_class_is_priced_at_its_own_rate() -> None:
    entry = _entry(
        model="m1",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_creation_input_tokens=2_000_000,
        cache_creation_5m_input_tokens=1_000_000,
        cache_creation_1h_input_tokens=1_000_000,
        cache_read_input_tokens=1_000_000,
    )
    derived = derive_cost(entry, _table())
    # 3 + 15 + 3.75 + 6 + 0.30
    assert derived.total_cost_usd == pytest.approx(28.05)
    assert set(derived.priced_classes) == {
        "input",
        "output",
        "cache_write_5m",
        "cache_write_1h",
        "cache_read",
    }
    assert derived.gaps == ()


def test_cache_tiers_are_not_interchangeable() -> None:
    """The whole reason the TTL split is captured: the rates differ."""
    at_5m = derive_cost(
        _entry(
            model="m1",
            cache_creation_input_tokens=1_000_000,
            cache_creation_5m_input_tokens=1_000_000,
        ),
        _table(),
    )
    at_1h = derive_cost(
        _entry(
            model="m1",
            cache_creation_input_tokens=1_000_000,
            cache_creation_1h_input_tokens=1_000_000,
        ),
        _table(),
    )
    assert at_5m.total_cost_usd == pytest.approx(3.75)
    assert at_1h.total_cost_usd == pytest.approx(6.0)


def test_cache_reads_are_far_cheaper_than_fresh_input() -> None:
    """Pricing cache reads as input would overstate a cached run ~10x."""
    as_read = derive_cost(
        _entry(model="m1", cache_read_input_tokens=1_000_000), _table()
    )
    as_input = derive_cost(_entry(model="m1", input_tokens=1_000_000), _table())
    assert as_read.total_cost_usd is not None
    assert as_input.total_cost_usd is not None
    assert as_read.total_cost_usd * 5 < as_input.total_cost_usd


def test_untiered_cache_writes_are_reported_not_priced() -> None:
    """An aggregate-only cache-write count can't be priced honestly."""
    entry = _entry(
        model="m1", input_tokens=1_000_000, cache_creation_input_tokens=500_000
    )
    derived = derive_cost(entry, _table())
    assert derived.basis == "derived"
    # Only the input tokens were priced; the cache writes are a stated gap.
    assert derived.total_cost_usd == pytest.approx(3.0)
    assert any("TTL" in gap for gap in derived.gaps)
    assert any("500000" in gap for gap in derived.gaps)


def test_missing_class_rate_is_a_gap_not_a_zero() -> None:
    table = _table(rates={"m1": {"input": 3.0}})
    entry = _entry(model="m1", input_tokens=1_000_000, output_tokens=1_000_000)
    derived = derive_cost(entry, table)
    assert derived.total_cost_usd == pytest.approx(3.0)
    assert derived.priced_classes == ("input",)
    assert any("output" in gap for gap in derived.gaps)


def test_unknown_model_is_a_gap() -> None:
    derived = derive_cost(_entry(model="mystery", input_tokens=1000), _table())
    assert derived.basis == "unpriced"
    assert any("mystery" in gap for gap in derived.gaps)


def test_entry_without_a_model_label_is_a_gap() -> None:
    derived = derive_cost(_entry(input_tokens=1000), _table())
    assert derived.basis == "unpriced"
    assert any("no model label" in gap for gap in derived.gaps)


# ----------------------------------------------------------------------
# Multi-model derivation
# ----------------------------------------------------------------------


def test_multi_model_entry_prices_each_model_at_its_own_rates() -> None:
    table = _table(
        rates={
            "m1": RATES.model_dump(),
            "m2": {"input": 1.0, "output": 5.0, "cache_read": 0.1},
        }
    )
    entry = _entry(
        models=[
            ModelUsage(model="m1", input_tokens=1_000_000),
            ModelUsage(model="m2", output_tokens=1_000_000),
        ]
    )
    derived = derive_cost(entry, table)
    assert derived.basis == "derived"
    assert derived.total_cost_usd == pytest.approx(3.0 + 5.0)


def test_per_model_cache_writes_have_no_tier_and_are_a_gap() -> None:
    """``modelUsage`` reports no TTL split, so those tokens stay unpriced."""
    entry = _entry(
        models=[
            ModelUsage(
                model="m1", input_tokens=1_000_000, cache_creation_input_tokens=99
            )
        ]
    )
    derived = derive_cost(entry, _table())
    assert derived.total_cost_usd == pytest.approx(3.0)
    assert any("per-model TTL split" in gap for gap in derived.gaps)


def test_one_unknown_model_does_not_void_the_others() -> None:
    entry = _entry(
        models=[
            ModelUsage(model="m1", input_tokens=1_000_000),
            ModelUsage(model="mystery", input_tokens=1_000_000),
        ]
    )
    derived = derive_cost(entry, _table())
    assert derived.basis == "derived"
    assert derived.total_cost_usd == pytest.approx(3.0)
    assert any("mystery" in gap for gap in derived.gaps)


# ----------------------------------------------------------------------
# Rollup buckets
# ----------------------------------------------------------------------


def test_summary_separates_reported_from_derived() -> None:
    entries = [
        _entry("reported", model="m1", total_cost_usd=0.5),
        _entry("derived", model="m1", input_tokens=1_000_000),
    ]
    summary = summarize(EXPERIMENT_ID, entries, price_table=_table())
    totals = summary.totals
    assert totals.reported_cost_usd == pytest.approx(0.5)
    assert totals.derived_cost_usd == pytest.approx(3.0)
    assert totals.total_cost_usd == pytest.approx(3.5)
    assert totals.entries_reported == 1
    assert totals.entries_derived == 1
    assert totals.basis == "mixed"


@pytest.mark.parametrize(
    ("kwargs", "table_supplied", "expected"),
    [
        ({"total_cost_usd": 0.5}, False, "reported"),
        ({"model": "m1", "input_tokens": 1_000_000}, True, "derived"),
        ({"model": "m1", "input_tokens": 1_000_000}, False, "unpriced"),
        ({"num_turns": 3}, True, "unpriced"),
    ],
)
def test_basis_labels_each_case(
    kwargs: dict[str, Any], table_supplied: bool, expected: str
) -> None:
    summary = summarize(
        EXPERIMENT_ID,
        [_entry(**kwargs)],
        price_table=_table() if table_supplied else None,
    )
    assert summary.totals.basis == expected


def test_summary_echoes_the_rate_table_provenance() -> None:
    """A derived number is auditable only against the rates that made it."""
    summary = summarize(EXPERIMENT_ID, [_entry(model="m1")], price_table=_table())
    assert summary.price_table == {
        "source": "test rates (not real prices)",
        "as_of": "2026-07-30",
        "unit": "usd_per_mtok",
        "usable": "yes",
    }


def test_summary_without_a_table_has_no_provenance_block() -> None:
    summary = summarize(EXPERIMENT_ID, [_entry(model="m1")])
    assert summary.price_table is None


def test_by_idea_buckets_and_counts_what_it_cannot_attribute() -> None:
    entries = [
        _entry("e1", idea_id="idea-1", total_cost_usd=0.1),
        _entry("e2", idea_id="idea-1", total_cost_usd=0.2),
        _entry("e3", total_cost_usd=0.7),  # a multi-idea dispatch
    ]
    summary = summarize(EXPERIMENT_ID, entries)
    assert set(summary.by_idea) == {"idea-1"}
    assert summary.by_idea["idea-1"].total_cost_usd == pytest.approx(0.3)
    assert summary.unattributed["by_idea"] == 1
    # by_role stays the complete partition.
    assert summary.by_role["executor"].total_cost_usd == pytest.approx(
        summary.totals.total_cost_usd
    )


def test_by_model_slices_tokens_and_omits_attempt_fields() -> None:
    entries = [
        _entry(
            "e1",
            num_turns=7,
            duration_ms=1234,
            models=[
                ModelUsage(model="m1", input_tokens=10, total_cost_usd=0.1),
                ModelUsage(model="m2", output_tokens=20, total_cost_usd=0.2),
            ],
        ),
        _entry("e2", total_cost_usd=0.4),  # no per-model split
    ]
    summary = summarize(EXPERIMENT_ID, entries)
    assert set(summary.by_model) == {"m1", "m2"}
    assert summary.by_model["m1"].total_cost_usd == pytest.approx(0.1)
    assert summary.by_model["m2"].output_tokens == 20
    assert summary.unattributed["by_model"] == 1
    # num_turns / duration_ms belong to the attempt; a per-model share
    # would be invented, so the model bucket has no such field at all.
    assert not hasattr(summary.by_model["m1"], "num_turns")
    assert summary.totals.num_turns == 7


def test_by_model_slice_without_its_own_dollars_is_unpriced_there() -> None:
    """An attempt-level figure can't be attributed to one of its models."""
    entry = _entry(
        "e1",
        total_cost_usd=0.9,
        models=[ModelUsage(model="m1", input_tokens=10)],
    )
    summary = summarize(EXPERIMENT_ID, [entry])
    assert summary.totals.basis == "reported"
    assert summary.by_model["m1"].entries_unpriced == 1
    assert summary.by_model["m1"].total_cost_usd == 0.0
    assert summary.by_model["m1"].input_tokens == 10


def test_summary_json_round_trips() -> None:
    """The summary is a machine contract; it must serialize cleanly."""
    summary = summarize(
        EXPERIMENT_ID,
        [_entry("e1", model="m1", input_tokens=10, idea_id="i1", variant_id="v1")],
        price_table=_table(),
    )
    dumped = json.loads(summary.model_dump_json())
    assert dumped["by_idea"]["i1"]["entries"] == 1
    assert dumped["price_table"]["as_of"] == "2026-07-30"


def test_single_model_attempt_appears_in_by_model() -> None:
    """A gateway attempt reports one label and no split; that is exact.

    Filing every single-model attempt under "unattributed" would leave
    ``by_model`` covering only multi-model runs, which is the rarer case.
    """
    entries = [
        _entry("e1", model="m1", total_cost_usd=0.4, input_tokens=10),
        _entry("e2", total_cost_usd=0.1),  # no label at all
    ]
    summary = summarize(EXPERIMENT_ID, entries)
    assert set(summary.by_model) == {"m1"}
    assert summary.by_model["m1"].total_cost_usd == pytest.approx(0.4)
    assert summary.by_model["m1"].input_tokens == 10
    assert summary.by_model["m1"].entries_reported == 1
    assert summary.unattributed["by_model"] == 1


def test_explicit_split_wins_over_the_single_label() -> None:
    """When both are present the reported split is the finer truth."""
    entry = _entry(
        "e1",
        model="m1",
        models=[
            ModelUsage(model="m1", input_tokens=5),
            ModelUsage(model="m2", input_tokens=7),
        ],
    )
    summary = summarize(EXPERIMENT_ID, [entry])
    assert set(summary.by_model) == {"m1", "m2"}
    assert summary.by_model["m1"].input_tokens == 5


def test_by_model_credits_each_model_its_own_derived_dollars() -> None:
    """A derived per-model row must show its amount, not $0.

    A row whose basis says ``derived`` while its dollars say zero is worse
    than no row: it reads as "this model was free" for exactly the
    attempts pricing exists to illuminate.
    """
    table = _table(
        rates={
            "m1": {"input": 3.0},
            "m2": {"output": 15.0},
        }
    )
    entry = _entry(
        "e1",
        models=[
            ModelUsage(model="m1", input_tokens=1_000_000),
            ModelUsage(model="m2", output_tokens=1_000_000),
        ],
    )
    summary = summarize(EXPERIMENT_ID, [entry], price_table=table)
    assert summary.by_model["m1"].derived_cost_usd == pytest.approx(3.0)
    assert summary.by_model["m2"].derived_cost_usd == pytest.approx(15.0)
    assert summary.by_model["m1"].basis == "derived"
    assert summary.totals.derived_cost_usd == pytest.approx(18.0)


def test_by_model_derived_sums_to_the_attempt_total() -> None:
    """Per-model derived dollars are a partition, not an approximation."""
    entry = _entry("e1", model="m1", input_tokens=1_000_000, output_tokens=1_000_000)
    summary = summarize(EXPERIMENT_ID, [entry], price_table=_table())
    assert summary.by_model["m1"].derived_cost_usd == pytest.approx(
        summary.totals.derived_cost_usd
    )


def test_unpriced_model_slice_shows_no_dollars_and_says_unpriced() -> None:
    entry = _entry("e1", models=[ModelUsage(model="mystery", input_tokens=10)])
    summary = summarize(EXPERIMENT_ID, [entry], price_table=_table())
    assert summary.by_model["mystery"].basis == "unpriced"
    assert summary.by_model["mystery"].total_cost_usd == 0.0
    assert summary.by_model["mystery"].input_tokens == 10
