"""Group-membership authority rejections — chapter 07 §13.3.

Every chapter-7 §7 entry MUST be observed at least once by the
vocabulary-closure assertion in
:mod:`conformance.scenarios.test_error_vocabulary`. This file pins
the wire-observable trigger paths for ``eden://error/forbidden``:
a registered worker bearer hitting an endpoint that requires
membership in a group it is NOT a member of returns 403 with the
``forbidden`` envelope.

The reference adapter runs auth-enabled (post-#148), so these
scenarios populate ``session_observed_problem_types`` through the
default :class:`WireClient` and unblock
``test_v0_vocabulary_each_observed_at_least_once``. Auth-enabled
scenarios that previously observed ``forbidden`` lived in
``test_worker_auth_enabled.py`` but used a raw ``httpx.Client``
(its own dedicated auth-enabled subprocess), so its problem+json
responses never flowed into the session-scoped observation
accumulator that ``WireClient`` populates.
"""

from __future__ import annotations

import pytest
from conformance.harness import _seed
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = "Worker auth"


def test_accept_by_non_orchestrator_returns_403_forbidden(
    wire_client: WireClient,
) -> None:
    """spec/v0/07-wire-protocol.md §13.3 — non-``orchestrators`` accept → 403 forbidden.

    ``POST /tasks/{T}/accept`` is gated on the ``orchestrators``
    group (the §13.3 dispatcher's group-membership check). A
    registered worker that is NOT in ``orchestrators`` (the
    default-fixture ``test-worker``) MUST receive 403
    ``eden://error/forbidden``.
    """
    tid = _seed.create_ideation_task(wire_client)
    claim = _seed.claim(wire_client, tid, worker_id="test-worker")
    _seed.submit_idea(wire_client, tid, worker_id=claim["worker_id"])
    r = wire_client.post(
        wire_client.tasks_path(tid, "/accept"),
        as_worker="test-worker",
    )
    assert r.status_code == 403, r.text
    assert r.json().get("type") == "eden://error/forbidden", r.text


def test_terminate_by_neither_group_returns_403_forbidden(
    wire_client: WireClient,
) -> None:
    """spec/v0/07-wire-protocol.md §13.3 — terminate by neither group → 403 forbidden.

    ``POST /v0/experiments/{E}/terminate`` is gated on the ``admins``
    OR ``orchestrators`` group (the §13.3 dispatcher's group-membership
    check; issue #256). A registered worker in NEITHER group MUST
    receive 403 ``eden://error/forbidden``.
    """
    r = wire_client.post(
        wire_client.terminate_path(),
        json={"reason": "neither-group probe"},
        as_worker="test-worker",
    )
    assert r.status_code == 403, r.text
    assert r.json().get("type") == "eden://error/forbidden", r.text
