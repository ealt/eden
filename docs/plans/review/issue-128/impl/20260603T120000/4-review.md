Findings:

- **major**: `source_experiment_id` is still specified/typed as optional or nullable in places, contradicting the chapter 10 recovery contract that it MUST be stamped from the manifest on imported experiments. See [02-data-model.md](/Users/ericalt/Documents/eden-worktrees/impl-issue-128-disambiguate-names/spec/v0/02-data-model.md:173), [07-wire-protocol.md](/Users/ericalt/Documents/eden-worktrees/impl-issue-128-disambiguate-names/spec/v0/07-wire-protocol.md:500), [experiment.schema.json](/Users/ericalt/Documents/eden-worktrees/impl-issue-128-disambiguate-names/spec/v0/schemas/experiment.schema.json:42), and [experiment.py](/Users/ericalt/Documents/eden-worktrees/impl-issue-128-disambiguate-names/reference/packages/eden-contracts/src/eden_contracts/experiment.py:43). The importer always stamps it at [_checkpoint.py](/Users/ericalt/Documents/eden-worktrees/impl-issue-128-disambiguate-names/reference/packages/eden-storage/src/eden_storage/_checkpoint.py:645), and [10-checkpoints.md](/Users/ericalt/Documents/eden-worktrees/impl-issue-128-disambiguate-names/spec/v0/10-checkpoints.md:203) says it MUST be the source manifest id. The remaining optional/no-override wording weakens the recovery-probe invariant.

The checkpoint import experiment-id prose itself now matches the dual-model framing: receiver-owned id, never source id, with provenance. I did not find remaining stale “fresh per import” wording outside the intended multi-experiment/control-plane contexts.

Verification: `python3 scripts/spec-xref-check.py` passes.

**CONVERGED: no**.