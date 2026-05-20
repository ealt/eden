"""Pydantic bindings for the control-plane wire shapes.

Mirrors `spec/v0/11-control-plane.md` §2.1 (experiment registry entry),
§4.2 (`ExperimentLease`), and the request/response shapes in
`spec/v0/07-wire-protocol.md` §15.

`ExperimentLease` mirrors `spec/v0/schemas/lease.schema.json` and is
the only protocol object in this module that has a normative JSON
Schema; the others are wire shapes documented in chapter 07 §15 as
JSON examples.
"""

from __future__ import annotations

from typing import Annotated, Literal

from eden_contracts import DateTimeStr, WorkerId
from pydantic import BaseModel, ConfigDict, Field

from ._common import ConfigUriStr

LastKnownState = Literal["running", "terminated"]
"""Cached projection of `experiment.state` per chapter 11 §2.1.

Mirrors the authoritative `02-data-model.md` §2.5 vocabulary. The
control plane maintains this as an eventually-consistent projection
of the task-store-server's value, refreshed per chapter 11 §3.
"""


class ExperimentLease(BaseModel):
    """A per-experiment, time-bounded ownership claim.

    Issued by the control plane per chapter 11 §4 and observed through
    the wire endpoints in chapter 07 §15.2. Schema:
    `spec/v0/schemas/lease.schema.json`.
    """

    model_config = ConfigDict(strict=True, extra="allow")

    lease_id: Annotated[str, Field(min_length=1)]
    experiment_id: Annotated[str, Field(min_length=1)]
    holder: WorkerId
    holder_instance: Annotated[str, Field(min_length=1)]
    acquired_at: DateTimeStr
    expires_at: DateTimeStr
    renewed_at: DateTimeStr


class RegisteredExperiment(BaseModel):
    """One row of the chapter 11 §2 experiment registry.

    Returned by `register_experiment`, `list_experiments`,
    `read_experiment_metadata`. The `warnings` array surfaces
    operator-visible state-sync degradation per chapter 11 §3.4; it
    is OPTIONAL and absent when no degradation has occurred.
    """

    model_config = ConfigDict(strict=True, extra="allow")

    experiment_id: Annotated[str, Field(min_length=1)]
    config_uri: ConfigUriStr
    created_at: DateTimeStr
    last_known_state: LastKnownState
    # Codex round 7 MINOR: `lease` is REQUIRED-but-nullable per
    # chapter 11 §4.4. Removing the default makes Pydantic reject
    # a wire payload that omits the key entirely — distinguishing
    # "present and null" (compliant) from "absent" (non-compliant).
    # See `_dump_registry_entry` on the server side: it always
    # emits the key with an explicit `null` when no active lease.
    lease: ExperimentLease | None
    warnings: list[str] | None = None
    """OPTIONAL — operator-visible state-sync warnings per §3.4.

    Absent when no degradation; present (and non-empty) when the
    control plane has surfaced at least one warning for this
    experiment. The warning text is human-readable; clients SHOULD
    forward the strings to operators unchanged.
    """


class RegisterExperimentRequest(BaseModel):
    """Body for `POST /v0/control/experiments`."""

    model_config = ConfigDict(strict=True, extra="allow")

    experiment_id: Annotated[str, Field(min_length=1)]
    config_uri: ConfigUriStr


class LeaseAcquireRequest(BaseModel):
    """Body for `POST /v0/control/experiments/{E}/leases`."""

    model_config = ConfigDict(strict=True, extra="allow")

    holder: WorkerId
    holder_instance: Annotated[str, Field(min_length=1)]


class LeaseRenewRequest(BaseModel):
    """Body for `POST /v0/control/leases/{L}/renew`."""

    model_config = ConfigDict(strict=True, extra="allow")

    holder_instance: Annotated[str, Field(min_length=1)]


class LeaseReleaseRequest(BaseModel):
    """Body for `POST /v0/control/leases/{L}/release`."""

    model_config = ConfigDict(strict=True, extra="allow")

    holder_instance: Annotated[str, Field(min_length=1)]


class ListExperimentsResponse(BaseModel):
    """Wrapper returned by `GET /v0/control/experiments`."""

    model_config = ConfigDict(strict=True, extra="allow")

    experiments: list[RegisteredExperiment]


class ListLeasesResponse(BaseModel):
    """Wrapper returned by `GET /v0/control/leases?holder=<id>`."""

    model_config = ConfigDict(strict=True, extra="allow")

    leases: list[ExperimentLease]
