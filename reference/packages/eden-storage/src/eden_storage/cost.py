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
  read-time reduction over rows ([`rollup.py`](rollup.py)).
"""

from __future__ import annotations

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


class ModelUsage(BaseModel):
    """One model's slice of an attempt's usage.

    An attempt can span models (a fast model for tool loops, a stronger
    one for the hard turn), and the cheapest thing to do — collapse it
    to a single label — throws away exactly the breakdown that makes
    "which model is the spend" answerable. So the per-model split is
    kept as structure the rollup slices at read time, next to the
    attempt-level totals rather than instead of them.

    Deliberately narrower than :class:`CostEntry`: ``num_turns`` and
    ``duration_ms`` belong to the attempt, not to a model within it, and
    a per-model copy of them would be a fabricated division.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    model: Annotated[str, Field(min_length=1)]
    total_cost_usd: Annotated[float, Field(ge=0.0)] | None = None
    input_tokens: Annotated[int, Field(ge=0)] | None = None
    output_tokens: Annotated[int, Field(ge=0)] | None = None
    cache_creation_input_tokens: Annotated[int, Field(ge=0)] | None = None
    cache_read_input_tokens: Annotated[int, Field(ge=0)] | None = None


class CostEntry(BaseModel):
    """One attributable spend event. Reference-only; not a wire schema.

    Every numeric field is optional: sources differ in what they report,
    and a partially-reported entry is strictly more useful than a
    dropped one. A rollup sums what is present and reports how many
    entries were missing it rather than silently reading a partial total
    as a complete one.

    ``total_cost_usd`` means **as the provider reported it** and is never
    written by a derivation. Dollars computed from tokens are a
    read-time concern (:mod:`eden_storage.pricing`) precisely so a
    stored figure can always be trusted as first-hand.
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
    """Model label, when the attempt used exactly one.

    A convenience for the common case; ``models`` is the general answer
    and is populated whether the attempt used one model or five.
    """

    models: list[ModelUsage] = Field(default_factory=list)
    """Per-model usage split, when the source reports one.

    A ``list``, not a ``tuple``: the entry round-trips through JSON on
    the wire and through ``model_dump`` inside the store, and a JSON
    array deserializes to a list — which ``strict=True`` would refuse to
    coerce into a tuple. (Found the hard way: a tuple here made every
    multi-model entry unrecordable *and* un-POSTable.)
    """

    total_cost_usd: Annotated[float, Field(ge=0.0)] | None = None
    """What the provider charged, as the provider reported it."""

    input_tokens: Annotated[int, Field(ge=0)] | None = None
    output_tokens: Annotated[int, Field(ge=0)] | None = None
    cache_creation_input_tokens: Annotated[int, Field(ge=0)] | None = None
    """All cache writes, both TTL tiers."""

    cache_creation_5m_input_tokens: Annotated[int, Field(ge=0)] | None = None
    """Cache writes at the 5-minute TTL.

    Split out from the aggregate because cache writes are priced **per
    TTL tier** — a 1-hour write costs materially more than a 5-minute
    one — so pricing tokens without the split means picking a tier and
    hoping. Sums with the 1h field to ``cache_creation_input_tokens``
    when the source reports the breakdown at all.
    """

    cache_creation_1h_input_tokens: Annotated[int, Field(ge=0)] | None = None
    """Cache writes at the 1-hour TTL. See the 5m field."""

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


def cost_entry_id(*, role: CostRole, attempt_key: str) -> str:
    """Derive the reference hosts' per-attempt idempotency key.

    ``attempt_key`` MUST identify one *attempt*, not one task. The
    executor host passes its freshly-minted ``variant_id`` (one per
    execution attempt, so a reclaimed-and-rerun task yields two rows);
    the evaluator host passes ``<task_id>:<variant_id>``; the ideator
    host passes its per-dispatch key.
    """
    return f"cost-{role}-{attempt_key}"
