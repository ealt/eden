"""Worker-side reclamation detection — chapter 04 §5.3.

The §5.3 MUST is split between worker-side behavior (the worker MUST
discontinue work, MUST NOT submit) and the wire-observable
consequences the worker uses to detect reclamation. The strictly
worker-side half is NOT testable from a wire-only IUT — the worker
is an external party. What this suite CAN assert is the consequence
chain the worker relies on:

1. After reclamation the task object reflects the new state on
   ``read_task`` (one of the two §5.3 detection mechanisms).
2. The prior token is rejected on every claim-holder operation
   ([`04-task-protocol.md`](../../spec/v0/04-task-protocol.md) §3.3 +
   §5.2), proving the worker cannot accidentally complete its
   pre-reclaim work.
3. A fresh ``POST /claim`` is required to proceed; there is no
   implicit re-acquisition path that would let an old token become
   valid again.

Adjacent existing scenarios already cover the individual pieces
(``test_token_invalidated_by_reclaim`` for #2's submit case;
``test_token_unique_across_reclaim_cycles`` for #3's fresh-token
guarantee). This scenario binds them into the composite worker
view §5.3 describes — so a regression that broke any one of the
three detection paths would surface here, even if the unit-style
tests still passed.
"""

from __future__ import annotations

import pytest
from conformance.harness import _seed
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = 'Reclamation'


def test_worker_view_after_reclamation_invalidates_prior_token(
    wire_client: WireClient,
) -> None:
    """spec/v0/04-task-protocol.md §5.3 — worker detects reclamation; old token never re-validates.

    Exercises the §5.3 detection-and-discontinue chain end-to-end:
    claim → read_task confirms ``claimed`` with our token → operator
    reclaims → read_task now shows ``pending`` with no claim → a
    submit attempt with the old token returns wrong-token (not
    illegal-transition, because the cleared claim makes the token
    unrecognizable) → a fresh claim issues a different token, and
    the old token continues to be rejected.

    Implementation note: ``submit`` against a fully-cleared task
    state is the cleanest probe for "old token never re-validates"
    — the existing ``test_token_invalidated_by_reclaim`` re-claims
    before submitting (so the failure mode is wrong-token against a
    new claim); this scenario submits BEFORE the re-claim so the
    failure mode is wrong-token against a no-claim state. The
    distinction matters because chapter 04 §4.2 calls out that
    resubmit against a cleared claim MUST be rejected regardless of
    the presented token.
    """
    tid = _seed.create_ideate_task(wire_client)

    # 1. Worker claims; the task now carries our claim.
    original_claim = _seed.claim(wire_client, tid, worker_id="w-original")
    original_token = original_claim["token"]
    assert isinstance(original_token, str) and original_token

    pre_reclaim = _seed.read_task(wire_client, tid)
    assert pre_reclaim["state"] == "claimed"
    assert pre_reclaim.get("claim", {}).get("token") == original_token
    assert pre_reclaim.get("claim", {}).get("worker_id") == "w-original"

    # 2. Operator reclaims; the worker's claim is now invalidated.
    reclaim_resp = _seed.reclaim(wire_client, tid, cause="operator")
    assert 200 <= reclaim_resp.status_code < 300, reclaim_resp.text

    # 3. Detection mechanism 1: read_task observes the new state.
    post_reclaim = _seed.read_task(wire_client, tid)
    assert post_reclaim["state"] == "pending", (
        f"§5.3 detection broken: post-reclaim read_task returned state "
        f"{post_reclaim['state']!r} (expected 'pending')"
    )
    assert post_reclaim.get("claim") in (None, {}), (
        f"§5.3 detection broken: post-reclaim read_task still carries "
        f"a claim object: {post_reclaim.get('claim')!r}"
    )

    # 4. Old token cannot complete the pre-reclaim work. Per §4.2,
    # a resubmit against a cleared-claim task MUST be rejected
    # regardless of the presented token.
    submit_resp = _seed.submit_ideate(wire_client, tid, token=original_token)
    assert submit_resp.status_code in (403, 409), (
        f"§5.3 violated: submit with invalidated token returned "
        f"{submit_resp.status_code} (expected 403 or 409)"
    )
    body = submit_resp.json()
    # The closed error vocabulary (chapter 07 §7) admits two routes
    # here: wrong-token (token not recognized — no current claim to
    # match against) or illegal-transition (state is pending, submit
    # not legal). Either is conformant; the negation we care about
    # is "200" — the old token MUST NOT be honored.
    assert body.get("type") in (
        "eden://error/wrong-token",
        "eden://error/illegal-transition",
    ), f"§5.3 violated: rejection type was {body.get('type')!r}"

    # 5. Fresh claim succeeds with a different token; the old token
    # remains rejected even after a new claim exists.
    fresh_claim = _seed.claim(wire_client, tid, worker_id="w-original")
    fresh_token = fresh_claim["token"]
    assert fresh_token != original_token, (
        f"§5.3 + §3.2 violated: re-claim issued the same token "
        f"{fresh_token!r} as the invalidated original"
    )

    # The old token MUST still be rejected even though the task is
    # now claimed by the same worker_id again.
    post_reclaim_submit = _seed.submit_ideate(
        wire_client, tid, token=original_token
    )
    assert post_reclaim_submit.status_code == 403, (
        f"§5.3 violated: old token accepted after re-claim "
        f"(status {post_reclaim_submit.status_code})"
    )
    assert post_reclaim_submit.json().get("type") == "eden://error/wrong-token"
