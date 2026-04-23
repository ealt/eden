# Experiment fixture

A minimal valid EDEN experiment layout, used by the conformance and
reference-implementation test suites.

- `.eden/config.yaml` — an experiment config that validates against
  [`spec/v0/schemas/experiment-config.schema.json`](../../../spec/v0/schemas/experiment-config.schema.json).
  The `schema-validity` CI job asserts this in every run.

This fixture was ported from the predecessor project's fixture
directory at Phase 1 (chunk 1c). Role scripts (`plan.py`,
`implement.py`, `eval.py`) and the planner workspace are not part
of the protocol-layer fixture; they will be migrated with the
reference implementation in a later phase.
