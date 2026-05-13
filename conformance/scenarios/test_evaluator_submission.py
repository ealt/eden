"""Evaluator submission semantics — chapter 03 §4.2, §4.4."""

from __future__ import annotations

import pytest
from conformance.harness import _seed
from conformance.harness.event_cursor import EventLog
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = 'Evaluator submission'


def _drive_to_starting_variant(client: WireClient) -> tuple[str, str]:
    """Return (eval_task_id, variant_id) for a freshly-prepared evaluator scenario."""
    variant_id = _seed.drive_to_starting_variant(client)
    eval_tid = _seed.create_evaluation_task(client, variant_id=variant_id)
    return eval_tid, variant_id


def test_submit_with_mismatched_variant_id_rejected(wire_client: WireClient) -> None:
    """spec/v0/03-roles.md §4.4 — submission's variant_id MUST equal the task's variant.

    "An evaluator submits with: variant_id — the variant it evaluated."
    The task store enforces this so an evaluator cannot misroute a
    metrics result onto an unrelated variant.
    """
    eval_tid, _variant_id = _drive_to_starting_variant(wire_client)
    c = _seed.claim(wire_client, eval_tid)
    r = _seed.submit_evaluation(
        wire_client,
        eval_tid,
        worker_id=c["worker_id"],
        variant_id="some-other-variant",
        evaluation={"score": 1.0},
    )
    assert r.status_code == 409, r.text
    assert r.json().get("type") == "eden://error/illegal-transition"


def test_success_evaluation_outside_schema_must_not_complete_variant(
    wire_client: WireClient,
) -> None:
    """spec/v0/03-roles.md §4.2 — evaluation keys MUST be a subset of evaluation_schema.

    "Produce a `metrics` object whose keys are a subset of the
    declared `evaluation_schema` keys." A conforming IUT MUST reject a
    success submission whose metrics include a key the schema does
    not declare; the variant MUST NOT terminalize as success. Where in
    the pipeline the rejection surfaces is implementation-defined,
    so the assertion checks the observable end-state.
    """
    eval_tid, variant_id = _drive_to_starting_variant(wire_client)
    c = _seed.claim(wire_client, eval_tid)
    r = _seed.submit_evaluation(
        wire_client,
        eval_tid,
        worker_id=c["worker_id"],
        variant_id=variant_id,
        evaluation={"score": 1.0, "undeclared_key": 99},
    )
    if 400 <= r.status_code < 500:
        # Rejected at submit with 4xx — conforming.
        variant = _seed.read_variant(wire_client, variant_id)
        assert variant["status"] != "success"
        return
    assert r.status_code == 200, (
        f"submit returned {r.status_code}; expected 4xx rejection or 200 — "
        "5xx is a server bug, not §4.2 latitude"
    )
    accept = _seed.accept(wire_client, eval_tid)
    if 200 <= accept.status_code < 300:
        variant = _seed.read_variant(wire_client, variant_id)
        assert variant["status"] != "success", (
            "evaluator success with undeclared metric key must not produce success variant"
        )
        return
    assert 400 <= accept.status_code < 500, (
        f"/accept returned {accept.status_code}; expected 4xx rejection — "
        "5xx is a server bug"
    )
    variant = _seed.read_variant(wire_client, variant_id)
    assert variant["status"] != "success"


def test_success_metric_wrong_type_must_not_complete_variant(
    wire_client: WireClient,
) -> None:
    """spec/v0/03-roles.md §4.2 — metric values MUST satisfy per-metric type rules.

    "Values satisfy the per-metric type rules (02 §1.3, §7.2)."
    The fixture's `retries` is declared `integer`; a non-integer
    value MUST NOT be accepted onto the variant.
    """
    eval_tid, variant_id = _drive_to_starting_variant(wire_client)
    c = _seed.claim(wire_client, eval_tid)
    r = _seed.submit_evaluation(
        wire_client,
        eval_tid,
        worker_id=c["worker_id"],
        variant_id=variant_id,
        # retries is declared `integer`; 1.5 is not a JSON-legal integer.
        evaluation={"score": 1.0, "retries": 1.5},
    )
    if 400 <= r.status_code < 500:
        variant = _seed.read_variant(wire_client, variant_id)
        assert variant["status"] != "success"
        return
    assert r.status_code == 200, (
        f"submit returned {r.status_code}; expected 4xx rejection or 200"
    )
    accept = _seed.accept(wire_client, eval_tid)
    if 200 <= accept.status_code < 300:
        variant = _seed.read_variant(wire_client, variant_id)
        assert variant["status"] != "success", (
            "evaluator success with type-violating metric must not produce success variant"
        )
        return
    assert 400 <= accept.status_code < 500, (
        f"/accept returned {accept.status_code}; expected 4xx rejection"
    )
    variant = _seed.read_variant(wire_client, variant_id)
    assert variant["status"] != "success"


def test_success_writes_variant_fields_post_accept(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/03-roles.md §4.4 — accepted success writes evaluation + uri.

    Asserts the §4.4 variant-side write rule: after /accept on a success
    submission, the variant's `status == "success"`, `metrics`,
    `artifacts_uri`, and `completed_at` carry the submitted values,
    and `variant.succeeded` is in the event log. The §4.4 atomicity
    claim ("written atomically with the event") is asserted in
    `Composite commits` (chunk 11b) via the chapter-05 §2.2 group;
    this test only pins the per-field positive-write coverage.
    """
    eval_tid, variant_id = _drive_to_starting_variant(wire_client)
    c = _seed.claim(wire_client, eval_tid)
    evaluation={"score": 0.75, "retries": 3}
    artifacts_uri = "file:///tmp/eden-conformance-success-artifacts"
    r = _seed.submit_evaluation(
        wire_client,
        eval_tid,
        worker_id=c["worker_id"],
        variant_id=variant_id,
        evaluation=evaluation,
        artifacts_uri=artifacts_uri,
    )
    assert r.status_code == 200, r.text
    accept = _seed.accept(wire_client, eval_tid)
    assert 200 <= accept.status_code < 300, accept.text
    variant = _seed.read_variant(wire_client, variant_id)
    assert variant["status"] == "success"
    assert variant["evaluation"] == evaluation
    assert variant.get("artifacts_uri") == artifacts_uri
    assert variant.get("completed_at") is not None
    succeeded = [
        e
        for e in event_log.find_by_type(event_log.replay_all(), "variant.succeeded")
        if e["data"].get("variant_id") == variant_id
    ]
    assert len(succeeded) == 1


def test_status_error_writes_variant_evaluation_and_artifacts(
    wire_client: WireClient,
) -> None:
    """spec/v0/03-roles.md §4.4 — status=error MUST write variant metrics + artifacts_uri.

    "metrics — set to the submission's `metrics` when status ∈
    {'success', 'error'}." Distinct from the evaluation_error case (which
    discards metrics): the §4.4 variant-side write rule is per-status,
    and the error path keeps the metrics around because the variant
    DID run; only the run failed. The reject reason is
    `worker_error` — `validation_error` would discard the payload
    instead.
    """
    eval_tid, variant_id = _drive_to_starting_variant(wire_client)
    c = _seed.claim(wire_client, eval_tid)
    evaluation={"score": 0.0, "retries": 5}
    artifacts_uri = "file:///tmp/eden-conformance-error-artifacts"
    r = _seed.submit_evaluation(
        wire_client,
        eval_tid,
        worker_id=c["worker_id"],
        variant_id=variant_id,
        status="error",
        evaluation=evaluation,
        artifacts_uri=artifacts_uri,
    )
    assert r.status_code == 200, r.text
    rejected = _seed.reject(wire_client, eval_tid, reason="worker_error")
    assert 200 <= rejected.status_code < 300, rejected.text
    variant = _seed.read_variant(wire_client, variant_id)
    assert variant["status"] == "error"
    assert variant["evaluation"] == evaluation
    assert variant.get("artifacts_uri") == artifacts_uri
    assert variant.get("completed_at") is not None


def test_eval_error_keeps_variant_starting_and_does_not_graft_evaluation(
    wire_client: WireClient,
) -> None:
    """spec/v0/03-roles.md §4.4 — evaluation_error MUST keep variant in starting; metrics discarded.

    "When status == evaluation_error, the orchestrator MUST NOT write
    metrics on the variant; any submission-carried metrics is
    discarded." Observed: after submitting evaluation_error with metrics
    and rejecting the task, the variant stays in `starting` and its
    `metrics` field is unset.
    """
    eval_tid, variant_id = _drive_to_starting_variant(wire_client)
    c = _seed.claim(wire_client, eval_tid)
    r = _seed.submit_evaluation(
        wire_client,
        eval_tid,
        worker_id=c["worker_id"],
        variant_id=variant_id,
        status="evaluation_error",
        evaluation={"score": 0.5, "retries": 1},
    )
    assert r.status_code == 200, r.text
    rejected = _seed.reject(wire_client, eval_tid, reason="worker_error")
    assert 200 <= rejected.status_code < 300, rejected.text
    variant = _seed.read_variant(wire_client, variant_id)
    assert variant["status"] == "starting"
    assert variant.get("metrics") is None, variant
    assert variant.get("artifacts_uri") is None, variant


def test_retry_exhausted_eval_error_does_not_graft_prior_evaluation(
    wire_client: WireClient,
) -> None:
    """spec/v0/03-roles.md §4.4 — retry-exhausted evaluation_error MUST NOT graft prior metrics.

    "On the retry-exhausted evaluation_error terminal transition itself,
    the orchestrator MUST NOT graft metrics or artifacts from any
    prior evaluation_error submission onto the variant; the variant's metrics
    and artifacts_uri fields remain unset."
    """
    eval_tid, variant_id = _drive_to_starting_variant(wire_client)
    c = _seed.claim(wire_client, eval_tid)
    r = _seed.submit_evaluation(
        wire_client,
        eval_tid,
        worker_id=c["worker_id"],
        variant_id=variant_id,
        status="evaluation_error",
        evaluation={"score": 0.25, "retries": 7},
    )
    assert r.status_code == 200, r.text
    rejected = _seed.reject(wire_client, eval_tid, reason="worker_error")
    assert 200 <= rejected.status_code < 300, rejected.text
    # Now the orchestrator decides the retry budget is exhausted.
    decl = _seed.declare_variant_evaluation_error(wire_client, variant_id)
    assert 200 <= decl.status_code < 300, decl.text
    variant = _seed.read_variant(wire_client, variant_id)
    assert variant["status"] == "evaluation_error"
    assert variant.get("metrics") is None, variant
    assert variant.get("artifacts_uri") is None, variant


def test_resubmit_idempotent_under_role_rules(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/03-roles.md §4.4 — identical resubmission MUST be accepted; only one task.submitted.

    Per the §4.4 amendment in this chunk: "identical normative
    fields (`variant_id`, `status`, `metrics`) MUST be accepted." The
    test holds all four fields (`variant_id`, `status`, `metrics`,
    `artifacts_uri`) identical between the two submits as the
    baseline-equivalence check; the artifacts_uri-non-equivalence
    test below pins the half of the amendment that says
    `artifacts_uri` is NOT in the equivalence formula.
    """
    eval_tid, variant_id = _drive_to_starting_variant(wire_client)
    c = _seed.claim(wire_client, eval_tid)
    evaluation={"score": 0.9, "retries": 0}
    artifacts_uri = "file:///tmp/eden-conformance-idem-baseline"
    r1 = _seed.submit_evaluation(
        wire_client,
        eval_tid,
        worker_id=c["worker_id"],
        variant_id=variant_id,
        evaluation=evaluation,
        artifacts_uri=artifacts_uri,
    )
    assert r1.status_code == 200, r1.text
    r2 = _seed.submit_evaluation(
        wire_client,
        eval_tid,
        worker_id=c["worker_id"],
        variant_id=variant_id,
        evaluation=dict(evaluation),
        artifacts_uri=artifacts_uri,
    )
    assert r2.status_code == 200, r2.text
    submitted = [
        e
        for e in event_log.find_by_type(event_log.replay_all(), "task.submitted")
        if e["data"].get("task_id") == eval_tid
    ]
    assert len(submitted) == 1


def test_resubmit_with_different_artifacts_uri_is_idempotent(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/03-roles.md §4.4 — artifacts_uri MUST NOT block equivalence.

    Per the §4.4 amendment in this chunk: "`artifacts_uri` is NOT
    part of equivalence — the first submission's `artifacts_uri` is
    the committed one." Two submits with identical
    `variant_id`+`status`+`metrics` but DIFFERENT `artifacts_uri`
    MUST both return 200, MUST emit only one `task.submitted`, and
    after `/accept` the variant's `artifacts_uri` MUST equal the
    *first* submission's value.
    """
    eval_tid, variant_id = _drive_to_starting_variant(wire_client)
    c = _seed.claim(wire_client, eval_tid)
    evaluation={"score": 0.5, "retries": 1}
    first_uri = "file:///tmp/eden-conformance-first"
    second_uri = "file:///tmp/eden-conformance-second"
    r1 = _seed.submit_evaluation(
        wire_client,
        eval_tid,
        worker_id=c["worker_id"],
        variant_id=variant_id,
        evaluation=evaluation,
        artifacts_uri=first_uri,
    )
    assert r1.status_code == 200, r1.text
    r2 = _seed.submit_evaluation(
        wire_client,
        eval_tid,
        worker_id=c["worker_id"],
        variant_id=variant_id,
        evaluation=dict(evaluation),
        artifacts_uri=second_uri,
    )
    assert r2.status_code == 200, r2.text
    submitted = [
        e
        for e in event_log.find_by_type(event_log.replay_all(), "task.submitted")
        if e["data"].get("task_id") == eval_tid
    ]
    assert len(submitted) == 1
    accept = _seed.accept(wire_client, eval_tid)
    assert 200 <= accept.status_code < 300, accept.text
    variant = _seed.read_variant(wire_client, variant_id)
    assert variant.get("artifacts_uri") == first_uri
