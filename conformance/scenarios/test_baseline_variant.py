"""Baseline variant (wire projection) — chapter 02 §9.4, chapter 06 §2.

The wire-observable surface of the ``kind == "baseline"`` variant
(issue #122): ``kind`` round-trips through create/read/list; a baseline
is never integratable; override-path metrics validate against
``evaluation_schema``; ``idea_id`` is conditionally required; and
creating a baseline requires the ``orchestrators`` group. The
default-path auto-dispatch of a baseline evaluation task is an
orchestrator-role decision (not a task-store wire MUST) and is covered by
reference-impl orchestrator e2e tests, not here (chapter 09 §5 group
entry).
"""

from __future__ import annotations

from typing import Any

import pytest
from conformance.harness import _seed
from conformance.harness.event_cursor import EventLog
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = 'Baseline variant'

_SEED = "a" * 40
_NOW = "2026-05-01T00:00:00Z"
_ORCH = "orchestrator-actor"  # member of the `orchestrators` group


def _baseline_body(
    *,
    variant_id: str,
    experiment_id: str,
    status: str = "starting",
    evaluation: dict[str, Any] | None = None,
    completed_at: str | None = None,
    with_idea_id: bool = False,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "variant_id": variant_id,
        "experiment_id": experiment_id,
        "kind": "baseline",
        "status": status,
        "parent_commits": [_SEED],
        "commit_sha": _SEED,
        "started_at": _NOW,
    }
    if evaluation is not None:
        body["evaluation"] = evaluation
    if completed_at is not None:
        body["completed_at"] = completed_at
    if with_idea_id:
        body["idea_id"] = "idea-x"
    return body


def test_kind_round_trips(wire_client: WireClient) -> None:
    """spec/v0/02-data-model.md §9.4 — `kind` round-trips through the wire.

    §9.4 makes ``kind == "baseline"`` a first-class variant classifier; the
    chapter-7 §4 binding maps the variant JSON to ``variant.schema.json``
    verbatim, so a created baseline's ``kind`` MUST be observable on both
    ``read_variant`` and ``list_variants``.
    """
    vid = _seed.fresh_variant_id("baseline")
    body = _baseline_body(variant_id=vid, experiment_id=wire_client.experiment_id)
    resp = wire_client.post(wire_client.variants_path(), json=body, as_worker=_ORCH)
    assert resp.status_code == 200, resp.text

    read = _seed.read_variant(wire_client, vid)
    assert read["kind"] == "baseline"
    assert "idea_id" not in read or read.get("idea_id") is None

    listed = wire_client.get(wire_client.variants_path())
    listed.raise_for_status()
    match = [v for v in listed.json() if v["variant_id"] == vid]
    assert match and match[0]["kind"] == "baseline"


def test_baseline_not_integratable(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/06-integrator.md §2 — a baseline MUST NOT be integrated.

    §2 says a conforming integrator MUST NOT integrate a
    ``kind == "baseline"`` variant. Wire projection: ``integrate_variant``
    on a baseline in ``success`` returns 409 ``invalid-precondition``,
    writes no ``variant_commit_sha``, and emits no ``variant.integrated``.
    """
    vid = _seed.fresh_variant_id("baseline")
    body = _baseline_body(
        variant_id=vid,
        experiment_id=wire_client.experiment_id,
        status="success",
        evaluation={"score": 0.5},
        completed_at=_NOW,
    )
    wire_client.post(
        wire_client.variants_path(), json=body, as_worker=_ORCH
    ).raise_for_status()

    r = _seed.integrate_variant(wire_client, vid, variant_commit_sha="b" * 40)
    assert r.status_code == 409, r.text
    assert r.json().get("type") == "eden://error/invalid-precondition"

    read = _seed.read_variant(wire_client, vid)
    assert read.get("variant_commit_sha") is None
    integrated = [
        e
        for e in event_log.find_by_type(
            event_log.replay_all(), "variant.integrated"
        )
        if e["data"].get("variant_id") == vid
    ]
    assert integrated == []


def test_override_metrics_validation(wire_client: WireClient) -> None:
    """spec/v0/02-data-model.md §9.2 — baseline override metrics MUST validate.

    §9.2: every evaluation payload persisted on a variant MUST validate
    against the experiment's ``evaluation_schema``. The override path
    (direct ``success`` create) applies this at create time — bad metrics
    are rejected, good metrics accepted.
    """
    bad = _baseline_body(
        variant_id=_seed.fresh_variant_id("baseline"),
        experiment_id=wire_client.experiment_id,
        status="success",
        evaluation={"not_a_declared_metric": 1.0},
        completed_at=_NOW,
    )
    r_bad = wire_client.post(wire_client.variants_path(), json=bad, as_worker=_ORCH)
    # Metrics that don't match evaluation_schema are a semantic
    # precondition violation, mapped to 409 invalid-precondition (the same
    # mapping evaluation-submission metric rejection uses).
    assert r_bad.status_code == 409, r_bad.text
    assert r_bad.json().get("type") == "eden://error/invalid-precondition"

    good = _baseline_body(
        variant_id=_seed.fresh_variant_id("baseline"),
        experiment_id=wire_client.experiment_id,
        status="success",
        evaluation={"score": 0.9},
        completed_at=_NOW,
    )
    r_good = wire_client.post(wire_client.variants_path(), json=good, as_worker=_ORCH)
    assert r_good.status_code == 200, r_good.text


def test_idea_id_conditionally_required(wire_client: WireClient) -> None:
    """spec/v0/02-data-model.md §9.4 — idea_id required unless kind=='baseline'.

    §9.4 / §10 invariant 2: every variant MUST carry ``idea_id`` except a
    ``kind == "baseline"`` one. Wire projection: ``create_variant`` without
    ``idea_id`` is rejected for an ordinary variant but accepted for a
    baseline.
    """
    # Ordinary variant (kind absent) without idea_id → rejected.
    ordinary = {
        "variant_id": _seed.fresh_variant_id("v"),
        "experiment_id": wire_client.experiment_id,
        "status": "starting",
        "parent_commits": [_SEED],
        "started_at": _NOW,
    }
    r_ord = wire_client.post(
        wire_client.variants_path(), json=ordinary, as_worker="impl-worker"
    )
    assert r_ord.status_code == 400, r_ord.text

    # Baseline without idea_id → accepted.
    baseline = _baseline_body(
        variant_id=_seed.fresh_variant_id("baseline"),
        experiment_id=wire_client.experiment_id,
    )
    r_base = wire_client.post(
        wire_client.variants_path(), json=baseline, as_worker=_ORCH
    )
    assert r_base.status_code == 200, r_base.text


def test_per_kind_create_authority(wire_client: WireClient) -> None:
    """spec/v0/07-wire-protocol.md §4 — baseline create requires orchestrators.

    §4: creating a ``kind == "baseline"`` variant requires the caller be in
    the ``orchestrators`` group (an ordinary worker MUST be rejected),
    closing the privilege hole where any worker could fabricate a passing
    baseline. An ordinary variant create stays worker-authenticated.
    """
    # Plain worker (not in orchestrators) → 403 forbidden.
    body = _baseline_body(
        variant_id=_seed.fresh_variant_id("baseline"),
        experiment_id=wire_client.experiment_id,
    )
    r_worker = wire_client.post(
        wire_client.variants_path(), json=body, as_worker="impl-worker"
    )
    assert r_worker.status_code == 403, r_worker.text
    assert r_worker.json().get("type") == "eden://error/forbidden"

    # Orchestrators-group member → accepted.
    body2 = _baseline_body(
        variant_id=_seed.fresh_variant_id("baseline"),
        experiment_id=wire_client.experiment_id,
    )
    r_orch = wire_client.post(
        wire_client.variants_path(), json=body2, as_worker=_ORCH
    )
    assert r_orch.status_code == 200, r_orch.text
