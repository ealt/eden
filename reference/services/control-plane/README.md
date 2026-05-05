# control-plane (placeholder)

This directory is a placeholder for the future control-plane service.
Phase 12 of the roadmap will populate it; see
[`docs/roadmap.md`](../../../docs/roadmap.md) "Phase 12 —
Multi-experiment (leases, control plane, switcher)".

Phase 12's scope:

- Control plane service + lease data model.
- Multi-replica orchestrator; lease-holder fail-over chaos test.
- Cross-experiment views in the shared planner.
- Experiment switcher in the Web UI.
- Multi-experiment conformance scenarios.

Until Phase 12, each experiment runs as its own self-contained
Compose stack provisioned by
[`reference/scripts/setup-experiment/setup-experiment.sh`](../../scripts/setup-experiment/setup-experiment.sh);
there is no cross-experiment coordination layer.

This service is intentionally NOT a `pyproject.toml` workspace
member; it stays out of the build until there is something to build.
