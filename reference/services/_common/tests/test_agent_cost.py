"""Tests for cost extraction + ledger recording (issue #343).

The happy path runs against
[`fixtures/claude-agent-log-success.jsonl`](fixtures/claude-agent-log-success.jsonl)
— a **real** Claude Code ``--output-format stream-json`` capture from an
eden-experiments belief-state-recovery execution task, reduced to one
line per record type with prose / session ids / hook output redacted.
Every number in the ``result`` record is verbatim from the capture, so
these assertions pin the real field names (``total_cost_usd``, the
``usage`` sub-keys, ``modelUsage``) rather than a shape we invented.

The degradation cases are synthesized, because the whole point is
behavior on logs a real run produces but a fixture can't be harvested
for reliably: a deadline-killed agent (no ``result`` line), interleaved
stderr, a truncated tail.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from eden_service_common import (
    cost_from_agent_log,
    cost_from_outcome,
    cost_from_reported,
    record_outcome_cost,
)
from eden_service_common.agent_cost import MAX_AGENT_LOG_TAIL_BYTES
from eden_storage import InMemoryStore

FIXTURE = Path(__file__).parent / "fixtures" / "claude-agent-log-success.jsonl"


def _result_line(**overrides: object) -> str:
    record: dict[str, object] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 25131,
        "num_turns": 5,
        "total_cost_usd": 0.136,
        "usage": {
            "input_tokens": 7,
            "output_tokens": 1186,
            "cache_creation_input_tokens": 16592,
            "cache_read_input_tokens": 62199,
        },
        "modelUsage": {"claude-sonnet-4-6": {"costUSD": 0.136}},
    }
    record.update(overrides)
    return json.dumps(record)


# ----------------------------------------------------------------------
# Real-capture happy path
# ----------------------------------------------------------------------


def test_parses_real_capture() -> None:
    """Every figure comes out of the real stream-json capture."""
    fields = cost_from_agent_log(FIXTURE, task_id="execution-1")
    assert fields is not None
    assert fields.source == "claude-code-stream-json"
    assert fields.total_cost_usd == pytest.approx(0.1233009)
    assert fields.input_tokens == 6
    assert fields.output_tokens == 637
    assert fields.cache_creation_input_tokens == 16584
    assert fields.cache_read_input_tokens == 47413
    assert fields.num_turns == 4
    assert fields.duration_ms == 18118
    assert fields.model == "claude-sonnet-4-6"


def test_ignores_non_result_records() -> None:
    """The fixture's system / assistant / rate-limit lines carry no cost.

    A parser that took the first record with a ``usage`` key, or that
    summed per-turn usage, would disagree with the ``result`` totals.
    """
    fields = cost_from_agent_log(FIXTURE)
    assert fields is not None
    # `system:init` in the fixture declares a model; the figure below
    # comes from the result record's aggregate usage, not a turn's.
    assert fields.input_tokens == 6


# ----------------------------------------------------------------------
# Degradation — every one of these is a no-op, never a raise
# ----------------------------------------------------------------------


def test_missing_file_returns_none(tmp_path: Path) -> None:
    assert cost_from_agent_log(tmp_path / "nope.log", task_id="t") is None


def test_directory_instead_of_file_returns_none(tmp_path: Path) -> None:
    assert cost_from_agent_log(tmp_path, task_id="t") is None


def test_no_result_record_returns_none(tmp_path: Path) -> None:
    """A deadline-killed agent's log has turns but no ``result``."""
    log = tmp_path / "killed.log"
    log.write_text(
        '{"type": "system", "subtype": "init", "model": "x"}\n'
        '{"type": "assistant", "message": "..."}\n',
        encoding="utf-8",
    )
    assert cost_from_agent_log(log, task_id="t") is None


def test_truncated_final_line_returns_none(tmp_path: Path) -> None:
    """A SIGKILL mid-write leaves a partial JSON line, not a crash."""
    log = tmp_path / "partial.log"
    log.write_text(_result_line()[:60], encoding="utf-8")
    assert cost_from_agent_log(log, task_id="t") is None


def test_interleaved_stderr_is_tolerated(tmp_path: Path) -> None:
    """The reference `execution.py` merges the agent's stderr into the log."""
    log = tmp_path / "mixed.log"
    log.write_text(
        "Traceback (most recent call last):\n"
        "  File \"x.py\", line 1\n"
        "some-hook: warning: not json at all\n"
        f"{_result_line()}\n"
        "post-result stderr noise\n",
        encoding="utf-8",
    )
    fields = cost_from_agent_log(log, task_id="t")
    assert fields is not None
    assert fields.total_cost_usd == pytest.approx(0.136)


def test_last_result_record_wins(tmp_path: Path) -> None:
    """Two result records (a resumed session) resolve to the later one."""
    log = tmp_path / "two.log"
    log.write_text(
        f"{_result_line(total_cost_usd=0.1)}\n"
        f"{_result_line(total_cost_usd=0.9)}\n",
        encoding="utf-8",
    )
    fields = cost_from_agent_log(log)
    assert fields is not None
    assert fields.total_cost_usd == pytest.approx(0.9)


def test_result_beyond_tail_cap_is_still_found(tmp_path: Path) -> None:
    """A log larger than the tail cap still yields its trailing result.

    Agent logs grow with tool output; the host reads a bounded tail
    rather than the whole file, and the ``result`` record is last.
    """
    log = tmp_path / "big.log"
    filler = json.dumps({"type": "assistant", "message": "x" * 900}) + "\n"
    with log.open("w", encoding="utf-8") as fp:
        written = 0
        while written < MAX_AGENT_LOG_TAIL_BYTES + 4096:
            written += fp.write(filler)
        fp.write(_result_line() + "\n")
    assert log.stat().st_size > MAX_AGENT_LOG_TAIL_BYTES

    fields = cost_from_agent_log(log, task_id="t")
    assert fields is not None
    assert fields.total_cost_usd == pytest.approx(0.136)


def test_result_before_tail_cap_is_dropped(tmp_path: Path) -> None:
    """A result buried before the tail window is not recovered.

    Documents the bound rather than pretending it doesn't exist: the
    host trades unbounded reads for the (Claude-Code-guaranteed)
    result-is-last convention.
    """
    log = tmp_path / "buried.log"
    filler = json.dumps({"type": "assistant", "message": "x" * 900}) + "\n"
    with log.open("w", encoding="utf-8") as fp:
        fp.write(_result_line() + "\n")
        written = 0
        while written < MAX_AGENT_LOG_TAIL_BYTES + 4096:
            written += fp.write(filler)
    assert cost_from_agent_log(log, task_id="t") is None


@pytest.mark.parametrize(
    "bad",
    [
        {"total_cost_usd": "0.12"},
        {"total_cost_usd": True},
        {"total_cost_usd": -1.0},
        {"total_cost_usd": float("nan")},
        {"total_cost_usd": float("inf")},
    ],
)
def test_wrong_typed_cost_is_dropped_field_wise(
    tmp_path: Path, bad: dict[str, object]
) -> None:
    """A malformed figure drops that field; the rest still lands.

    ``NaN`` / ``Infinity`` matter specifically: ``json.loads`` accepts
    both, and a non-finite value would otherwise reach the ledger's
    ``ge=0`` constraint and raise where a no-op was promised.
    """
    log = tmp_path / "bad.log"
    log.write_text(_result_line(**bad) + "\n", encoding="utf-8")
    fields = cost_from_agent_log(log, task_id="t")
    assert fields is not None
    assert fields.total_cost_usd is None
    assert fields.output_tokens == 1186


def test_missing_usage_block_keeps_top_level_figures(tmp_path: Path) -> None:
    log = tmp_path / "nousage.log"
    log.write_text(_result_line(usage="not-an-object") + "\n", encoding="utf-8")
    fields = cost_from_agent_log(log)
    assert fields is not None
    assert fields.input_tokens is None
    assert fields.total_cost_usd == pytest.approx(0.136)


def test_all_figures_unusable_returns_none(tmp_path: Path) -> None:
    """Nothing recoverable means nothing recorded — not an empty row."""
    log = tmp_path / "empty.log"
    log.write_text(
        json.dumps({"type": "result", "subtype": "success"}) + "\n",
        encoding="utf-8",
    )
    assert cost_from_agent_log(log) is None


def test_multi_model_run_reports_no_single_model(tmp_path: Path) -> None:
    """Two models in one run means no honest single ``model`` label."""
    log = tmp_path / "multi.log"
    log.write_text(
        _result_line(
            modelUsage={
                "claude-sonnet-4-6": {"costUSD": 0.1},
                "claude-haiku-4-5": {"costUSD": 0.036},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    fields = cost_from_agent_log(log)
    assert fields is not None
    assert fields.model is None
    assert fields.total_cost_usd == pytest.approx(0.136)


# ----------------------------------------------------------------------
# Worker-reported figures
# ----------------------------------------------------------------------


def test_reported_cost_is_normalized() -> None:
    fields = cost_from_reported(
        {
            "total_cost_usd": 0.4,
            "input_tokens": 10,
            "output_tokens": 20,
            "model": "gateway-model",
            "unknown_extra": "ignored",
        }
    )
    assert fields is not None
    assert fields.source == "worker-reported"
    assert fields.total_cost_usd == pytest.approx(0.4)
    assert fields.model == "gateway-model"


def test_reported_empty_object_returns_none() -> None:
    assert cost_from_reported({}) is None


# ----------------------------------------------------------------------
# Outcome dispatch
# ----------------------------------------------------------------------


def test_outcome_without_cost_keys_returns_none(tmp_path: Path) -> None:
    assert (
        cost_from_outcome(
            {"status": "success", "commit_sha": "a" * 40},
            base_dir=tmp_path,
            task_id="t",
        )
        is None
    )


def test_outcome_relative_agent_log_resolves_against_base_dir(
    tmp_path: Path,
) -> None:
    """A relative path resolves like ``EDEN_OUTPUT`` does — under cwd."""
    (tmp_path / ".eden").mkdir()
    (tmp_path / ".eden" / "agent.log").write_text(
        _result_line() + "\n", encoding="utf-8"
    )
    fields = cost_from_outcome(
        {"status": "success", "agent_log": ".eden/agent.log"},
        base_dir=tmp_path,
        task_id="t",
    )
    assert fields is not None
    assert fields.total_cost_usd == pytest.approx(0.136)


def test_outcome_reported_cost_beats_agent_log(tmp_path: Path) -> None:
    """An explicit report is the user's own accounting; don't override it."""
    log = tmp_path / "a.log"
    log.write_text(_result_line(total_cost_usd=0.9) + "\n", encoding="utf-8")
    fields = cost_from_outcome(
        {"cost": {"total_cost_usd": 0.1}, "agent_log": str(log)},
        base_dir=tmp_path,
        task_id="t",
    )
    assert fields is not None
    assert fields.source == "worker-reported"
    assert fields.total_cost_usd == pytest.approx(0.1)


def test_outcome_non_dict_cost_falls_back_to_agent_log(tmp_path: Path) -> None:
    log = tmp_path / "a.log"
    log.write_text(_result_line() + "\n", encoding="utf-8")
    fields = cost_from_outcome(
        {"cost": "0.12", "agent_log": str(log)},
        base_dir=tmp_path,
        task_id="t",
    )
    assert fields is not None
    assert fields.source == "claude-code-stream-json"


# ----------------------------------------------------------------------
# record_outcome_cost — the host-facing entry point
# ----------------------------------------------------------------------


def _store() -> InMemoryStore:
    return InMemoryStore(experiment_id="exp_01hzzzzzzzzzzzzzzzzzzzzzzz")


def test_record_writes_an_attributed_entry() -> None:
    store = _store()
    entry = record_outcome_cost(
        store=store,
        outcome={"status": "success", "agent_log": str(FIXTURE)},
        base_dir=FIXTURE.parent,
        role="executor",
        task_id="execution-1",
        attempt_key="variant-1",
        variant_id="variant-1",
        idea_id="idea-1",
    )
    assert entry is not None

    (stored,) = store.list_cost_entries()
    assert stored.entry_id == "cost-executor-variant-1"
    assert stored.role == "executor"
    assert stored.task_id == "execution-1"
    assert stored.variant_id == "variant-1"
    assert stored.idea_id == "idea-1"
    assert stored.total_cost_usd == pytest.approx(0.1233009)
    assert stored.recorded_at is not None


def test_record_is_a_no_op_when_there_is_no_cost() -> None:
    store = _store()
    assert (
        record_outcome_cost(
            store=store,
            outcome={"status": "error"},
            base_dir=Path("/nonexistent"),
            role="executor",
            task_id="execution-1",
            attempt_key="variant-1",
        )
        is None
    )
    assert store.list_cost_entries() == []


def test_record_swallows_a_ledger_failure() -> None:
    """An unreachable ledger must not fail an otherwise-good attempt."""

    class _Broken:
        experiment_id = "exp_01hzzzzzzzzzzzzzzzzzzzzzzz"

        def record_cost(self, entry: object) -> None:
            raise RuntimeError("ledger unreachable")

        def list_cost_entries(self, **_kwargs: object) -> list[object]:
            return []

    assert (
        record_outcome_cost(
            store=_Broken(),  # type: ignore[arg-type]
            outcome={"status": "success", "agent_log": str(FIXTURE)},
            base_dir=FIXTURE.parent,
            role="executor",
            task_id="execution-1",
            attempt_key="variant-1",
        )
        is None
    )


def test_record_twice_for_one_attempt_does_not_double_count() -> None:
    store = _store()
    for _ in range(2):
        record_outcome_cost(
            store=store,
            outcome={"cost": {"total_cost_usd": 0.5}},
            base_dir=Path("/tmp"),
            role="executor",
            task_id="execution-1",
            attempt_key="variant-1",
        )
    entries = store.list_cost_entries()
    assert len(entries) == 1
    assert entries[0].total_cost_usd == pytest.approx(0.5)


def test_two_attempts_on_one_task_each_record() -> None:
    """A reclaimed-and-rerun task spent money twice; the ledger says so."""
    store = _store()
    for variant_id in ("variant-1", "variant-2"):
        record_outcome_cost(
            store=store,
            outcome={"cost": {"total_cost_usd": 0.5}},
            base_dir=Path("/tmp"),
            role="executor",
            task_id="execution-1",
            attempt_key=variant_id,
            variant_id=variant_id,
        )
    entries = store.list_cost_entries()
    assert len(entries) == 2
    assert sum(e.total_cost_usd or 0.0 for e in entries) == pytest.approx(1.0)
