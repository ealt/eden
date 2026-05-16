"""Experiment-lifecycle conformance scaffolds (chapter 02 §2.5).

The full scenario suite for this group lands in Phase 12a-3 wave 6.
This file's scaffold tests anchor the chapter-9 §5 ``Experiment
lifecycle`` index entry to a citing test per the
``check_citations.py`` three-legged traceability rule. Each scaffold
is ``pytest.mark.skip``-ed pending the wire endpoint that lands in
wave 3 (``POST /terminate`` + ``GET /state``).
"""

from __future__ import annotations

import pytest
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = "Experiment lifecycle"


@pytest.mark.skip(reason="wave 3 wires POST /terminate + GET /state")
def test_terminated_experiment_rejects_create_task(
    wire_client: WireClient,
) -> None:
    """spec/v0/02-data-model.md §2.5 — terminated rejects task creation.

    Per chapter 02 §2.5: the task store MUST reject every ``create_task``
    op against a terminated experiment with
    ``eden://error/illegal-transition``. The full assertion shape
    (404 path response + same response shape across all three kinds)
    lands in wave 6.
    """
    _ = wire_client


@pytest.mark.skip(reason="wave 3 wires POST /terminate + GET /state")
def test_terminated_experiment_rejects_claim(
    wire_client: WireClient,
) -> None:
    """spec/v0/02-data-model.md §2.5 — terminated rejects claim of pending.

    Per chapter 02 §2.5 and chapter 04 §3.5 step 0: a pending task that
    exists at termination time cannot be claimed by any worker after
    termination; the pending row remains in storage but is unreachable.
    """
    _ = wire_client


@pytest.mark.skip(reason="wave 3 wires POST /terminate; drain semantics tested in wave 6")
def test_integration_drains_after_termination(
    wire_client: WireClient,
) -> None:
    """spec/v0/02-data-model.md §2.5 — integration drain on terminated experiment.

    Per chapter 02 §2.5: variants in ``status == "success"`` without
    ``variant_commit_sha`` at termination time MUST get integrated;
    the ``variant_commit_sha`` is written and ``variant.integrated``
    fires even after ``experiment.terminated``. The §6.4.1
    race-resolution contract permits either event ordering.
    """
    _ = wire_client
