# control-plane

Reference FastAPI server hosting the EDEN control plane
(spec/v0/11-control-plane.md). Exposes the 19 chapter-07 §15
endpoints under `/v0/control/...`.

## Running

```text
python3 -m eden_control_plane_server \
    --store-url :memory: \
    --port 0 \
    [--admin-token <token>] \
    [--lease-duration-seconds 30]
```

`--store-url` accepts `:memory:` (ephemeral; tests / single-replica
deployments) or `postgresql://…` (the production-shaped backend per
chapter 11 §4 / plan §3.4 Option A).

On startup the server announces `EDEN_CONTROL_PLANE_LISTENING host=…
port=…` on stdout, mirroring the task-store-server convention.

## Scope

This service implements the deployment-level coordination layer
(experiment registry, leases, deployment-scoped worker/group
registry) introduced in chapter 11. Per-experiment task/idea/variant
data continues to live in the task-store-server; the control plane
maintains only the cross-experiment metadata.

The chapter 11 §3 state-sync poller will be added in wave 4 alongside
the orchestrator integration; wave 3 ships the registry, lease ops,
and the deployment-scoped worker/group registry only.
