"""Evaluator submission semantics — chapter 03 §4.2, §4.4."""

from __future__ import annotations

import pytest
from conformance.harness import _seed
from conformance.harness.event_cursor import EventLog
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = 'Evaluator submission'


def _drive_to_starting_trial(client: WireClient) -> tuple[str, str]:
    """Return (eval_task_id, trial_id) for a freshly-prepared evaluator scenario."""
    trial_id = _seed.drive_to_starting_trial(client)
    eval_tid = _seed.create_evaluate_task(client, trial_id=trial_id)
    return eval_tid, trial_id


def test_submit_with_mismatched_trial_id_rejected(wire_client: WireClient) -> None:
    """spec/v0/03-roles.md §4.4 — submission's trial_id MUST equal the task's trial.

    "An evaluator submits with: trial_id — the trial it evaluated."
    The task store enforces this so an evaluator cannot misroute a
    metrics result onto an unrelated trial.
    """
    eval_tid, _trial_id = _drive_to_starting_trial(wire_client)
    c = _seed.claim(wire_client, eval_tid)
    r = _seed.submit_evaluate(
        wire_client,
        eval_tid,
        token=c["token"],
        trial_id="some-other-trial",
        metrics={"score": 1.0},
    )
    assert r.status_code == 409, r.text
    assert r.json().get("type") == "eden://error/illegal-transition"


def test_success_metrics_outside_schema_must_not_complete_trial(
    wire_client: WireClient,
) -> None:
    """spec/v0/03-roles.md §4.2 — metrics keys MUST be a subset of metrics_schema.

    "Produce a `metrics` object whose keys are a subset of the
    declared `metrics_schema` keys." A conforming IUT MUST reject a
    success submission whose metrics include a key the schema does
    not declare; the trial MUST NOT terminalize as success. Where in
    the pipeline the rejection surfaces is implementation-defined,
    so the assertion checks the observable end-state.
    """
    eval_tid, trial_id = _drive_to_starting_trial(wire_client)
    c = _seed.claim(wire_client, eval_tid)
    r = _seed.submit_evaluate(
        wire_client,
        eval_tid,
        token=c["token"],
        trial_id=trial_id,
        metrics={"score": 1.0, "undeclared_key": 99},
    )
    if 400 <= r.status_code < 500:
        # Rejected at submit with 4xx — conforming.
        trial = _seed.read_trial(wire_client, trial_id)
        assert trial["status"] != "success"
        return
    assert r.status_code == 200, (
        f"submit returned {r.status_code}; expected 4xx rejection or 200 — "
        "5xx is a server bug, not §4.2 latitude"
    )
    accept = _seed.accept(wire_client, eval_tid)
    if 200 <= accept.status_code < 300:
        trial = _seed.read_trial(wire_client, trial_id)
        assert trial["status"] != "success", (
            "evaluator success with undeclared metric key must not produce success trial"
        )
        return
    assert 400 <= accept.status_code < 500, (
        f"/accept returned {accept.status_code}; expected 4xx rejection — "
        "5xx is a server bug"
    )
    trial = _seed.read_trial(wire_client, trial_id)
    assert trial["status"] != "success"


def test_success_metric_wrong_type_must_not_complete_trial(
    wire_client: WireClient,
) -> None:
    """spec/v0/03-roles.md §4.2 — metric values MUST satisfy per-metric type rules.

    "Values satisfy the per-metric type rules (02 §1.3, §7.2)."
    The fixture's `retries` is declared `integer`; a non-integer
    value MUST NOT be accepted onto the trial.
    """
    eval_tid, trial_id = _drive_to_starting_trial(wire_client)
    c = _seed.claim(wire_client, eval_tid)
    r = _seed.submit_evaluate(
        wire_client,
        eval_tid,
        token=c["token"],
        trial_id=trial_id,
        # retries is declared `integer`; 1.5 is not a JSON-legal integer.
        metrics={"score": 1.0, "retries": 1.5},
    )
    if 400 <= r.status_code < 500:
        trial = _seed.read_trial(wire_client, trial_id)
        assert trial["status"] != "success"
        return
    assert r.status_code == 200, (
        f"submit returned {r.status_code}; expected 4xx rejection or 200"
    )
    accept = _seed.accept(wire_client, eval_tid)
    if 200 <= accept.status_code < 300:
        trial = _seed.read_trial(wire_client, trial_id)
        assert trial["status"] != "success", (
            "evaluator success with type-violating metric must not produce success trial"
        )
        return
    assert 400 <= accept.status_code < 500, (
        f"/accept returned {accept.status_code}; expected 4xx rejection"
    )
    trial = _seed.read_trial(wire_client, trial_id)
    assert trial["status"] != "success"


def test_success_writes_trial_fields_post_accept(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/03-roles.md §4.4 — accepted success MUST write metrics + artifacts_uri on the trial.

    Asserts the §4.4 trial-side write rule: after /accept on a success
    submission, the trial's `status == "success"`, `metrics`,
    `artifacts_uri`, and `completed_at` carry the submitted values,
    and `trial.succeeded` is in the event log. The §4.4 atomicity
    claim ("written atomically with the event") is asserted in
    `Composite commits` (chunk 11b) via the chapter-05 §2.2 group;
    this test only pins the per-field positive-write coverage.
    """
    eval_tid, trial_id = _drive_to_starting_trial(wire_client)
    c = _seed.claim(wire_client, eval_tid)
    metrics = {"score": 0.75, "retries": 3}
    artifacts_uri = "file:///tmp/eden-conformance-success-artifacts"
    r = _seed.submit_evaluate(
        wire_client,
        eval_tid,
        token=c["token"],
        trial_id=trial_id,
        metrics=metrics,
        artifacts_uri=artifacts_uri,
    )
    assert r.status_code == 200, r.text
    accept = _seed.accept(wire_client, eval_tid)
    assert 200 <= accept.status_code < 300, accept.text
    trial = _seed.read_trial(wire_client, trial_id)
    assert trial["status"] == "success"
    assert trial["metrics"] == metrics
    assert trial.get("artifacts_uri") == artifacts_uri
    assert trial.get("completed_at") is not None
    succeeded = [
        e
        for e in event_log.find_by_type(event_log.replay_all(), "trial.succeeded")
        if e["data"].get("trial_id") == trial_id
    ]
    assert len(succeeded) == 1


def test_status_error_writes_trial_metrics_and_artifacts(
    wire_client: WireClient,
) -> None:
    """spec/v0/03-roles.md §4.4 — status=error MUST write trial metrics + artifacts_uri.

    "metrics — set to the submission's `metrics` when status ∈
    {'success', 'error'}." Distinct from the eval_error case (which
    discards metrics): the §4.4 trial-side write rule is per-status,
    and the error path keeps the metrics around because the trial
    DID run; only the run failed. The reject reason is
    `worker_error` — `validation_error` would discard the payload
    instead.
    """
    eval_tid, trial_id = _drive_to_starting_trial(wire_client)
    c = _seed.claim(wire_client, eval_tid)
    metrics = {"score": 0.0, "retries": 5}
    artifacts_uri = "file:///tmp/eden-conformance-error-artifacts"
    r = _seed.submit_evaluate(
        wire_client,
        eval_tid,
        token=c["token"],
        trial_id=trial_id,
        status="error",
        metrics=metrics,
        artifacts_uri=artifacts_uri,
    )
    assert r.status_code == 200, r.text
    rejected = _seed.reject(wire_client, eval_tid, reason="worker_error")
    assert 200 <= rejected.status_code < 300, rejected.text
    trial = _seed.read_trial(wire_client, trial_id)
    assert trial["status"] == "error"
    assert trial["metrics"] == metrics
    assert trial.get("artifacts_uri") == artifacts_uri
    assert trial.get("completed_at") is not None


def test_eval_error_keeps_trial_starting_and_does_not_graft_metrics(
    wire_client: WireClient,
) -> None:
    """spec/v0/03-roles.md §4.4 — eval_error MUST keep trial in starting; metrics discarded.

    "When status == eval_error, the orchestrator MUST NOT write
    metrics on the trial; any submission-carried metrics is
    discarded." Observed: after submitting eval_error with metrics
    and rejecting the task, the trial stays in `starting` and its
    `metrics` field is unset.
    """
    eval_tid, trial_id = _drive_to_starting_trial(wire_client)
    c = _seed.claim(wire_client, eval_tid)
    r = _seed.submit_evaluate(
        wire_client,
        eval_tid,
        token=c["token"],
        trial_id=trial_id,
        status="eval_error",
        metrics={"score": 0.5, "retries": 1},
    )
    assert r.status_code == 200, r.text
    rejected = _seed.reject(wire_client, eval_tid, reason="worker_error")
    assert 200 <= rejected.status_code < 300, rejected.text
    trial = _seed.read_trial(wire_client, trial_id)
    assert trial["status"] == "starting"
    assert trial.get("metrics") is None, trial
    assert trial.get("artifacts_uri") is None, trial


def test_retry_exhausted_eval_error_does_not_graft_prior_metrics(
    wire_client: WireClient,
) -> None:
    """spec/v0/03-roles.md §4.4 — retry-exhausted eval_error terminal MUST NOT graft prior metrics.

    "On the retry-exhausted eval_error terminal transition itself,
    the orchestrator MUST NOT graft metrics or artifacts from any
    prior eval_error submission onto the trial; the trial's metrics
    and artifacts_uri fields remain unset."
    """
    eval_tid, trial_id = _drive_to_starting_trial(wire_client)
    c = _seed.claim(wire_client, eval_tid)
    r = _seed.submit_evaluate(
        wire_client,
        eval_tid,
        token=c["token"],
        trial_id=trial_id,
        status="eval_error",
        metrics={"score": 0.25, "retries": 7},
    )
    assert r.status_code == 200, r.text
    rejected = _seed.reject(wire_client, eval_tid, reason="worker_error")
    assert 200 <= rejected.status_code < 300, rejected.text
    # Now the orchestrator decides the retry budget is exhausted.
    decl = _seed.declare_trial_eval_error(wire_client, trial_id)
    assert 200 <= decl.status_code < 300, decl.text
    trial = _seed.read_trial(wire_client, trial_id)
    assert trial["status"] == "eval_error"
    assert trial.get("metrics") is None, trial
    assert trial.get("artifacts_uri") is None, trial


def test_resubmit_idempotent_under_role_rules(
    wire_client: WireClient, event_log: EventLog
) -> None:
    """spec/v0/03-roles.md §4.4 — identical resubmission MUST be accepted; only one task.submitted.

    Per the §4.4 amendment in this chunk: "identical normative
    fields (`trial_id`, `status`, `metrics`) MUST be accepted." The
    test holds all four fields (`trial_id`, `status`, `metrics`,
    `artifacts_uri`) identical between the two submits as the
    baseline-equivalence check; the artifacts_uri-non-equivalence
    test below pins the half of the amendment that says
    `artifacts_uri` is NOT in the equivalence formula.
    """
    eval_tid, trial_id = _drive_to_starting_trial(wire_client)
    c = _seed.claim(wire_client, eval_tid)
    metrics = {"score": 0.9, "retries": 0}
    artifacts_uri = "file:///tmp/eden-conformance-idem-baseline"
    r1 = _seed.submit_evaluate(
        wire_client,
        eval_tid,
        token=c["token"],
        trial_id=trial_id,
        metrics=metrics,
        artifacts_uri=artifacts_uri,
    )
    assert r1.status_code == 200, r1.text
    r2 = _seed.submit_evaluate(
        wire_client,
        eval_tid,
        token=c["token"],
        trial_id=trial_id,
        metrics=dict(metrics),
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
    `trial_id`+`status`+`metrics` but DIFFERENT `artifacts_uri`
    MUST both return 200, MUST emit only one `task.submitted`, and
    after `/accept` the trial's `artifacts_uri` MUST equal the
    *first* submission's value.
    """
    eval_tid, trial_id = _drive_to_starting_trial(wire_client)
    c = _seed.claim(wire_client, eval_tid)
    metrics = {"score": 0.5, "retries": 1}
    first_uri = "file:///tmp/eden-conformance-first"
    second_uri = "file:///tmp/eden-conformance-second"
    r1 = _seed.submit_evaluate(
        wire_client,
        eval_tid,
        token=c["token"],
        trial_id=trial_id,
        metrics=metrics,
        artifacts_uri=first_uri,
    )
    assert r1.status_code == 200, r1.text
    r2 = _seed.submit_evaluate(
        wire_client,
        eval_tid,
        token=c["token"],
        trial_id=trial_id,
        metrics=dict(metrics),
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
    trial = _seed.read_trial(wire_client, trial_id)
    assert trial.get("artifacts_uri") == first_uri
