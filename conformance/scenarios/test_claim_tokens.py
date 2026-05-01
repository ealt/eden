"""Claim-token semantics — freshness, authorization, no-reclaim-while-claimed."""

from __future__ import annotations

import pytest
from conformance.harness import _seed
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = 'Claim tokens'


def test_token_present_on_claim(wire_client: WireClient) -> None:
    """spec/v0/02-data-model.md §3.4 — claim object carries `token`."""
    tid = _seed.create_plan_task(wire_client)
    c = _seed.claim(wire_client, tid)
    assert isinstance(c.get("token"), str) and c["token"]


def test_token_unique_across_tasks(wire_client: WireClient) -> None:
    """spec/v0/04-task-protocol.md §3.2 — tokens are unique within an experiment."""
    t1 = _seed.create_plan_task(wire_client)
    t2 = _seed.create_plan_task(wire_client)
    c1 = _seed.claim(wire_client, t1, worker_id="w1")
    c2 = _seed.claim(wire_client, t2, worker_id="w2")
    assert c1["token"] != c2["token"]


def test_token_unique_across_reclaim_cycles(wire_client: WireClient) -> None:
    """spec/v0/04-task-protocol.md §3.2 — re-claim after reclaim issues a fresh token."""
    tid = _seed.create_plan_task(wire_client)
    c1 = _seed.claim(wire_client, tid, worker_id="w1")
    _seed.reclaim(wire_client, tid, cause="operator")
    c2 = _seed.claim(wire_client, tid, worker_id="w1")
    assert c1["token"] != c2["token"]


def test_wrong_token_rejected(wire_client: WireClient) -> None:
    """spec/v0/04-task-protocol.md §3.3 — submit with wrong token returns 403 wrong-token."""
    tid = _seed.create_plan_task(wire_client)
    _seed.claim(wire_client, tid)
    r = _seed.submit_plan(wire_client, tid, token="not-the-real-token")
    assert r.status_code == 403, f"Expected 403 wrong-token, got {r.status_code}"
    assert r.json().get("type") == "eden://error/wrong-token"


def test_token_invalidated_by_reclaim(wire_client: WireClient) -> None:
    """spec/v0/04-task-protocol.md §5.2 — prior token rejected after reclaim."""
    tid = _seed.create_plan_task(wire_client)
    c = _seed.claim(wire_client, tid)
    _seed.reclaim(wire_client, tid, cause="operator")
    # Re-claim so the task is in `claimed` state again (otherwise submit
    # would be rejected as illegal-transition rather than wrong-token).
    _seed.claim(wire_client, tid, worker_id="w-fresh")
    r = _seed.submit_plan(wire_client, tid, token=c["token"])
    assert r.status_code == 403
    assert r.json().get("type") == "eden://error/wrong-token"


def test_no_reclaim_while_claimed(wire_client: WireClient) -> None:
    """spec/v0/04-task-protocol.md §3.4 — second worker cannot claim a claimed task."""
    tid = _seed.create_plan_task(wire_client)
    _seed.claim(wire_client, tid, worker_id="w1")
    r = wire_client.post(wire_client.tasks_path(tid, "/claim"), json={"worker_id": "w2"})
    assert r.status_code == 409
    assert r.json().get("type") == "eden://error/illegal-transition"
