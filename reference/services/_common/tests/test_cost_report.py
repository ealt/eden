"""Tests for the per-experiment cost report (issue #343 milestone 3).

The report is what makes the ledger answer the question #343 was filed
for — "what did this run cost, per role and per variant?" — so the tests
that matter are the ones about *honest* accounting: partial figures must
not read as complete ones, ideation spend must not vanish from the
totals just because it predates any variant, and spend attributed to a
variant the store no longer has must still appear.

Driven end-to-end through a real ``StoreClient`` against a real app, so
the wire shaping (``exclude_none``, filter forwarding) is exercised
rather than mocked.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from eden_service_common.cost_report import (
    build_report,
    render_table,
    resolve_bearer,
)
from eden_storage import CostEntry, InMemoryStore, summarize
from eden_wire import StoreClient, make_app
from fastapi.testclient import TestClient

EXPERIMENT_ID = "exp_zp0q3v6xsnk0jf9hfb54m73626"


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore(experiment_id=EXPERIMENT_ID)


@pytest.fixture
def client(store: InMemoryStore) -> StoreClient:
    app_client = TestClient(make_app(store), base_url="http://wire.test")
    return StoreClient("http://wire.test", EXPERIMENT_ID, client=app_client)


def _record(
    store: InMemoryStore, entry_id: str, **overrides: Any
) -> None:
    fields: dict[str, Any] = {
        "entry_id": entry_id,
        "experiment_id": EXPERIMENT_ID,
        "role": "executor",
        "source": "claude-code-stream-json",
        "task_id": f"task-{entry_id}",
        "total_cost_usd": 0.1,
        "input_tokens": 10,
        "output_tokens": 20,
    }
    fields.update(overrides)
    store.record_cost(CostEntry.model_validate(fields))


def _report(client: StoreClient, **kwargs: Any) -> dict[str, Any]:
    return build_report(
        client=client,
        experiment_id=EXPERIMENT_ID,
        role=kwargs.get("role"),
        variant_id=kwargs.get("variant_id"),
    )


# ----------------------------------------------------------------------
# summarize() — the reduction
# ----------------------------------------------------------------------


def test_summarize_partitions_by_role_and_variant() -> None:
    entries = [
        CostEntry(
            entry_id="e1",
            experiment_id=EXPERIMENT_ID,
            role="ideator",
            source="worker-reported",
            task_id="ideation-1",
            total_cost_usd=0.2,
            input_tokens=100,
        ),
        CostEntry(
            entry_id="e2",
            experiment_id=EXPERIMENT_ID,
            role="executor",
            source="claude-code-stream-json",
            task_id="execution-1",
            variant_id="v1",
            total_cost_usd=0.5,
        ),
        CostEntry(
            entry_id="e3",
            experiment_id=EXPERIMENT_ID,
            role="evaluator",
            source="claude-code-stream-json",
            task_id="evaluate-1",
            variant_id="v1",
            total_cost_usd=0.05,
        ),
    ]
    summary = summarize(EXPERIMENT_ID, entries)

    assert summary.totals.entries == 3
    assert summary.totals.total_cost_usd == pytest.approx(0.75)
    assert set(summary.by_role) == {"ideator", "executor", "evaluator"}
    assert summary.by_role["executor"].total_cost_usd == pytest.approx(0.5)
    # Ideation spend has no variant: it is in totals + by_role, and in
    # no by_variant bucket.
    assert set(summary.by_variant) == {"v1"}
    assert summary.by_variant["v1"].total_cost_usd == pytest.approx(0.55)
    assert sum(
        t.total_cost_usd for t in summary.by_role.values()
    ) == pytest.approx(summary.totals.total_cost_usd)


def test_summarize_counts_entries_missing_a_dollar_figure() -> None:
    """A token-only entry must not make a partial total look complete."""
    entries = [
        CostEntry(
            entry_id="e1",
            experiment_id=EXPERIMENT_ID,
            role="ideator",
            source="worker-reported",
            task_id="ideation-1",
            input_tokens=100,
            output_tokens=20,
        ),
        CostEntry(
            entry_id="e2",
            experiment_id=EXPERIMENT_ID,
            role="executor",
            source="claude-code-stream-json",
            task_id="execution-1",
            variant_id="v1",
            total_cost_usd=0.5,
        ),
    ]
    summary = summarize(EXPERIMENT_ID, entries)
    assert summary.totals.total_cost_usd == pytest.approx(0.5)
    assert summary.totals.entries_missing_cost_usd == 1
    assert summary.by_role["ideator"].entries_missing_cost_usd == 1
    assert summary.by_role["ideator"].input_tokens == 100
    assert summary.by_role["executor"].entries_missing_cost_usd == 0


def test_summarize_of_an_empty_ledger_is_zeroed_not_absent() -> None:
    summary = summarize(EXPERIMENT_ID, [])
    assert summary.totals.entries == 0
    assert summary.totals.total_cost_usd == 0.0
    assert summary.by_role == {}
    assert summary.by_variant == {}


# ----------------------------------------------------------------------
# build_report() — reduction + the per-variant join
# ----------------------------------------------------------------------


def test_report_joins_variant_status_and_evaluation(
    store: InMemoryStore, client: StoreClient
) -> None:
    """Per-variant rows carry the evaluation payload, so DCI-per-dollar is local.

    Without the join a consumer has to correlate two endpoints itself,
    which is the friction this milestone exists to remove.
    """
    from eden_contracts import Idea, Variant

    store.create_idea(
        Idea(
            idea_id="idea-1",
            experiment_id=EXPERIMENT_ID,
            slug="p0",
            priority=1.0,
            parent_commits=["a" * 40],
            artifacts_uri="file:///tmp/eden-test/ideas/idea-1/content.md",
            state="drafting",
            created_at="2026-07-01T00:00:00.000Z",
        )
    )
    store.create_variant(
        Variant(
            variant_id="v1",
            experiment_id=EXPERIMENT_ID,
            idea_id="idea-1",
            status="starting",
            parent_commits=["a" * 40],
            started_at="2026-07-01T00:00:00.000Z",
        )
    )
    _record(store, "e1", variant_id="v1", total_cost_usd=0.5)

    report = _report(client)
    (row,) = report["by_variant"]
    assert row["variant_id"] == "v1"
    assert row["status"] == "starting"
    assert row["idea_id"] == "idea-1"
    assert row["evaluation"] is None
    assert row["cost"]["total_cost_usd"] == pytest.approx(0.5)


def test_report_keeps_spend_on_an_unknown_variant(
    store: InMemoryStore, client: StoreClient
) -> None:
    """Spend attributed to a variant the store lacks is reported, not dropped.

    The money was spent regardless of what happened to the variant
    record; dropping the row would understate the run's cost, which is
    the one thing this report must not do.
    """
    _record(store, "e1", variant_id="ghost", total_cost_usd=0.5)

    report = _report(client)
    (row,) = report["by_variant"]
    assert row["variant_id"] == "ghost"
    assert row["status"] is None
    assert row["evaluation"] is None
    assert report["totals"]["total_cost_usd"] == pytest.approx(0.5)


def test_report_forwards_filters(
    store: InMemoryStore, client: StoreClient
) -> None:
    _record(store, "e1", role="executor", variant_id="v1")
    _record(store, "e2", role="evaluator", variant_id="v1")
    _record(store, "e3", role="executor", variant_id="v2")

    everything = _report(client)
    assert everything["totals"]["entries"] == 3

    by_role = _report(client, role="executor")
    assert by_role["filters"]["role"] == "executor"
    assert by_role["totals"]["entries"] == 2
    assert set(by_role["by_role"]) == {"executor"}

    by_variant = _report(client, variant_id="v1")
    assert by_variant["totals"]["entries"] == 2
    assert [r["variant_id"] for r in by_variant["by_variant"]] == ["v1"]


def test_report_is_json_serializable_and_stable(
    store: InMemoryStore, client: StoreClient
) -> None:
    """The JSON form is the machine contract, so it must round-trip and sort."""
    _record(store, "e2", variant_id="v2")
    _record(store, "e1", variant_id="v1")

    report = _report(client)
    round_tripped = json.loads(json.dumps(report, sort_keys=True))
    assert round_tripped == report
    assert [r["variant_id"] for r in report["by_variant"]] == ["v1", "v2"]
    assert {e["entry_id"] for e in report["entries"]} == {"e1", "e2"}


def test_report_on_empty_ledger_has_the_full_shape(
    client: StoreClient,
) -> None:
    """A run that recorded nothing still produces a well-formed report."""
    report = _report(client)
    assert report["totals"]["entries"] == 0
    assert report["by_role"] == {}
    assert report["by_variant"] == []
    assert report["entries"] == []


# ----------------------------------------------------------------------
# Rendering + auth resolution
# ----------------------------------------------------------------------


def test_table_render_flags_incomplete_totals(
    store: InMemoryStore, client: StoreClient
) -> None:
    _record(store, "e1", variant_id="v1", total_cost_usd=None)
    text = render_table(_report(client))
    assert "no dollar figure" in text
    assert "v1" in text


def test_table_render_omits_the_caveat_when_complete(
    store: InMemoryStore, client: StoreClient
) -> None:
    _record(store, "e1", variant_id="v1", total_cost_usd=0.25)
    text = render_table(_report(client))
    assert "no dollar figure" not in text
    assert "$0.2500" in text


def test_bearer_comes_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auth never rides argv — it would land in history and every ps listing."""
    monkeypatch.delenv("EDEN_BEARER", raising=False)
    monkeypatch.delenv("EDEN_ADMIN_TOKEN", raising=False)
    assert resolve_bearer() is None

    monkeypatch.setenv("EDEN_ADMIN_TOKEN", "secret")
    assert resolve_bearer() == "admin:secret"

    monkeypatch.setenv("EDEN_BEARER", "wkr_abc:othersecret")
    assert resolve_bearer() == "wkr_abc:othersecret"
