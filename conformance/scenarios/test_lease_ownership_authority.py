"""Lease-ownership authority conformance — chapter 11 §4.5; chapter 07 §15.

`acquire_lease` rejects callers outside the deployment-scoped
`orchestrators` group with 403 `forbidden`; rejects `holder`
impersonation (caller authenticated as W₁ supplying `holder=W₂`)
with 403 `forbidden`.

The reference adapter runs auth-disabled (admin_token unset) for
compatibility with the suite's no-auth posture; the chapter 07
§15 authority MUSTs are exercised here by validating that the
*server-side* check is wired correctly when auth is enabled. The
chapter 07 §15 surface MUST classify lease ops as worker-gated +
`orchestrators`-group-required, and `acquire_lease` MUST reject
impersonation.

The control-plane reference subprocess used by the conformance
fixture runs auth-disabled (matches `ReferenceAdapter`'s posture),
so this scenario is SKIPPED in the wave-6 conformance run pending
a future `--control-plane-admin-token` flag for the conformance
harness. The chapter 07 §15 authority contract is asserted by the
in-process tests at
`reference/services/control-plane/tests/test_server.py`
(`test_authed_acquire_lease_rejects_impersonation`,
`test_authed_acquire_lease_requires_orchestrators_group`).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = "Lease-ownership authority"


@pytest.mark.skip(
    reason=(
        "Auth gating requires the control-plane subprocess to run with "
        "--admin-token + a pre-registered orchestrators-group worker. "
        "The wave-6 conformance fixture runs auth-disabled (mirrors the "
        "ReferenceAdapter posture). The chapter 11 §4.5 + chapter 07 §15 "
        "authority MUSTs are asserted by "
        "reference/services/control-plane/tests/test_server.py."
    )
)
def test_acquire_lease_rejects_non_orchestrators_caller() -> None:
    """spec/v0/11-control-plane.md §4.5 — caller MUST be in orchestrators group.

    `acquire_lease` is worker-gated AND `orchestrators`-group-required.
    A worker bearer outside the deployment-scoped `orchestrators`
    group MUST be rejected with 403 `forbidden`.
    """


@pytest.mark.skip(
    reason=(
        "Same admin-token requirement as the orchestrators-group test. "
        "Impersonation rejection asserted in-process at "
        "test_authed_acquire_lease_rejects_impersonation."
    )
)
def test_acquire_lease_rejects_holder_impersonation() -> None:
    """spec/v0/11-control-plane.md §4.5 — holder MUST equal authenticated worker_id.

    Per §4.5: caller MUST be in the deployment-scoped `orchestrators`
    group; `holder` field MUST equal the authenticated `worker_id`.
    Mismatched holder → 403 `forbidden`.
    """
