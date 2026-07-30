"""Cost extraction from worker outcome files and agent logs (issue #343).

A reference worker host never talks to an LLM itself — the user's
`*_command` does, so only the user's process knows what the attempt
cost. Two ways for it to say so, both optional keys on the outcome JSON
the host already reads (the worker-host subprocess binding,
``spec/v0/reference-bindings/worker-host-subprocess.md`` §3, §4):

- ``agent_log`` — a path to a Claude Code ``--output-format stream-json``
  log. The final ``{"type": "result"}`` line already carries
  ``total_cost_usd`` and a ``usage`` breakdown; the host parses it, so
  user code adds one key naming a file it already writes.
- ``cost`` — already-normalized figures, for user code driving a
  non-Claude provider (a gateway that returns its own ``usage``).

Both funnel into the same :class:`CostFields`, which the caller stamps
with the identifiers it owns.

**Every failure here is a no-op, never an error.** Cost is bookkeeping
about an attempt; a missing / truncated / malformed log MUST NOT fail an
otherwise-good variant. Callers get ``None`` and log it.

Only the aggregate ``usage`` totals are read. Per-model splits (the
``modelUsage`` map) are collapsed to a single ``model`` label when the
run used exactly one model and dropped otherwise — cost attribution is
per attempt, not per model.
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eden_storage import CostEntry, CostLedger, CostRole, CostSource, cost_entry_id

log = logging.getLogger(__name__)

MAX_AGENT_LOG_TAIL_BYTES = 8 * 1024 * 1024
"""How much of an agent log's tail to scan for the ``result`` line.

Claude Code emits ``result`` last, so scanning the tail is sufficient
and bounds the read — an agent log grows with tool output and can reach
hundreds of MB, which a worker host must not pull into memory. Files at
or under the cap are read whole.
"""


@dataclass(frozen=True)
class CostFields:
    """Normalized cost figures, before identifier attribution.

    Every field is optional: sources differ in what they report, and a
    partially-populated record beats a dropped one. ``source`` records
    which extraction path produced it, so a rollup can tell parsed
    figures from worker-asserted ones.
    """

    source: CostSource
    model: str | None = None
    total_cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    num_turns: int | None = None
    duration_ms: int | None = None

    def is_empty(self) -> bool:
        """True when no figure was recovered (nothing worth recording)."""
        return all(
            getattr(self, name) is None
            for name in (
                "total_cost_usd",
                "input_tokens",
                "output_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
                "num_turns",
                "duration_ms",
            )
        )


def cost_from_outcome(
    outcome: dict[str, Any], *, base_dir: Path, task_id: str
) -> CostFields | None:
    """Extract cost from a worker outcome dict, or ``None`` if absent.

    ``cost`` wins over ``agent_log`` when both are present: an explicit
    report is the user's own accounting, and re-deriving it from a log
    the host doesn't own would silently override them.

    ``base_dir`` resolves a relative ``agent_log`` (the per-task worktree
    for executor / evaluator hosts, matching how ``EDEN_OUTPUT`` itself
    is resolved). ``task_id`` appears only in log context.
    """
    reported = outcome.get("cost")
    if isinstance(reported, dict):
        fields = cost_from_reported(reported)
        if fields is not None:
            return fields
        log.warning(
            "agent_cost_reported_unusable",
            extra={"task_id": task_id},
        )
        return None

    agent_log = outcome.get("agent_log")
    if isinstance(agent_log, str) and agent_log:
        path = Path(agent_log)
        if not path.is_absolute():
            path = base_dir / path
        return cost_from_agent_log(path, task_id=task_id)
    return None


def cost_from_reported(reported: dict[str, Any]) -> CostFields | None:
    """Normalize a worker-reported ``cost`` object.

    Unknown keys are ignored and wrong-typed values are dropped
    field-by-field, so a partially-malformed report still yields the
    fields that were well-formed. Returns ``None`` when nothing usable
    survives.
    """
    fields = CostFields(
        source="worker-reported",
        model=_as_str(reported.get("model")),
        total_cost_usd=_as_float(reported.get("total_cost_usd")),
        input_tokens=_as_int(reported.get("input_tokens")),
        output_tokens=_as_int(reported.get("output_tokens")),
        cache_creation_input_tokens=_as_int(
            reported.get("cache_creation_input_tokens")
        ),
        cache_read_input_tokens=_as_int(reported.get("cache_read_input_tokens")),
        num_turns=_as_int(reported.get("num_turns")),
        duration_ms=_as_int(reported.get("duration_ms")),
    )
    return None if fields.is_empty() else fields


def cost_from_agent_log(path: Path, *, task_id: str = "") -> CostFields | None:
    """Parse the last ``result`` record out of a Claude Code stream-json log.

    Tolerates everything a real log throws at a parser: interleaved
    stderr (the reference `execution.py` merges the two streams), a
    partial final line from a killed agent, non-`result` record types,
    and a truncated head when the file exceeds
    :data:`MAX_AGENT_LOG_TAIL_BYTES`. Returns ``None`` when no complete
    ``result`` record is present — including the deadline-kill case,
    where the agent spent money the log never summarizes (see the
    module docstring on why that is a no-op rather than an error).
    """
    record = _last_result_record(path, task_id=task_id)
    if record is None:
        return None
    usage = record.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    fields = CostFields(
        source="claude-code-stream-json",
        model=_sole_model(record),
        total_cost_usd=_as_float(record.get("total_cost_usd")),
        input_tokens=_as_int(usage.get("input_tokens")),
        output_tokens=_as_int(usage.get("output_tokens")),
        cache_creation_input_tokens=_as_int(
            usage.get("cache_creation_input_tokens")
        ),
        cache_read_input_tokens=_as_int(usage.get("cache_read_input_tokens")),
        num_turns=_as_int(record.get("num_turns")),
        duration_ms=_as_int(record.get("duration_ms")),
    )
    return None if fields.is_empty() else fields


def _last_result_record(path: Path, *, task_id: str) -> dict[str, Any] | None:
    """Return the last well-formed ``type == "result"`` object, or ``None``."""
    truncated = False
    try:
        with path.open("rb") as fp:
            size = os.fstat(fp.fileno()).st_size
            truncated = size > MAX_AGENT_LOG_TAIL_BYTES
            if truncated:
                fp.seek(size - MAX_AGENT_LOG_TAIL_BYTES)
                # The seek lands mid-line; that partial line is not a
                # complete JSON record, so drop it rather than logging a
                # parse failure for it.
                fp.readline()
            found: dict[str, Any] | None = None
            for raw in fp:
                obj = _parse_record(raw)
                if obj is not None and obj.get("type") == "result":
                    found = obj
    except OSError as exc:
        log.warning(
            "agent_cost_log_unreadable",
            extra={"task_id": task_id, "path": str(path), "error": str(exc)},
        )
        return None
    if found is None:
        log.info(
            "agent_cost_no_result_record",
            extra={
                "task_id": task_id,
                "path": str(path),
                "truncated_head": truncated,
            },
        )
    return found


def _parse_record(raw: bytes) -> dict[str, Any] | None:
    """Decode one log line into a dict, or ``None`` if it is not one.

    Non-JSON lines are expected, not exceptional: the reference
    `execution.py` points the agent's stderr at the same file.
    """
    try:
        obj = json.loads(raw.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


def _sole_model(record: dict[str, Any]) -> str | None:
    """Return the model label when the run used exactly one, else ``None``.

    ``modelUsage`` is keyed by model id. More than one key means the run
    spanned models and no single label is honest; zero means the field
    was absent, in which case the ``system``/``init`` line's ``model``
    would be the fallback — but that line lives at the head of the log,
    which the tail scan may have dropped, so it is deliberately not
    consulted.
    """
    model_usage = record.get("modelUsage")
    if not isinstance(model_usage, dict) or len(model_usage) != 1:
        return None
    (name,) = model_usage
    return _as_str(name)


def record_outcome_cost(
    *,
    store: CostLedger,
    outcome: dict[str, Any],
    base_dir: Path,
    role: CostRole,
    task_id: str,
    attempt_key: str,
    variant_id: str | None = None,
    idea_id: str | None = None,
) -> CostEntry | None:
    """Extract cost from ``outcome`` and record it; ``None`` if nothing to record.

    The one call site shape shared by every worker host: read the
    optional cost keys, stamp the identifiers the host owns, write the
    ledger row. Returns the recorded entry (for tests / logging).

    Callers invoke this **before** submitting, and MUST call it while the
    per-task worktree still exists — a relative ``agent_log`` resolves
    against ``base_dir``, which for executor / evaluator hosts is a
    worktree that gets removed at task end.

    Never raises: neither a malformed log nor an unreachable ledger may
    fail an attempt that otherwise succeeded. ``attempt_key`` MUST be
    per-attempt so a reclaimed-and-rerun task records both spends (see
    :func:`eden_storage.cost_entry_id`).
    """
    try:
        fields = cost_from_outcome(outcome, base_dir=base_dir, task_id=task_id)
        if fields is None:
            return None
        entry = CostEntry(
            entry_id=cost_entry_id(role=role, attempt_key=attempt_key),
            experiment_id=store.experiment_id,
            role=role,
            source=fields.source,
            task_id=task_id,
            variant_id=variant_id,
            idea_id=idea_id,
            model=fields.model,
            total_cost_usd=fields.total_cost_usd,
            input_tokens=fields.input_tokens,
            output_tokens=fields.output_tokens,
            cache_creation_input_tokens=fields.cache_creation_input_tokens,
            cache_read_input_tokens=fields.cache_read_input_tokens,
            num_turns=fields.num_turns,
            duration_ms=fields.duration_ms,
        )
        store.record_cost(entry)
    except Exception:  # noqa: BLE001 — cost is bookkeeping, never fatal
        log.warning(
            "agent_cost_record_failed",
            exc_info=True,
            extra={"task_id": task_id, "role": role},
        )
        return None
    log.info(
        "agent_cost_recorded",
        extra={
            "task_id": task_id,
            "role": role,
            "variant_id": variant_id,
            "source": fields.source,
            "total_cost_usd": fields.total_cost_usd,
        },
    )
    return entry


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _as_float(value: Any) -> float | None:
    # bool is an int subclass; a JSON `true` here is malformed, not 1.0.
    # `json.loads` accepts the non-standard NaN / Infinity literals, and
    # a NaN would survive a `< 0` test only to trip the CostEntry
    # `ge=0.0` constraint downstream — where a raise would violate this
    # module's no-op contract. Reject non-finite here instead.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return float(value)


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return None if value < 0 else value
