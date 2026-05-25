"""Worker authentication / cross-application claim — chapter 04 §3.3, §4.1.

12a-1 retired the per-claim opaque token in favor of identity-keyed
ownership. The wire-observable consequence: any client
authenticating as the same ``worker_id`` may submit a claim taken by
another client authenticating as that same worker_id. The token
half disappears; the worker_id half is the contract.

This file pins the cross-application-claim MUST from plan §6.4:

- Two distinct WireClient instances (modelling distinct
  applications / processes / machines) authenticated as the same
  ``worker_id`` can collaborate on a claim's submit step: client A
  claims, client B submits — without ever exchanging an
  authentication artifact through the claim object.

The conformance adapter runs the IUT auth-enabled (per-worker
bearer tokens). Two distinct WireClient instances bind to the same
IUT and share the worker's bearer via
``copy_worker_bearers_from``; the §13.2 bearer scheme is the
identity-claim surface.
"""

from __future__ import annotations

import pytest
from conformance.harness import _seed
from conformance.harness.adapter import IutHandle
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = 'Worker auth'


def test_two_clients_share_a_claim_via_worker_identity(
    wire_client: WireClient, iut: IutHandle
) -> None:
    """spec/v0/04-task-protocol.md §3.3 + §4.1 — same worker_id across clients shares ownership.

    Client A claims as worker ``eric``; client B (a fresh WireClient
    against the same IUT) submits as ``eric``. The submit MUST
    succeed because §4.1's claim-match is keyed on the recorded
    ``worker_id``, NOT on a per-claim artifact passed between
    clients.

    A regression that retained a hidden per-claim token — even
    server-side — would break this scenario because client B has no
    way to obtain it.
    """
    wid = _seed.fresh_worker_id("eric")
    _seed.register_worker(wire_client, wid)
    tid = _seed.create_ideation_task(wire_client)

    # Client A: claim.
    claim = _seed.claim(wire_client, tid, worker_id=wid)
    assert claim["worker_id"] == wid

    # Client B: a fresh WireClient pointed at the same IUT, sharing
    # ``wid``'s bearer (the §13.2 cross-application identity surface).
    with WireClient(
        base_url=iut.base_url,
        experiment_id=iut.experiment_id,
        extra_headers=iut.extra_headers,
    ) as client_b:
        client_b.copy_worker_bearers_from(wire_client)
        r = _seed.submit_idea(client_b, tid, worker_id=wid)
    assert r.status_code == 200, r.text
    task = _seed.read_task(wire_client, tid)
    assert task["state"] == "submitted"
    assert task.get("submitted_by") == wid


def test_two_clients_disagreeing_on_worker_id_rejected(
    wire_client: WireClient, iut: IutHandle
) -> None:
    """spec/v0/04-task-protocol.md §4.1 — submit by a different worker_id raises WrongClaimant.

    Cross-application claim works only when both clients authenticate
    as the same worker. A client B authenticated as a different
    registered worker MUST fail the §4.1 atomic claim-match.
    """
    claimant = _seed.fresh_worker_id("claimant")
    intruder = _seed.fresh_worker_id("intruder")
    _seed.register_worker(wire_client, claimant)
    _seed.register_worker(wire_client, intruder)
    tid = _seed.create_ideation_task(wire_client)
    _seed.claim(wire_client, tid, worker_id=claimant)

    with WireClient(
        base_url=iut.base_url,
        experiment_id=iut.experiment_id,
        extra_headers=iut.extra_headers,
    ) as client_b:
        client_b.copy_worker_bearers_from(wire_client)
        r = _seed.submit_idea(client_b, tid, worker_id=intruder)
    assert r.status_code == 403, r.text
    assert r.json().get("type") == "eden://error/wrong-claimant"
