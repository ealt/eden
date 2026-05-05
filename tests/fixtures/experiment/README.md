# Experiment fixture

A minimal valid EDEN experiment layout, used by the conformance and reference-implementation test suites.

- `.eden/config.yaml` — an experiment config that validates against [`spec/v0/schemas/experiment-config.schema.json`](../../../spec/v0/schemas/experiment-config.schema.json). The `schema-validity` CI job asserts this in every run.
- `ideate.py`, `execute.py`, `eval.py`, `Dockerfile` — subprocess-mode role scripts consumed by the chunk-10d `compose-smoke-subprocess` and chunk-10d-followup-A `compose-smoke-subprocess-docker` CI jobs. The reference worker hosts spawn these via the `*_command` keys in `.eden/config.yaml` (per [`spec/v0/reference-bindings/worker-host-subprocess.md`](../../../spec/v0/reference-bindings/worker-host-subprocess.md)).

Ported from the predecessor project at Phase 1 (chunk 1c). The pre-rename version of this fixture carried two role-binding-shaped keys (`planner_root`, `workspace`) that were never read by any EDEN code path; the directed-evolution rename pass removed them.
