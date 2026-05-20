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
from .memory import InMemoryControlPlaneStore
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
from .postgres import PostgresControlPlaneStore
from .store import ControlPlaneStore

__all__ = [
    "ControlPlaneClient",
    "ControlPlaneStore",
    "ExperimentLease",
    "InMemoryControlPlaneStore",
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
    "PostgresControlPlaneStore",
    "RegisterExperimentRequest",
    "RegisteredExperiment",
    "raise_for_control_plane_envelope",
]
