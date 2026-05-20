"""Pydantic models and HTTP client for the EDEN control plane.

Mirrors `spec/v0/11-control-plane.md` (chapter 11) and the wire bindings
in `spec/v0/07-wire-protocol.md` §15.
"""

from .client import ControlPlaneClient
from .errors import (
    LeaseError,
    LeaseExpired,
    LeaseHeldByOther,
    LeaseInstanceMismatch,
    LeaseNotHeld,
    raise_for_control_plane_envelope,
)
from .models import (
    ExperimentLease,
    LastKnownState,
    LeaseAcquireRequest,
    LeaseReleaseRequest,
    LeaseRenewRequest,
    ListExperimentsResponse,
    ListLeasesResponse,
    RegisteredExperiment,
    RegisterExperimentRequest,
)

__all__ = [
    "ControlPlaneClient",
    "ExperimentLease",
    "LastKnownState",
    "LeaseAcquireRequest",
    "LeaseError",
    "LeaseExpired",
    "LeaseHeldByOther",
    "LeaseInstanceMismatch",
    "LeaseNotHeld",
    "LeaseReleaseRequest",
    "LeaseRenewRequest",
    "ListExperimentsResponse",
    "ListLeasesResponse",
    "RegisterExperimentRequest",
    "RegisteredExperiment",
    "raise_for_control_plane_envelope",
]
