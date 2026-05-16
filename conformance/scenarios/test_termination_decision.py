"""Termination-decision conformance scaffolds (chapter 03 §6.2 decision-type 0).

The full scenario suite for this group lands in Phase 12a-3 wave 6.
This file's scaffold tests anchor the chapter-9 §5 ``Termination
decision`` index entry to a citing test per the
``check_citations.py`` three-legged traceability rule (group identity
+ MUST citation + group-relevance). Each scaffold is ``pytest.mark.skip``-ed
pending the wire endpoint that lands in wave 3 (``POST /terminate``)
and the policy-driven orchestrator branch that lands in wave 4.
"""

from __future__ import annotations

import pytest

from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = "Termination decision"


@pytest.mark.skip(
    reason="wave 3 wires POST /terminate; wave 4 lands the policy-driven branch"
)
def test_terminate_decision_emits_experiment_terminated(
    wire_client: WireClient,
) -> None:
    """spec/v0/03-roles.md §6.2 — termination decision commits state + event.

    Per chapter 03 §6.2 decision-type 0: when the termination policy
    returns ``Terminate(reason)``, the orchestrator MUST atomically
    transition the experiment's ``state`` from ``"running"`` to
    ``"terminated"`` and append ``experiment.terminated`` with the
    policy's reason. The four operational decisions (1-4 of §6.2) MUST
    NOT run on a terminated experiment; the integration decision (4)
    continues to drain. The scaffold's full assertion shape lands in
    wave 6.
    """
    _ = wire_client  # consumed by the future wave-6 implementation


@pytest.mark.skip(reason="wave 4 lands the policy-fault path")
def test_terminate_policy_raises_emits_policy_error(
    wire_client: WireClient,
) -> None:
    """spec/v0/03-roles.md §6.2 — policy fault tolerance.

    Per chapter 03 §6.2 decision-type 0 fault-tolerance subsection:
    a termination policy that raises MUST be treated as ``Continue``
    (the operational decisions still run) and the orchestrator MUST
    emit ``experiment.policy_error``. A failing policy is a config
    bug, not a deployment failure.
    """
    _ = wire_client
