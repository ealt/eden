"""State-sync conformance — chapter 11 §3.

After a `terminate_experiment` against the task-store-server, the
control plane's `last_known_state` converges to `"terminated"`
within bounded staleness; `acquire_lease` triggers an on-demand
refresh; persistent task-store-server unreachable surfaces a
`warnings` entry on `read_experiment_metadata`.

The state-sync poller's behavior is wire-observable through
`read_experiment_metadata.last_known_state` + the `warnings`
array. The conformance fixture runs the control plane WITHOUT the
`--task-store-url` flag (the conformance suite has no task-store-
server bound to the control plane), so the §3.2 polling path is
not exercised in v0 conformance. The contract is asserted by the
in-process tests at
`reference/services/control-plane/tests/test_state_sync.py`.
"""

from __future__ import annotations

import pytest
from eden_control_plane import ControlPlaneClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = "State synchronization"


def test_register_experiment_starts_running(
    control_plane_client: ControlPlaneClient,
) -> None:
    """spec/v0/11-control-plane.md §3.2 — fresh registry entry is `running`.

    The wire-observable initial state per §2.1 is `"running"`; the
    §3 sync mechanism only mutates this projection on observed
    transitions from the task-store-server. With no poller bound,
    the initial value is observable but no transition is asserted.
    """
    entry = control_plane_client.register_experiment(
        "exp-a", "file:///etc/a.yaml"
    )
    assert entry.last_known_state == "running"


@pytest.mark.skip(
    reason=(
        "Driving the §3.2 state-sync poller through the conformance "
        "fixture requires a task-store-server alongside the control "
        "plane (the poller's `make_task_store_reader` consults "
        "`read_experiment` on it). The fixture currently runs the "
        "control plane stand-alone. The bounded-staleness, on-demand "
        "refresh, and §3.4 warnings contracts are asserted by "
        "reference/services/control-plane/tests/test_state_sync.py."
    )
)
def test_bounded_staleness_terminated_transition() -> None:
    """spec/v0/11-control-plane.md §3.2 — last_known_state converges to terminated.

    After a `terminate_experiment` against the task-store-server,
    the poller's next tick MUST update `last_known_state` to
    `"terminated"`. Bounded by one polling interval.
    """


@pytest.mark.skip(
    reason="See state_synchronization skip rationale above."
)
def test_acquire_lease_triggers_refresh() -> None:
    """spec/v0/11-control-plane.md §3.3 — acquire_lease refreshes state.

    The §3.3 on-demand refresh: `acquire_lease` MUST trigger a
    one-shot state read so a freshly-leased experiment carries
    up-to-date `last_known_state` regardless of polling cadence.
    """


@pytest.mark.skip(
    reason="See state_synchronization skip rationale above."
)
def test_persistent_unreachable_emits_warning() -> None:
    """spec/v0/11-control-plane.md §3.4 — stale-state warning at threshold.

    When the per-experiment consecutive-failure counter crosses the
    deployment-configured threshold, subsequent
    `read_experiment_metadata` responses MUST include a `warnings`
    entry naming the staleness.
    """
