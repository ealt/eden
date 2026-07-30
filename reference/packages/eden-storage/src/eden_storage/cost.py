"""Cost ledger records — **reference-only, non-normative** (issue #343).

`spec/v0` has no home for per-role token / dollar spend: the chapter-3
submission shapes carry no cost field ([`submissions.py`](submissions.py)),
the chapter-2 `Variant` record has no cost property, and the chapter-5
event registry is closed at v0
([`spec/v0/05-event-protocol.md`](../../../../../spec/v0/05-event-protocol.md) §3.6).
Extra keys smuggled onto an execution submission payload are silently
dropped by :func:`eden_storage.submissions.submission_from_payload`, and
extra keys on an *evaluation* payload are rejected outright by the
`evaluation_schema` exact-key-match rule
([`spec/v0/02-data-model.md`](../../../../../spec/v0/02-data-model.md) §9.2).

So the reference impl records cost in its own ledger, reached through the
non-normative `/_reference/` wire surface that
[`spec/v0/07-wire-protocol.md`](../../../../../spec/v0/07-wire-protocol.md) §5
sanctions for implementation extensions. Nothing here is required of a
conforming implementation, and no conformance assertion depends on it.
Giving cost a normative home is scoped on
[issue #343](https://github.com/ealt/eden/issues/343).

Two properties are load-bearing for the ledger's purpose (answering
"what did this experiment cost, per role and per variant?"):

- **Idempotent by `entry_id`.** A worker host may re-record after a
  transport failure, so :meth:`record_cost` is first-write-wins: a
  repeat `entry_id` is a no-op rather than a duplicate row. The caller
  owns the key, and it MUST be per-*attempt*, not per-task — a reclaimed
  task that runs twice spends money twice. :func:`cost_entry_id` derives
  the reference hosts' keys from the per-attempt identifiers.
- **Attribution, not aggregation.** One row per spend event with the
  role / task / variant / idea it is attributable to; every rollup is a
  read-time reduction over rows.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

CostRole = Literal["ideator", "executor", "evaluator"]
"""Which role spent it. Role nouns per [`docs/glossary.md`](../../../../../docs/glossary.md).

The integrator is absent deliberately: it runs in-orchestrator and
consumes no metered inference. A future LLM-driven integrator would add
the fourth value here.
"""

CostSource = Literal["claude-code-stream-json", "worker-reported"]
"""How the numbers were obtained.

- ``claude-code-stream-json`` — the worker host parsed a Claude Code
  ``--output-format stream-json`` agent log
  (:mod:`eden_service_common.agent_cost`).
- ``worker-reported`` — user code reported already-normalized figures
  (e.g. an ideator bridge reading gateway ``usage``).
"""


class CostEntry(BaseModel):
    """One attributable spend event. Reference-only; not a wire schema.

    Every numeric field is optional: sources differ in what they report,
    and a partially-reported entry is strictly more useful than a
    dropped one. A rollup sums what is present and reports how many
    entries were missing it rather than silently reading a partial total
    as a complete one.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    entry_id: Annotated[str, Field(min_length=1, max_length=128)]
    """Caller-supplied idempotency key, unique per spend *attempt*."""

    experiment_id: Annotated[str, Field(min_length=1)]
    role: CostRole
    source: CostSource
    task_id: Annotated[str, Field(min_length=1)]
    variant_id: Annotated[str, Field(min_length=1)] | None = None
    idea_id: Annotated[str, Field(min_length=1)] | None = None

    model: Annotated[str, Field(min_length=1)] | None = None
    """Model label, when the source reports exactly one."""

    total_cost_usd: Annotated[float, Field(ge=0.0)] | None = None
    input_tokens: Annotated[int, Field(ge=0)] | None = None
    output_tokens: Annotated[int, Field(ge=0)] | None = None
    cache_creation_input_tokens: Annotated[int, Field(ge=0)] | None = None
    cache_read_input_tokens: Annotated[int, Field(ge=0)] | None = None
    num_turns: Annotated[int, Field(ge=0)] | None = None
    duration_ms: Annotated[int, Field(ge=0)] | None = None

    recorded_at: str | None = None
    """Store-stamped ISO-8601 timestamp.

    ``None`` on an entry a caller has built but not yet recorded;
    :meth:`eden_storage.CostLedger.record_cost` stamps it from the
    store's clock, so every entry read back from a ledger has it set.
    A caller-supplied value is ignored.
    """

    def to_payload(self) -> dict[str, Any]:
        """JSON-shaped dict with absent optionals omitted."""
        return self.model_dump(mode="json", exclude_none=True)


_TOKEN_FIELDS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "num_turns",
    "duration_ms",
)


class CostTotals(BaseModel):
    """Summed figures over a set of ledger entries.

    ``entries_missing_cost_usd`` is the honesty field: a source that
    reports tokens but no dollar figure would otherwise make
    ``total_cost_usd`` read as a complete total when it is a partial
    one. A consumer that cares about completeness checks it rather than
    inferring completeness from a non-zero sum.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    entries: int = 0
    entries_missing_cost_usd: int = 0
    total_cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    num_turns: int = 0
    duration_ms: int = 0


class CostSummary(BaseModel):
    """Per-experiment cost rollup: overall, per role, per variant.

    A read-time reduction over ledger rows, not stored state — so it can
    never disagree with the ledger. Entries with no ``variant_id``
    (ideation spend, which precedes any variant) count in ``totals`` and
    ``by_role`` but appear in no ``by_variant`` bucket; ``by_role`` is
    the complete partition.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    experiment_id: str
    totals: CostTotals
    by_role: dict[str, CostTotals]
    by_variant: dict[str, CostTotals]


def _accumulate(totals: CostTotals, entry: CostEntry) -> None:
    totals.entries += 1
    if entry.total_cost_usd is None:
        totals.entries_missing_cost_usd += 1
    else:
        totals.total_cost_usd += entry.total_cost_usd
    for field in _TOKEN_FIELDS:
        value = getattr(entry, field)
        if value is not None:
            setattr(totals, field, getattr(totals, field) + value)


def summarize(experiment_id: str, entries: Iterable[CostEntry]) -> CostSummary:
    """Reduce ledger entries into a :class:`CostSummary`.

    A pure function so the same reduction serves an in-process consumer
    and one reading entries over the wire — there is no server-side
    summary endpoint to drift from it.
    """
    summary = CostSummary(
        experiment_id=experiment_id,
        totals=CostTotals(),
        by_role={},
        by_variant={},
    )
    for entry in entries:
        _accumulate(summary.totals, entry)
        _accumulate(summary.by_role.setdefault(entry.role, CostTotals()), entry)
        if entry.variant_id is not None:
            _accumulate(
                summary.by_variant.setdefault(entry.variant_id, CostTotals()), entry
            )
    return summary


def cost_entry_id(*, role: CostRole, attempt_key: str) -> str:
    """Derive the reference hosts' per-attempt idempotency key.

    ``attempt_key`` MUST identify one *attempt*, not one task. The
    executor host passes its freshly-minted ``variant_id`` (one per
    execution attempt, so a reclaimed-and-rerun task yields two rows);
    the evaluator host passes ``<task_id>:<variant_id>``; the ideator
    host passes its per-dispatch key.
    """
    return f"cost-{role}-{attempt_key}"
