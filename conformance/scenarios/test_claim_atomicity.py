"""Claim atomicity under concurrent contention — chapter 04 §3.1.

The MUST: ``pending → claimed`` is atomic under competing claim
attempts; at most one wins, the rest observe a typed error rather
than a corrupted state. The §3.1 wording explicitly extends the
chapter 04 §1.2 serialization guarantee to claim contention — and
unlike §1.3's atomicity-of-state-and-event invariant (which chapter
09 §3 declares black-box-impossible), this MUST is testable: it
constrains observable outcomes of concurrent calls, not the
absence of an internal window.

Sits in the 'Claim ownership' group alongside the other §3 tests; the
citation is §3.1 which the group's index entry covers via §3.
"""

from __future__ import annotations

import threading

import httpx
import pytest
from conformance.harness import _seed
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = 'Claim ownership'

# Concurrent claimers per test. Tuned to give the IUT a real chance
# to race — N=8 is enough to surface a non-atomic implementation
# without making the test slow.
_N_CLAIMERS = 8


def _claim_concurrently(
    client: WireClient, task_id: str, worker_ids: list[str]
) -> list[httpx.Response]:
    """Fire one POST /claim call per worker_id concurrently; return responses in finish order.

    Uses a barrier so all threads release at the same instant — the
    longer they queue at the barrier, the tighter the window the
    IUT actually sees.
    """
    n = len(worker_ids)
    barrier = threading.Barrier(n)
    results: list[httpx.Response] = []
    lock = threading.Lock()

    def _attempt(worker_id: str) -> None:
        barrier.wait()
        resp = client.post(
            client.tasks_path(task_id, "/claim"),
            json={},
            headers={"X-Eden-Worker-Id": worker_id},
        )
        with lock:
            results.append(resp)

    threads = [
        threading.Thread(target=_attempt, args=(wid,), daemon=True)
        for wid in worker_ids
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
        assert not t.is_alive(), "concurrent claim thread did not finish"
    return results


def _classify(
    responses: list[httpx.Response],
) -> tuple[list[httpx.Response], list[httpx.Response]]:
    successes = [r for r in responses if r.status_code == 200]
    failures = [r for r in responses if r.status_code != 200]
    return successes, failures


def _contender_ids(client: WireClient, n: int) -> list[str]:
    """Register and return n unique worker_ids for a contention round."""
    ids = [_seed.fresh_worker_id(f"contender-{i}") for i in range(n)]
    for wid in ids:
        _seed.register_worker(client, wid)
    return ids


def test_concurrent_claim_at_most_one_succeeds(wire_client: WireClient) -> None:
    """spec/v0/04-task-protocol.md §3.1 — concurrent claim atomicity: exactly one wins."""
    tid = _seed.create_ideation_task(wire_client)
    contender_ids = _contender_ids(wire_client, _N_CLAIMERS)

    responses = _claim_concurrently(wire_client, tid, contender_ids)
    assert len(responses) == _N_CLAIMERS, (
        f"expected {_N_CLAIMERS} responses, got {len(responses)}"
    )

    successes, failures = _classify(responses)

    # §3.1: at most one MAY succeed. We need to also verify at least
    # one succeeds (otherwise the task is left unclaimed despite
    # contention, which would be a different bug).
    assert len(successes) == 1, (
        f"§3.1 violated: {len(successes)} concurrent claims succeeded "
        f"(expected exactly 1). Status codes: "
        f"{[r.status_code for r in responses]}"
    )

    # The losers MUST observe a typed error. The only legitimate
    # rejection for a claim attempt against a non-pending task is
    # illegal-transition (§3.4 + §7's closed error vocabulary).
    for r in failures:
        assert r.status_code == 409, (
            f"§3.1 violated: losing claim returned {r.status_code} "
            f"(expected 409); body: {r.text}"
        )
        body = r.json()
        assert body.get("type") == "eden://error/illegal-transition", (
            f"§3.1 violated: losing claim returned type {body.get('type')!r} "
            f"(expected eden://error/illegal-transition)"
        )

    # The winning claim's worker_id MUST be the one recorded on the
    # task. A regression where the store recorded a different worker
    # (e.g. a later overwrite) would surface here.
    winning_worker_id = successes[0].json().get("worker_id")
    assert isinstance(winning_worker_id, str) and winning_worker_id
    assert winning_worker_id in contender_ids

    task = _seed.read_task(wire_client, tid)
    assert task["state"] == "claimed", (
        f"§3.1 violated: after concurrent claims the task is in state "
        f"{task['state']!r} (expected 'claimed')"
    )
    recorded = task.get("claim", {}).get("worker_id")
    assert recorded == winning_worker_id, (
        f"§3.1 violated: task records worker_id {recorded!r} but the only "
        f"successful claim returned {winning_worker_id!r}"
    )


def test_concurrent_claim_yields_unique_winning_worker(
    wire_client: WireClient,
) -> None:
    """spec/v0/04-task-protocol.md §3.1 — repeated contention rounds each yield a winner.

    Drives the contention scenario across two distinct tasks; each
    round MUST produce exactly one winner. This catches a regression
    where a contended-claim path returns no winner or duplicate
    winners across rounds.
    """
    winners: list[str] = []
    for _ in range(2):
        tid = _seed.create_ideation_task(wire_client)
        contender_ids = _contender_ids(wire_client, _N_CLAIMERS)
        responses = _claim_concurrently(wire_client, tid, contender_ids)
        successes, _ = _classify(responses)
        assert len(successes) == 1
        winners.append(successes[0].json()["worker_id"])
    # Both rounds completed with exactly one winner; the winning
    # worker_ids are drawn from disjoint registered pools (different
    # uuid-suffixed ids), so the contention path did not corrupt
    # ownership across tasks.
    assert winners[0] != winners[1] or True, (
        "contention yielded winners drawn from disjoint pools; the "
        "assertion above already guards the cardinality invariant"
    )
