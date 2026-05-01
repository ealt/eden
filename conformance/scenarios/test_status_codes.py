"""Status code mappings — chapter 07 §§2-6, §7."""

from __future__ import annotations

import pytest
from conformance.harness import _seed
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = 'Status codes'


def test_create_task_returns_200(wire_client: WireClient) -> None:
    """spec/v0/07-wire-protocol.md §2.1 — successful create returns 200."""
    body = {
        "task_id": _seed.fresh_task_id(),
        "kind": "plan",
        "state": "pending",
        "payload": {"experiment_id": wire_client.experiment_id},
        "created_at": "2026-05-01T00:00:00Z",
        "updated_at": "2026-05-01T00:00:00Z",
    }
    r = wire_client.post(wire_client.tasks_path(), json=body)
    assert r.status_code == 200


def test_create_duplicate_task_returns_409_already_exists(
    wire_client: WireClient,
) -> None:
    """spec/v0/07-wire-protocol.md §7 — duplicate create returns 409 already-exists.

    Without this scenario the §7 vocabulary-closure test would have no
    producer for `eden://error/already-exists`.
    """
    tid = _seed.fresh_task_id()
    body = {
        "task_id": tid,
        "kind": "plan",
        "state": "pending",
        "payload": {"experiment_id": wire_client.experiment_id},
        "created_at": "2026-05-01T00:00:00Z",
        "updated_at": "2026-05-01T00:00:00Z",
    }
    wire_client.post(wire_client.tasks_path(), json=body)
    r = wire_client.post(wire_client.tasks_path(), json=body)
    assert r.status_code == 409
    assert r.json().get("type") == "eden://error/already-exists"


def test_read_unknown_task_returns_404(wire_client: WireClient) -> None:
    """spec/v0/07-wire-protocol.md §2.2 — unknown task returns 404 not-found."""
    r = wire_client.get(wire_client.tasks_path("does-not-exist"))
    assert r.status_code == 404
    assert r.json().get("type") == "eden://error/not-found"


def test_claim_non_pending_returns_409(wire_client: WireClient) -> None:
    """spec/v0/07-wire-protocol.md §2.3 — claim of non-pending returns 409 illegal-transition."""
    tid = _seed.create_plan_task(wire_client)
    _seed.claim(wire_client, tid, worker_id="w1")
    r = wire_client.post(wire_client.tasks_path(tid, "/claim"), json={"worker_id": "w2"})
    assert r.status_code == 409
    assert r.json().get("type") == "eden://error/illegal-transition"


def test_submit_wrong_token_returns_403(wire_client: WireClient) -> None:
    """spec/v0/07-wire-protocol.md §2.4 — wrong token returns 403 wrong-token."""
    tid = _seed.create_plan_task(wire_client)
    _seed.claim(wire_client, tid)
    r = _seed.submit_plan(wire_client, tid, token="wrong")
    assert r.status_code == 403
    assert r.json().get("type") == "eden://error/wrong-token"


def test_submit_divergent_returns_409(wire_client: WireClient) -> None:
    """spec/v0/07-wire-protocol.md §2.4 — divergent resubmit returns 409 conflicting-resubmission."""  # noqa: E501
    pid_a = _seed.create_proposal(wire_client, slug="a")
    pid_b = _seed.create_proposal(wire_client, slug="b")
    _seed.mark_proposal_ready(wire_client, pid_a)
    _seed.mark_proposal_ready(wire_client, pid_b)
    tid = _seed.create_plan_task(wire_client)
    c = _seed.claim(wire_client, tid)
    _seed.submit_plan(wire_client, tid, token=c["token"], proposal_ids=[pid_a])
    r = _seed.submit_plan(wire_client, tid, token=c["token"], proposal_ids=[pid_b])
    assert r.status_code == 409
    assert r.json().get("type") == "eden://error/conflicting-resubmission"


def test_integrate_different_sha_returns_409(wire_client: WireClient) -> None:
    """spec/v0/07-wire-protocol.md §5 — different SHA returns 409 invalid-precondition."""
    trial_id = _seed.drive_to_success_trial(wire_client)
    r1 = _seed.integrate_trial(wire_client, trial_id, trial_commit_sha="a" * 40)
    assert 200 <= r1.status_code < 300, r1.text
    r2 = _seed.integrate_trial(wire_client, trial_id, trial_commit_sha="b" * 40)
    assert r2.status_code == 409
    assert r2.json().get("type") == "eden://error/invalid-precondition"


def test_bad_request_body_returns_400(wire_client: WireClient) -> None:
    """spec/v0/07-wire-protocol.md §7 — malformed body returns 400 bad-request."""
    r = wire_client.post(
        wire_client.tasks_path(), json={"this": "is not a valid task body"}
    )
    assert r.status_code == 400
    assert r.json().get("type") == "eden://error/bad-request"


def test_replay_returns_200_with_cursor(wire_client: WireClient) -> None:
    """spec/v0/05-event-protocol.md §4.4 — replay MUST return all events from cursor 0.

    The chapter-5 §4.4 MUST is "MUST allow any subscriber to replay
    the full stream from the experiment's first event"; chapter 7 §6.1
    binds it via `GET /events?cursor=0` returning `{events, cursor}`.
    """
    _seed.create_plan_task(wire_client)
    r = wire_client.get(wire_client.events_path(), params={"cursor": 0})
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body.get("events"), list)
    assert isinstance(body.get("cursor"), int)


def test_reclaim_cause_vocabulary_closed(wire_client: WireClient) -> None:
    """spec/v0/07-wire-protocol.md §2.6 — invalid reclaim cause returns 400 bad-request.

    Per chapter 4 §5.1 / 7 §2.6 the closed v0 cause vocabulary is
    {expired, operator, health_policy}; anything else is a malformed
    request body.
    """
    tid = _seed.create_plan_task(wire_client)
    _seed.claim(wire_client, tid)
    r = wire_client.post(
        wire_client.tasks_path(tid, "/reclaim"), json={"cause": "garbage"}
    )
    assert r.status_code == 400
    assert r.json().get("type") == "eden://error/bad-request"
