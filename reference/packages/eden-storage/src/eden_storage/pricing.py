"""Deriving dollars from token counts — reference-only (issue #343).

Not every provider reports what it charged. Claude Code emits
``total_cost_usd``; an OpenAI-compatible gateway typically reports only
``usage`` token counts. Rather than leave those attempts priceless (or,
worse, guess a blended per-token rate), this module prices recorded token
counts against an **operator-supplied rate table**.

Three properties are the whole point, and each exists because its
absence is a specific way to mislead a reader:

1. **A derived figure is never mistaken for a reported one.** Nothing
   here writes to :attr:`eden_storage.CostEntry.total_cost_usd` — that
   field keeps meaning "the provider said so". Derivation happens at
   read time and lands in a separate :class:`DerivedCost` carrying its
   own ``basis``, so a rollup that mixes the two can say which is which.
2. **Rates are configuration with provenance, not constants.** Published
   list prices, negotiated rates, Bedrock-vs-direct, and cache-TTL
   variants all differ, and every one of them goes stale. A table
   therefore MUST declare ``source`` and ``as_of``, and both travel into
   the report output so a number can be audited against the rates that
   produced it.
3. **An unpriced token class is a reported gap, never a zero.** A
   missing rate that silently priced at 0 would turn "we don't know" into
   "it was free" — the exact failure this module is meant to prevent.
   :class:`DerivedCost.gaps` names each one.

Cache pricing is where a naive implementation goes wrong. Anthropic-style
pricing has four distinct input rates, not one: fresh input, cache writes
at the 5-minute TTL, cache writes at the 1-hour TTL (materially more
expensive), and cache reads (roughly an order of magnitude *cheaper* than
fresh input). A single "input" rate applied to
``input_tokens + cache_creation + cache_read`` can be off by a large
multiple in either direction, which is why
:class:`eden_storage.CostEntry` records the classes separately and why
:class:`ModelRates` is per-class.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from .cost import CostEntry

TOKENS_PER_MTOK = 1_000_000

TokenClass = Literal[
    "input",
    "output",
    "cache_write_5m",
    "cache_write_1h",
    "cache_read",
]
"""The classes a token can be billed under. See the module docstring."""


class ModelRates(BaseModel):
    """Per-token-class rates for one model, in USD per million tokens.

    Every rate is optional: a table that knows input/output but not the
    cache tiers is still useful, and the classes it lacks surface as
    gaps rather than as zeros.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    input: Annotated[float, Field(ge=0.0)] | None = None
    output: Annotated[float, Field(ge=0.0)] | None = None
    cache_write_5m: Annotated[float, Field(ge=0.0)] | None = None
    cache_write_1h: Annotated[float, Field(ge=0.0)] | None = None
    cache_read: Annotated[float, Field(ge=0.0)] | None = None

    def rate_for(self, token_class: str) -> float | None:
        """Return the USD-per-Mtok rate for a class, or ``None`` if unknown."""
        return getattr(self, token_class, None)


class PriceTable(BaseModel):
    """An operator-supplied rate table with provenance.

    ``unit`` is pinned rather than assumed: per-million-tokens is the
    convention every provider publishes in, and a table authored in
    per-token units would otherwise be off by a factor of a million and
    look plausible. An unrecognized unit fails validation.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    source: Annotated[str, Field(min_length=1)]
    """Where these numbers came from — a URL, a contract, "operator-supplied"."""

    as_of: Annotated[str, Field(min_length=1)]
    """The date the rates were valid (``YYYY-MM-DD``), or ``"unset"``.

    ``"unset"`` is accepted so a template ships without inventing a
    date, and :meth:`is_usable` treats it as "this table has not been
    filled in" so a run can't quietly price against a placeholder.
    """

    unit: Literal["usd_per_mtok"] = "usd_per_mtok"
    notes: str | None = None

    rates: dict[str, ModelRates] = Field(default_factory=dict)
    """Model label → per-class rates."""

    aliases: dict[str, str] = Field(default_factory=dict)
    """Recorded model label → key in ``rates``.

    Exact-match aliases rather than prefix or fuzzy matching: the same
    model reaches EDEN under provider-specific labels (a bare
    ``claude-sonnet-4-6`` from Claude Code, an
    ``amazon-bedrock/us.anthropic.…-v1`` from a gateway), and guessing
    which label means which model is how you end up pricing an Opus run
    at Haiku rates.
    """

    def is_usable(self) -> bool:
        """False for an unfilled template (no rates, or a placeholder date)."""
        return bool(self.rates) and self.as_of != "unset"

    def rates_for(self, model: str | None) -> ModelRates | None:
        """Resolve a recorded model label to its rates, or ``None``."""
        if model is None:
            return None
        key = self.aliases.get(model, model)
        return self.rates.get(key)


def load_price_table(path: Path | str) -> PriceTable:
    """Load and validate a JSON price table.

    Raises ``ValueError`` on a malformed table — a silently-ignored
    unparseable table would report an unpriced run as if no table had
    been supplied, hiding the operator's own typo.
    """
    text = Path(path).read_text(encoding="utf-8")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"price table {path} is not valid JSON: {exc}") from exc
    return PriceTable.model_validate(raw)


class DerivedCost(BaseModel):
    """Dollars computed from token counts, with its own provenance.

    ``basis`` is the honesty field. ``"reported"`` means the provider's
    own figure was used and nothing was derived; ``"derived"`` means the
    figure came from tokens × rates; ``"unpriced"`` means neither was
    available. A consumer that shows a dollar amount without showing
    this is misrepresenting it.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    basis: Literal["reported", "derived", "unpriced"]
    total_cost_usd: float | None = None
    priced_classes: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()
    """Human-readable reasons a class or model could not be priced."""

    per_model: dict[str, float] = Field(default_factory=dict)
    """Derived dollars attributed to each model, when derivation ran.

    Kept alongside the total so a per-model rollup credits each model
    what its own tokens cost, instead of showing a model with a
    ``derived`` basis and $0 — which would read as "free" for exactly the
    attempts pricing was supposed to illuminate.
    """


def _class_tokens(entry: CostEntry) -> list[tuple[TokenClass, int]]:
    """Split an entry's tokens into billable classes.

    Cache writes come from the per-TTL fields when the source reported
    them. When only the aggregate is present the tier is genuinely
    unknown, so it is left for :func:`derive_cost` to report as a gap —
    picking a tier would be a coin flip with a multiple-of-two error.
    """
    classes: list[tuple[TokenClass, int]] = []
    if entry.input_tokens:
        classes.append(("input", entry.input_tokens))
    if entry.output_tokens:
        classes.append(("output", entry.output_tokens))
    if entry.cache_creation_5m_input_tokens:
        classes.append(("cache_write_5m", entry.cache_creation_5m_input_tokens))
    if entry.cache_creation_1h_input_tokens:
        classes.append(("cache_write_1h", entry.cache_creation_1h_input_tokens))
    if entry.cache_read_input_tokens:
        classes.append(("cache_read", entry.cache_read_input_tokens))
    return classes


def _unsplit_cache_writes(entry: CostEntry) -> int:
    """Cache-write tokens whose TTL tier the source did not report."""
    total = entry.cache_creation_input_tokens or 0
    split = (entry.cache_creation_5m_input_tokens or 0) + (
        entry.cache_creation_1h_input_tokens or 0
    )
    return max(0, total - split)


def derive_cost(entry: CostEntry, table: PriceTable | None) -> DerivedCost:
    """Price one entry: reported figure if present, else tokens × rates.

    A reported figure always wins — the provider's own accounting beats
    our arithmetic, and overriding it would make the ledger's most
    trustworthy number the one we mangled.

    Every way this can come up short is reported rather than absorbed:
    no table, a table with no rates for the entry's model, a model label
    absent from the entry entirely, a token class with no rate, and cache
    writes whose TTL tier was never reported.
    """
    if entry.total_cost_usd is not None:
        return DerivedCost(basis="reported", total_cost_usd=entry.total_cost_usd)
    if table is None or not table.is_usable():
        return DerivedCost(
            basis="unpriced",
            gaps=("no usable price table supplied",),
        )

    # Per-model pricing when the split is available: each model's tokens
    # against its own rates. Otherwise the attempt's aggregate tokens
    # against the single model label.
    if entry.models:
        return _derive_from_models(entry, table)
    return _derive_single(entry, table, model=entry.model)


def _derive_single(
    entry: CostEntry, table: PriceTable, *, model: str | None
) -> DerivedCost:
    rates = table.rates_for(model)
    if rates is None:
        reason = (
            "entry reports no model label"
            if model is None
            else f"no rates for model {model!r}"
        )
        return DerivedCost(basis="unpriced", gaps=(reason,))

    total = 0.0
    priced: list[str] = []
    gaps: list[str] = []
    for token_class, tokens in _class_tokens(entry):
        rate = rates.rate_for(token_class)
        if rate is None:
            gaps.append(f"no {token_class} rate for model {model!r} ({tokens} tokens)")
            continue
        total += tokens * rate / TOKENS_PER_MTOK
        priced.append(token_class)

    unsplit = _unsplit_cache_writes(entry)
    if unsplit:
        gaps.append(
            f"{unsplit} cache-write tokens have no reported TTL tier "
            "(5m vs 1h rates differ); left unpriced"
        )
    if not priced:
        return DerivedCost(basis="unpriced", gaps=tuple(gaps))
    return DerivedCost(
        basis="derived",
        total_cost_usd=total,
        priced_classes=tuple(priced),
        gaps=tuple(gaps),
        # A single-model attempt's whole derived cost belongs to that
        # model exactly — no allocation involved.
        per_model={model: total} if model is not None else {},
    )


def _derive_from_models(entry: CostEntry, table: PriceTable) -> DerivedCost:
    """Price a multi-model attempt, one model's slice at a time."""
    total = 0.0
    priced: list[str] = []
    gaps: list[str] = []
    per_model: dict[str, float] = {}
    for usage in entry.models:
        rates = table.rates_for(usage.model)
        if rates is None:
            gaps.append(f"no rates for model {usage.model!r}")
            continue
        for token_class, tokens in (
            ("input", usage.input_tokens),
            ("output", usage.output_tokens),
            ("cache_read", usage.cache_read_input_tokens),
        ):
            if not tokens:
                continue
            rate = rates.rate_for(token_class)
            if rate is None:
                gaps.append(
                    f"no {token_class} rate for model {usage.model!r} "
                    f"({tokens} tokens)"
                )
                continue
            amount = tokens * rate / TOKENS_PER_MTOK
            total += amount
            per_model[usage.model] = per_model.get(usage.model, 0.0) + amount
            priced.append(f"{usage.model}:{token_class}")
        # Per-model cache writes carry no TTL split — the provider
        # reports the tiers only in the attempt-level aggregate — so
        # they are named as a gap rather than priced at a guessed tier.
        if usage.cache_creation_input_tokens:
            gaps.append(
                f"{usage.cache_creation_input_tokens} cache-write tokens for "
                f"model {usage.model!r} have no per-model TTL split; left unpriced"
            )
    if not priced:
        return DerivedCost(basis="unpriced", gaps=tuple(gaps))
    return DerivedCost(
        basis="derived",
        total_cost_usd=total,
        priced_classes=tuple(priced),
        gaps=tuple(gaps),
        per_model=per_model,
    )
