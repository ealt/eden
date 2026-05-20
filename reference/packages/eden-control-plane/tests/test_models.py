"""Pydantic-layer tests for control-plane wire shapes.

Covers `RegisteredExperiment`, request bodies, and the two list-response
wrappers. None of these shapes have a normative JSON Schema today
(chapter 07 §15 documents them as JSON examples); this test file is
the Pydantic-side anchor for round-trip stability.
"""

from __future__ import annotations

from typing import Any

import pytest
from eden_control_plane import (
    ExperimentLease,
    LeaseAcquireRequest,
    LeaseReleaseRequest,
    LeaseRenewRequest,
    ListExperimentsResponse,
    ListLeasesResponse,
    RegisteredExperiment,
    RegisterExperimentRequest,
)
from pydantic import ValidationError

LEASE_PAYLOAD: dict[str, Any] = {
    "lease_id": "lease-abc-123",
    "experiment_id": "exp-1",
    "holder": "auto-orchestrator-1",
    "holder_instance": "uuid-aaaa",
    "acquired_at": "2026-05-19T12:00:00Z",
    "expires_at": "2026-05-19T12:00:30Z",
    "renewed_at": "2026-05-19T12:00:00Z",
}


REGISTRY_PAYLOAD: dict[str, Any] = {
    "experiment_id": "exp-1",
    "config_uri": "https://example.test/exp-1/config.yaml",
    "created_at": "2026-05-19T12:00:00Z",
    "last_known_state": "running",
    "lease": None,
}


def test_registered_experiment_accepts_lease_null() -> None:
    entry = RegisteredExperiment.model_validate(REGISTRY_PAYLOAD)
    assert entry.lease is None
    assert entry.warnings is None


def test_registered_experiment_rejects_missing_lease_key() -> None:
    """Codex round 7 MINOR: `lease` is REQUIRED-but-nullable per §4.4.

    A wire payload that omits the `lease` key entirely MUST be
    rejected — the spec MUST is "present and null" when no active
    lease. Distinct from `"lease": null` (compliant) which the
    prior test accepts.
    """
    payload = {k: v for k, v in REGISTRY_PAYLOAD.items() if k != "lease"}
    with pytest.raises(ValidationError):
        RegisteredExperiment.model_validate(payload)


def test_registered_experiment_with_lease_and_warnings() -> None:
    payload = {
        **REGISTRY_PAYLOAD,
        "lease": LEASE_PAYLOAD,
        "warnings": ["state-sync-stale: last successful read at 2026-05-19T11:55:00Z"],
    }
    entry = RegisteredExperiment.model_validate(payload)
    assert entry.lease is not None
    assert entry.lease.lease_id == "lease-abc-123"
    assert entry.warnings == [
        "state-sync-stale: last successful read at 2026-05-19T11:55:00Z"
    ]


def test_registered_experiment_round_trips() -> None:
    entry = RegisteredExperiment.model_validate(
        {**REGISTRY_PAYLOAD, "lease": LEASE_PAYLOAD}
    )
    dumped = entry.model_dump(mode="json", exclude_none=True)
    again = RegisteredExperiment.model_validate(dumped)
    assert again == entry


def test_last_known_state_enum_enforced() -> None:
    with pytest.raises(ValidationError):
        RegisteredExperiment.model_validate(
            {**REGISTRY_PAYLOAD, "last_known_state": "starting"}
        )


def test_config_uri_must_be_a_real_uri() -> None:
    with pytest.raises(ValidationError):
        RegisteredExperiment.model_validate(
            {**REGISTRY_PAYLOAD, "config_uri": "not a uri with spaces"}
        )


def test_register_experiment_request_validates_uri() -> None:
    body = RegisterExperimentRequest(
        experiment_id="exp-1", config_uri="file:///path/to/config.yaml"
    )
    assert body.experiment_id == "exp-1"
    with pytest.raises(ValidationError):
        RegisterExperimentRequest(experiment_id="", config_uri="https://x/")


def test_lease_acquire_request_requires_holder_and_holder_instance() -> None:
    with pytest.raises(ValidationError):
        LeaseAcquireRequest.model_validate({"holder_instance": "uuid-1"})  # missing holder
    with pytest.raises(ValidationError):
        LeaseAcquireRequest.model_validate({"holder": "w1", "holder_instance": ""})
    LeaseAcquireRequest(holder="auto-orchestrator-1", holder_instance="uuid-1")


@pytest.mark.parametrize("cls", [LeaseRenewRequest, LeaseReleaseRequest])
def test_lease_renew_release_require_holder_instance(
    cls: type[LeaseRenewRequest | LeaseReleaseRequest],
) -> None:
    with pytest.raises(ValidationError):
        cls(holder_instance="")
    cls(holder_instance="uuid-1")


def test_list_experiments_response_round_trips() -> None:
    body = ListExperimentsResponse.model_validate(
        {"experiments": [REGISTRY_PAYLOAD]}
    )
    assert len(body.experiments) == 1
    assert body.experiments[0].experiment_id == "exp-1"


def test_list_leases_response_round_trips() -> None:
    body = ListLeasesResponse.model_validate({"leases": [LEASE_PAYLOAD]})
    assert len(body.leases) == 1
    assert isinstance(body.leases[0], ExperimentLease)


def test_strict_mode_rejects_int_for_str() -> None:
    """All control-plane models use strict mode — non-string ids must reject."""
    with pytest.raises(ValidationError):
        ExperimentLease.model_validate({**LEASE_PAYLOAD, "lease_id": 12345})
