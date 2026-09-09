"""Read-time rollup of cost-ledger entries — reference-only (issue #343).

The third layer of the cost stack: [`cost.py`](cost.py) defines what a
spend event *is*, [`pricing.py`](pricing.py) turns tokens into dollars
when a provider didn't, and this module reduces entries into the buckets
an operator asks about — per role, per variant, per idea, per model.

Everything here is a **read-time** reduction over rows. Nothing is
stored, so no aggregate can drift from the ledger it summarizes, and a
rate-table correction re-prices history for free on the next read.
"""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict, Field

from .cost import CostEntry, ModelUsage
from .pricing import DerivedCost, PriceTable, derive_cost

_TOKEN_FIELDS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_creation_5m_input_tokens",
    "cache_creation_1h_input_tokens",
    "cache_read_input_tokens",
)

_ATTEMPT_FIELDS: tuple[str, ...] = ("num_turns", "duration_ms")
"""Fields that belong to an attempt, not to a model within it.

Summed for the attempt-level buckets and deliberately absent from
``by_model``, where a per-model share of an attempt's turn count or
wall-clock would be a fabricated division.
"""


class CostTokenTotals(BaseModel):
    """Summed dollars + tokens over a set of entries (or model slices).

    Three dollar figures rather than one, because collapsing them is how
    a reader ends up quoting a number whose provenance they can't state:

    - ``reported_cost_usd`` — what providers charged, summed.
    - ``derived_cost_usd`` — computed from tokens × rates for entries no
      provider priced (zero unless a price table was supplied).
    - ``total_cost_usd`` — their sum, which is the number to quote *with*
      ``basis``.

    **A dollar field is ``None`` when there is nothing behind it, never
    ``0.0``.** A numeric zero cannot be distinguished from "genuinely
    free" by a consumer summing the JSON, and no ``basis`` field alongside
    it fixes that — the sum is already wrong. ``None`` forces the
    question. So ``reported_cost_usd`` is ``None`` unless some entry
    reported, ``derived_cost_usd`` is ``None`` unless some entry was
    derived, and ``total_cost_usd`` is ``None`` when nothing in the bucket
    could be priced at all.

    Two completeness counters, because a total can fall short two ways:

    - ``entries_unpriced`` — entries that neither reported a figure nor
      could be derived.
    - ``entries_partially_priced`` — entries where *some* token classes
      priced and others had no rate. These carry dollars, so they are not
      unpriced; their figure is still a floor.

    Either being non-zero means ``total_cost_usd`` is a **floor**, not a
    total. :attr:`is_floor` says so in one place.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    entries: int = 0
    entries_reported: int = 0
    entries_derived: int = 0
    entries_unpriced: int = 0
    entries_partially_priced: int = 0
    reported_cost_usd: float | None = None
    derived_cost_usd: float | None = None
    total_cost_usd: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_creation_5m_input_tokens: int = 0
    cache_creation_1h_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    @property
    def is_floor(self) -> bool:
        """True when the total understates spend by a known-unknown amount."""
        return bool(self.entries_unpriced or self.entries_partially_priced)

    @property
    def basis(self) -> str:
        """How to characterize ``total_cost_usd`` in one word.

        ``reported`` / ``derived`` when every priced entry came from one
        source, ``mixed`` when both contributed, ``unpriced`` when
        nothing could be priced at all.
        """
        if self.entries_reported and self.entries_derived:
            return "mixed"
        if self.entries_reported:
            return "reported"
        if self.entries_derived:
            return "derived"
        return "unpriced"


class CostTotals(CostTokenTotals):
    """Attempt-level totals: :class:`CostTokenTotals` plus attempt fields."""

    num_turns: int = 0
    duration_ms: int = 0


class CostSummary(BaseModel):
    """Per-experiment cost rollup: overall, per role / variant / idea / model.

    A read-time reduction over ledger rows, not stored state — so it can
    never disagree with the ledger.

    Only ``by_role`` is a **complete** partition of ``totals``. The other
    three are partial by construction, and each for a reason worth
    knowing before dividing by one:

    - ``by_variant`` — ideation spend precedes any variant.
    - ``by_idea`` — a dispatch that produced several ideas spent one
      indivisible call on all of them, so it is attributed to none
      (splitting or picking would be an invention).
    - ``by_model`` — an entry appears when its source reported either a
      per-model split or a single model label (a single-model attempt's
      whole usage belongs to that model exactly, so it is included); an
      entry with neither is excluded. It carries no ``num_turns`` /
      ``duration_ms`` because those belong to the attempt.

    ``unattributed`` counts what each partial bucket left out, so a
    consumer can tell "no idea cost anything" from "the attribution
    wasn't available".
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    experiment_id: str
    totals: CostTotals
    by_role: dict[str, CostTotals]
    by_variant: dict[str, CostTotals]
    by_idea: dict[str, CostTotals]
    by_model: dict[str, CostTokenTotals]
    unattributed: dict[str, int] = Field(default_factory=dict)
    """Entry counts excluded from each partial bucket, keyed by bucket name."""

    price_table: dict[str, str] | None = None
    """Provenance of the rate table used for derivation, when one was.

    Echoed into the rollup (``source`` / ``as_of`` / ``unit``) so a
    derived figure can be audited against the rates that produced it
    rather than against whatever the table says today.
    """


def _refresh_total(totals: CostTokenTotals) -> None:
    """Recompute ``total_cost_usd``, keeping ``None`` for "nothing priced".

    Summing the two components would turn an all-unpriced bucket into
    ``0.0``, which is the exact confusion :class:`CostTokenTotals`
    documents against.
    """
    if totals.reported_cost_usd is None and totals.derived_cost_usd is None:
        totals.total_cost_usd = None
        return
    totals.total_cost_usd = (totals.reported_cost_usd or 0.0) + (
        totals.derived_cost_usd or 0.0
    )


def _accumulate(
    totals: CostTokenTotals, entry: CostEntry, derived: DerivedCost
) -> None:
    """Fold one entry (and its pricing verdict) into a bucket."""
    totals.entries += 1
    if derived.basis == "reported":
        totals.entries_reported += 1
        totals.reported_cost_usd = (totals.reported_cost_usd or 0.0) + (
            derived.total_cost_usd or 0.0
        )
    elif derived.basis == "derived":
        totals.entries_derived += 1
        totals.derived_cost_usd = (totals.derived_cost_usd or 0.0) + (
            derived.total_cost_usd or 0.0
        )
        if derived.partially_priced:
            totals.entries_partially_priced += 1
    else:
        totals.entries_unpriced += 1
    _refresh_total(totals)
    for field in _TOKEN_FIELDS:
        value = getattr(entry, field)
        if value is not None:
            setattr(totals, field, getattr(totals, field) + value)
    if isinstance(totals, CostTotals):
        for field in _ATTEMPT_FIELDS:
            value = getattr(entry, field)
            if value is not None:
                setattr(totals, field, getattr(totals, field) + value)


def _accumulate_model(
    totals: CostTokenTotals, usage: ModelUsage, derived: DerivedCost
) -> None:
    """Fold one model's slice of an attempt into a ``by_model`` bucket.

    Dollars come from the slice's own ``total_cost_usd`` when the provider
    reported one per model, else from this model's share of the
    derivation (:attr:`DerivedCost.per_model`), which was computed from
    this model's own tokens. A slice with neither counts as unpriced here
    even when the *attempt* carries a reported total — an attempt-level
    figure cannot be split across its models without inventing the split.
    """
    totals.entries += 1
    slice_derived = derived.per_model.get(usage.model)
    if usage.total_cost_usd is not None:
        totals.entries_reported += 1
        totals.reported_cost_usd = (
            totals.reported_cost_usd or 0.0
        ) + usage.total_cost_usd
    elif slice_derived is not None:
        # Priced from this model's own tokens, so the amount is this
        # model's — no share of an attempt total is being invented.
        totals.entries_derived += 1
        totals.derived_cost_usd = (totals.derived_cost_usd or 0.0) + slice_derived
        # This model's own status, NOT the attempt's: a fully-priced model
        # sharing an attempt with an unknown one is still exact.
        if usage.model in derived.partial_models:
            totals.entries_partially_priced += 1
    else:
        totals.entries_unpriced += 1
    _refresh_total(totals)
    for field in (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ):
        value = getattr(usage, field)
        if value is not None:
            setattr(totals, field, getattr(totals, field) + value)


def _model_slices(entry: CostEntry) -> list[ModelUsage]:
    """Per-model slices for an entry, synthesizing the single-model case.

    An attempt that used exactly one model reports its label in ``model``
    and its tokens at the top level, with no ``models`` list — a gateway
    bridge does exactly this. Attributing all of that attempt's tokens
    and its reported dollars to its one model is **exact**, not an
    allocation, so ``by_model`` covers it rather than filing every
    single-model attempt under "unattributed".
    """
    if entry.models:
        return list(entry.models)
    if entry.model is None:
        return []
    return [
        ModelUsage(
            model=entry.model,
            total_cost_usd=entry.total_cost_usd,
            input_tokens=entry.input_tokens,
            output_tokens=entry.output_tokens,
            cache_creation_input_tokens=entry.cache_creation_input_tokens,
            cache_read_input_tokens=entry.cache_read_input_tokens,
        )
    ]


def summarize(
    experiment_id: str,
    entries: Iterable[CostEntry],
    *,
    price_table: PriceTable | None = None,
) -> CostSummary:
    """Reduce ledger entries into a :class:`CostSummary`.

    A pure function so the same reduction serves an in-process consumer
    and one reading entries over the wire — there is no server-side
    summary endpoint to drift from it.

    ``price_table``, when supplied, prices entries no provider priced
    (:mod:`eden_storage.pricing`). Reported figures are never overridden,
    and derived dollars stay in their own field, so the two never blur.
    """
    summary = CostSummary(
        experiment_id=experiment_id,
        totals=CostTotals(),
        by_role={},
        by_variant={},
        by_idea={},
        by_model={},
        unattributed={"by_variant": 0, "by_idea": 0, "by_model": 0},
    )
    if price_table is not None:
        summary.price_table = {
            "source": price_table.source,
            "as_of": price_table.as_of,
            "unit": price_table.unit,
            # An unfilled table is echoed rather than dropped — a reader
            # who passed one deserves to see that it did nothing, not to
            # infer it from an all-unpriced total.
            "usable": "yes" if price_table.is_usable() else "no",
        }
    for entry in entries:
        derived = derive_cost(entry, price_table)
        _accumulate(summary.totals, entry, derived)
        _accumulate(
            summary.by_role.setdefault(entry.role, CostTotals()), entry, derived
        )
        if entry.variant_id is not None:
            _accumulate(
                summary.by_variant.setdefault(entry.variant_id, CostTotals()),
                entry,
                derived,
            )
        else:
            summary.unattributed["by_variant"] += 1
        if entry.idea_id is not None:
            _accumulate(
                summary.by_idea.setdefault(entry.idea_id, CostTotals()),
                entry,
                derived,
            )
        else:
            summary.unattributed["by_idea"] += 1
        for usage in _model_slices(entry):
            _accumulate_model(
                summary.by_model.setdefault(usage.model, CostTokenTotals()),
                usage,
                derived,
            )
        if not entry.models and entry.model is None:
            summary.unattributed["by_model"] += 1
    return summary
