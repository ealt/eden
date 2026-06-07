Findings: no blocking or major issues found.

The `source_experiment_id` requiredness is now aligned in the type-enforced surfaces: [experiment.py](/Users/ericalt/Documents/eden-worktrees/impl-issue-128-disambiguate-names/reference/packages/eden-contracts/src/eden_contracts/experiment.py:43), [experiment.schema.json](/Users/ericalt/Documents/eden-worktrees/impl-issue-128-disambiguate-names/spec/v0/schemas/experiment.schema.json:51), [02-data-model.md](/Users/ericalt/Documents/eden-worktrees/impl-issue-128-disambiguate-names/spec/v0/02-data-model.md:173), [07-wire-protocol.md](/Users/ericalt/Documents/eden-worktrees/impl-issue-128-disambiguate-names/spec/v0/07-wire-protocol.md:500), and the importer stamps it unconditionally at [_checkpoint.py](/Users/ericalt/Documents/eden-worktrees/impl-issue-128-disambiguate-names/reference/packages/eden-storage/src/eden_storage/_checkpoint.py:645). I also verified locally that the Pydantic model rejects both missing and null `source_experiment_id`.

Only cleanup-level prose remains: a couple descriptions still summarize the recovery probe as matching `checkpoint_exported_at` without naming the full `(source_experiment_id, checkpoint_exported_at)` pair. That does not make the field optional or create a spec/impl contradiction given the schema/model/table and chapter 10 MUST.

`python3 scripts/spec-xref-check.py` passes.

**CONVERGED: yes**.