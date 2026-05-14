"""Experiment-durability placeholder — chapter 01 §13.

Scenario authoring for the chapter 01 §13 aggregate experiment-
durability invariant is **deferred to a follow-up chunk** per the
Phase 12a-1g plan (operator decision §2.4). A complete suite would
drive a "stop-stack / kill-mount / restart / replay" harness against
any conforming IUT.

This module exists so chapter 9 §5's "Experiment durability" index row
has at least one citing test. The single skipped test below is the
placeholder; future scenarios slot in here and the skip is removed.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = "Experiment durability"


@pytest.mark.skip(
    reason="chapter 01 §13 scenario authoring deferred to a Phase-12a-1g "
    "follow-up; the row in chapter 9 §5 anchors the citation."
)
def test_aggregate_durability_placeholder() -> None:
    """spec/v0/01-concepts.md §13 — aggregate experiment-durability invariant.

    The chapter 01 §13 invariant requires that the union of protocol-
    owned state survive process / host / substrate restart. A wire-only
    suite cannot drive a substrate restart by itself; codifying the
    invariant as a scenario needs harness extensions to stop and
    restart the IUT's underlying substrate between assertions. That
    work is the deferred follow-up; this placeholder ensures the
    chapter 9 §5 index row resolves through the citation check.
    """
