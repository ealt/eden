"""Claim atomicity under concurrent contention — chapter 04 §3.1.

The MUST: ``pending → claimed`` is atomic under competing claim
attempts; at most one wins, the rest observe a typed error rather
than a corrupted state. The §3.1 wording explicitly extends the
chapter 04 §1.2 serialization guarantee to claim contention — and
unlike §1.3's atomicity-of-state-and-event invariant (which chapter
09 §3 declares black-box-impossible), this MUST is testable: it
constrains observable outcomes of concurrent calls, not the
absence of an internal window.

Sits in the 'Claim tokens' group alongside the other §3 tests; the
citation is §3.1 which the group's index entry covers via §3.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

import httpx
import pytest
from conformance.harness import _seed
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = 'Claim tokens'

# Concurrent claimers per test. Tuned to give the IUT a real chance
# to race — N=8 is enough to surface a non-atomic implementation
# without making the test slow.
_N_CLAIMERS = 8


def _claim_concurrently(
    client: WireClient, task_id: str, n: int
) -> list[httpx.Response]:
    """Fire ``n`` POST /claim calls concurrently; return responses in finish order.

    Uses a barrier so all threads release at the same instant — the
    longer they queue at the barrier, the tighter the window the
    IUT actually sees.
    """
    barrier = threading.Barrier(n)
    results: list[httpx.Response] = []
    lock = threading.Lock()

    def _attempt(worker_index: int) -> None:
        barrier.wait()
        resp = client.post(
            client.tasks_path(task_id, "/claim"),
            json={"worker_id": f"contender-{worker_index}"},
        )
        with lock:
            results.append(resp)

    threads = [
        threading.Thread(target=_attempt, args=(i,), daemon=True)
        for i in range(n)
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


def test_concurrent_claim_at_most_one_succeeds(wire_client: WireClient) -> None:
    """spec/v0/04-task-protocol.md §3.1 — concurrent claim atomicity: exactly one wins."""
    tid = _seed.create_ideate_task(wire_client)

    responses = _claim_concurrently(wire_client, tid, _N_CLAIMERS)
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

    # The winning claim's token MUST be the one recorded on the task.
    # A regression where the store recorded a different token (e.g. a
    # later overwrite) would surface here.
    winning_token = successes[0].json().get("token")
    assert isinstance(winning_token, str) and winning_token

    task = _seed.read_task(wire_client, tid)
    assert task["state"] == "claimed", (
        f"§3.1 violated: after concurrent claims the task is in state "
        f"{task['state']!r} (expected 'claimed')"
    )
    recorded = task.get("claim", {}).get("token")
    assert recorded == winning_token, (
        f"§3.1 violated: task records token {recorded!r} but the only "
        f"successful claim returned {winning_token!r}"
    )


def test_concurrent_claim_yields_unique_winning_token(
    wire_client: WireClient,
) -> None:
    """spec/v0/04-task-protocol.md §3.1 — repeated contention rounds each yield a fresh winner.

    Drives the contention scenario across two distinct tasks, then
    asserts the two winning tokens differ. This catches a regression
    where a contended-claim path returns a stale or shared token.
    """
    seen_tokens: list[str] = []
    for _ in range(2):
        tid = _seed.create_ideate_task(wire_client)
        responses = _claim_concurrently(wire_client, tid, _N_CLAIMERS)
        successes, _ = _classify(responses)
        assert len(successes) == 1
        seen_tokens.append(successes[0].json()["token"])
    assert seen_tokens[0] != seen_tokens[1], (
        f"§3.1 + §3.2 violated: contended claims on two tasks returned "
        f"the same winning token {seen_tokens[0]!r}"
    )


# Mark types we expect from the local IUT; intentionally narrow so a
# regression that quietly emits wrong-token (or any other code) for
# a contended claim is caught.
_PROBLEM_TYPE: Callable[[httpx.Response], str] = lambda r: r.json().get("type", "")  # noqa: E731
