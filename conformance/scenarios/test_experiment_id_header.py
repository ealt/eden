"""Experiment-id header disagreement — chapter 07 §1.3."""

from __future__ import annotations

import pytest
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = 'Experiment-id header disagreement'


def test_header_disagrees_returns_400(wire_client: WireClient) -> None:
    """spec/v0/07-wire-protocol.md §1.3 — header/path mismatch returns 400 experiment-id-mismatch."""  # noqa: E501
    r = wire_client.get(
        wire_client.tasks_path(), headers={"X-Eden-Experiment-Id": "other-experiment"}
    )
    assert r.status_code == 400
    assert r.json().get("type") == "eden://error/experiment-id-mismatch"
