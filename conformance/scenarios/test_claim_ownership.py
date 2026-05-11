"""Claim ownership — chapter 04 §3, §4.1, §4.2, §5.

12a-1 retired the per-claim opaque ``token`` and replaced it with
identity-keyed ownership: the worker_id recorded on the claim is the
sole identity the §4 submit transition matches against. This file
collects the wire-observable MUSTs that pin that contract:

- Claim record carries ``worker_id`` (§3.2).
- Re-claim while still claimed is rejected (§3.4).
- Submit by a different ``worker_id`` than the recorded claimant
  raises ``WrongClaimant`` (§4.1).
- After reclamation the claim object is cleared and a fresh claim
  is required to proceed (§5.2).
"""

from __future__ import annotations

import pytest
from conformance.harness import _seed
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = 'Claim ownership'


def test_worker_id_present_on_claim(wire_client: WireClient) -> None:
    """spec/v0/04-task-protocol.md §3.2 — claim object carries `worker_id`."""
    tid = _seed.create_ideation_task(wire_client)
    c = _seed.claim(wire_client, tid, worker_id="test-worker")
    assert isinstance(c.get("worker_id"), str) and c["worker_id"] == "test-worker"


def test_task_records_claim_worker_id(wire_client: WireClient) -> None:
    """spec/v0/04-task-protocol.md §3.2 — `read_task` reflects the claim's worker_id."""
    tid = _seed.create_ideation_task(wire_client)
    _seed.claim(wire_client, tid, worker_id="worker-a")
    task = _seed.read_task(wire_client, tid)
    claim = task.get("claim") or {}
    assert claim.get("worker_id") == "worker-a"


def test_no_reclaim_while_claimed(wire_client: WireClient) -> None:
    """spec/v0/04-task-protocol.md §3.4 — second worker cannot claim a claimed task."""
    tid = _seed.create_ideation_task(wire_client)
    _seed.claim(wire_client, tid, worker_id="worker-a")
    r = wire_client.post(
        wire_client.tasks_path(tid, "/claim"),
        json={},
        headers={"X-Eden-Worker-Id": "worker-b"},
    )
    assert r.status_code == 409
    assert r.json().get("type") == "eden://error/illegal-transition"


def test_submit_by_wrong_worker_rejected(wire_client: WireClient) -> None:
    """spec/v0/04-task-protocol.md §4.1 — submit by non-claimant raises WrongClaimant.

    Claim as ``worker-a``; submit as ``worker-b``. The store MUST
    reject the submit with ``WrongClaimant`` per the §4.1 step-2
    atomic claim-match. The wire surface is
    ``eden://error/wrong-claimant`` (HTTP 403).
    """
    tid = _seed.create_ideation_task(wire_client)
    _seed.claim(wire_client, tid, worker_id="worker-a")
    r = _seed.submit_idea(wire_client, tid, worker_id="worker-b")
    assert r.status_code == 403, (
        f"§4.1 violated: submit by non-claimant returned {r.status_code} "
        f"(expected 403). Body: {r.text}"
    )
    assert r.json().get("type") == "eden://error/wrong-claimant"


def test_claim_cleared_after_reclaim(wire_client: WireClient) -> None:
    """spec/v0/04-task-protocol.md §5.2 — reclamation clears the claim object.

    After an operator reclaim the task is back in ``pending``; the
    claim object is cleared so subsequent submit attempts fail
    ``NotClaimed`` regardless of the worker_id presented.
    """
    tid = _seed.create_ideation_task(wire_client)
    _seed.claim(wire_client, tid, worker_id="worker-a")

    reclaim_resp = _seed.reclaim(wire_client, tid, cause="operator")
    assert 200 <= reclaim_resp.status_code < 300, reclaim_resp.text

    post_reclaim = _seed.read_task(wire_client, tid)
    assert post_reclaim["state"] == "pending"
    assert post_reclaim.get("claim") in (None, {}), (
        f"§5.2 violated: post-reclaim read_task still carries a claim "
        f"object: {post_reclaim.get('claim')!r}"
    )

    # Submit attempt against a cleared-claim task fails with §4.1's
    # ``NotClaimed`` precondition regardless of which worker_id is
    # presented.
    r = _seed.submit_idea(wire_client, tid, worker_id="worker-a")
    assert r.status_code == 409, r.text
    assert r.json().get("type") in (
        "eden://error/not-claimed",
        "eden://error/illegal-transition",
    )


def test_resubmit_by_other_worker_after_reclaim_chain_rejected(
    wire_client: WireClient,
) -> None:
    """spec/v0/04-task-protocol.md §4.2 — claimant-id check survives reclaim cycles.

    Claim as ``worker-a``; reclaim; re-claim as ``worker-b``; then
    ``worker-a`` submits with its no-longer-current identity. The
    store MUST reject with ``WrongClaimant`` per §4.2 (resubmit by a
    different worker_id than the recorded claimant).
    """
    tid = _seed.create_ideation_task(wire_client)
    _seed.claim(wire_client, tid, worker_id="worker-a")
    _seed.reclaim(wire_client, tid, cause="operator")
    _seed.claim(wire_client, tid, worker_id="worker-b")

    r = _seed.submit_idea(wire_client, tid, worker_id="worker-a")
    assert r.status_code == 403, r.text
    assert r.json().get("type") == "eden://error/wrong-claimant"
