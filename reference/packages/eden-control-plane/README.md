# eden-control-plane

Pydantic models and HTTP client for the EDEN control plane
(spec/v0/11-control-plane.md).

This package ships:

- `ExperimentLease` — the per-experiment, time-bounded ownership claim
  defined in chapter 11 §4.2 (schema:
  [`spec/v0/schemas/lease.schema.json`](../../../spec/v0/schemas/lease.schema.json)).
- `RegisteredExperiment` — a single registry entry per chapter 11 §2.1.
- `ListExperimentsResponse`, `ListLeasesResponse` — the wire shapes for
  the §15.1 / §15.2 list endpoints.
- `LeaseAcquireRequest`, `LeaseRenewRequest`, `LeaseReleaseRequest` —
  the request bodies for the §15.2 lease ops.
- `ControlPlaneClient` — `httpx`-based HTTP client over the 19
  endpoints under `/v0/control/...` from chapter 07 §15.
- Wire error classes for the four lease error codes added to chapter 07
  §9 (`LeaseHeldByOther`, `LeaseNotHeld`, `LeaseExpired`,
  `LeaseInstanceMismatch`) plus reuse of existing eden-storage /
  eden-wire shapes (`NotFound`, `AlreadyExists`, `InvalidPrecondition`,
  `Unauthorized`, `Forbidden`).

The server side ships separately in `reference/services/control-plane/`
(wave 3).
