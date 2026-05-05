"""worker_id MUST NOT substitute for the claim token — chapter 04 §3.2 line 74.

The §3.2 bullet on ``worker_id`` reads:

> ``worker_id`` — an identifier the worker supplies at claim time.
> The task store MAY record it for audit but MUST NOT use it as a
> substitute for the token when authorizing subsequent operations.

The wire-observable consequence: a token produced by claim X
authorizes ONLY task X, even if the same worker_id holds another
claim on task Y. A regression that fell back to worker_id matching
when token lookup failed would let token(X) authorize submit(Y) so
long as the same worker_id appears on both claims; this scenario
catches that.

The existing ``test_wrong_token_rejected`` covers a
syntactically-distinct token; this scenario covers the subtler case
where the token IS valid (for a different task claimed by the same
worker) but the authorization rule MUST still reject it on the
target task.

Sits in the 'Claim tokens' group — same primary citation home as
the other §3.2 / §3.3 token-authorization tests.
"""

from __future__ import annotations

import pytest
from conformance.harness import _seed
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = 'Claim tokens'


def test_token_from_other_task_rejected_even_with_same_worker_id(
    wire_client: WireClient,
) -> None:
    """spec/v0/04-task-protocol.md §3.2 — worker_id is not a substitute for the token.

    Build the regression scenario from §3.2 line 74:

    1. Worker W claims task A → token T_A.
    2. Worker W claims task B → token T_B.
    3. Submit task B presenting T_A. The token IS valid (it
       authorizes A) and the worker_id IS valid (W claimed both A
       and B), but the token does not match B's claim. §3.2 +
       §3.3 require rejection: only T_B authorizes B.

    A naïve implementation that fell back to worker_id matching
    when token lookup mismatched would accept this submit; the
    spec MUSTs reject it.
    """
    same_worker_id = "w-shares-both-claims"

    task_a = _seed.create_ideate_task(wire_client)
    task_b = _seed.create_ideate_task(wire_client)

    claim_a = _seed.claim(wire_client, task_a, worker_id=same_worker_id)
    claim_b = _seed.claim(wire_client, task_b, worker_id=same_worker_id)

    token_a = claim_a["token"]
    token_b = claim_b["token"]
    assert isinstance(token_a, str) and token_a
    assert isinstance(token_b, str) and token_b
    # Sanity: §3.2's uniqueness MUST means the two tokens differ.
    # Without that we couldn't probe the substitution rule.
    assert token_a != token_b, (
        "§3.2 uniqueness MUST violated: two claims by the same worker "
        "returned identical tokens; cannot test the substitution rule"
    )
    # Sanity: claim_a's recorded worker_id matches what we sent.
    # Both claims being held by the same worker_id is what makes the
    # regression test meaningful.
    assert claim_a["worker_id"] == same_worker_id
    assert claim_b["worker_id"] == same_worker_id

    # The probe: submit task B using task A's token.
    resp = _seed.submit_idea(wire_client, task_b, token=token_a)

    assert resp.status_code == 403, (
        f"§3.2 line 74 violated: submit on task B with token from task A "
        f"(both claimed by worker_id={same_worker_id!r}) returned "
        f"{resp.status_code} (expected 403). Body: {resp.text}"
    )
    body = resp.json()
    assert body.get("type") == "eden://error/wrong-token", (
        f"§3.2 line 74 violated: rejection type was {body.get('type')!r} "
        f"(expected eden://error/wrong-token)"
    )

    # Confirm the converse: token B against task B is honored. If
    # this fails the test environment is broken, not the IUT.
    sanity = _seed.submit_idea(wire_client, task_b, token=token_b)
    assert sanity.status_code == 200, (
        f"sanity check failed: token B on task B returned "
        f"{sanity.status_code} (expected 200). Body: {sanity.text}"
    )
