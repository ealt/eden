# Operations playbooks

Short, focused operator how-tos for the reference EDEN deployment. Each
playbook covers when to use a flow, the canonical commands, and what
the wire-observable end-state should look like.

- [Dispatch-mode operator playbook](dispatch-mode.md) — flipping a
  decision type to manual / back to auto.
- [Reassignment operator playbook](reassign.md) — updating a task's
  target after the orchestrator has dispatched it.
- [Multi-orchestrator deployment](multi-orchestrator.md) — running
  two or more auto-orchestrator replicas.
- [Initial-admin credential recovery](initial-admin-credential.md) —
  minting a usable bearer for the worker `setup-experiment.sh`
  seeded into the `admins` group.
- [Experiment data durability](experiment-data-durability.md) — where
  experiment data lives, durability posture, custom data root,
  migration from named volumes (Phase 12a-1g).
- [Experiment lifecycle](experiment-lifecycle.md) — terminating an
  experiment (operator wire op + orchestrator policy-driven path),
  reference termination policies, drain semantics, idempotent
  re-terminate (Phase 12a-3).
- [Web UI multi-experiment operation](web-ui-multi-experiment.md) — the
  experiment switcher, the four credential-bootstrap postures, and the
  per-experiment config / repo layout (issue #145).

These docs assume the reference Compose deployment + the
[`docs/glossary.md`](../glossary.md) vocabulary. For the underlying
protocol semantics see [`spec/v0/`](../../spec/v0/).
