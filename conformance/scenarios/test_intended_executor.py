"""Intended-executor flow-through conformance scaffolds (chapter 02 §5.1).

The full scenario suite for this group lands in Phase 12a-3 wave 6.
This file's scaffold tests anchor the chapter-9 §5 ``Intended-executor
flow-through`` index entry to a citing test per the
``check_citations.py`` three-legged traceability rule.

The ``intended_executor`` field on ``Idea`` is wire-visible through
the existing ``create_idea`` / ``read_idea`` endpoints (12a-3 wave 1
added the field to ``idea.schema.json``), so the scaffold could in
principle run today against the reference IUT. The skip annotations
defer until the matching wave-3 wire surface (``POST /tasks`` with
operator-driven ``kind=execution``) lands; the wave-6 expansion will
exercise both auto-dispatch and operator-override paths end-to-end.
"""

from __future__ import annotations

import pytest

from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = "Intended-executor flow-through"


@pytest.mark.skip(reason="wave 6 lands the auto-dispatch flow-through assertion")
def test_idea_intended_executor_copied_to_task_target(
    wire_client: WireClient,
) -> None:
    """spec/v0/02-data-model.md §5.1 — intended_executor flows to task.target.

    Per chapter 02 §5.1 + chapter 03 §6.2 decision-type 2: an idea
    with ``intended_executor`` set produces an execution task whose
    ``target`` is copied from the idea. Claim eligibility resolves at
    claim time per chapter 04 §3.5 step 3, so a worker outside the
    target's worker / group MUST receive 403 worker-not-eligible.
    """
    _ = wire_client


@pytest.mark.skip(reason="wave 3 lifts admin authority on create_task(kind=execution)")
def test_admin_target_override_wins_over_intended_executor(
    wire_client: WireClient,
) -> None:
    """spec/v0/02-data-model.md §5.1 — admin target override.

    Per chapter 03 §6.5 + chapter 07 §2.1: an admin-driven
    ``create_task(kind=execution)`` with an explicit ``target``
    overrides the referenced idea's ``intended_executor``. Pre-12a-3
    this was orchestrators-only; 12a-3 broadens the authority gate to
    admins OR orchestrators.
    """
    _ = wire_client
