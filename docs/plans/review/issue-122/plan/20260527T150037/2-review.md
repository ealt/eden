**Findings**

- Medium: the plan now has a migration contradiction around pre-existing experiments. D.2 says missing `experiment.base_commit_sha` with `baseline.enabled: true` must fail fast, but the migration section says existing pre-change experiments without a baseline are acceptable with no backfill. Those cannot both be true for an upgraded store row where `base_commit_sha` is null and no baseline exists. See [issue-122-baseline-variant.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-122-baseline/docs/plans/issue-122-baseline-variant.md:91), [issue-122-baseline-variant.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-122-baseline/docs/plans/issue-122-baseline-variant.md:199), [issue-122-baseline-variant.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-122-baseline/docs/plans/issue-122-baseline-variant.md:219). The plan should pick one rule explicitly:
  - old experiments are unsupported after upgrade unless recreated, or
  - old experiments are tolerated and baseline auto-creation is skipped when `base_commit_sha` is absent, or
  - setup/migration backfills `base_commit_sha`.

- Low: D.4.3 and D.7 now disagree on whether baseline `variant.started` events require `kind`. D.4.3 still says the payload `SHOULD` carry `kind`, while D.7 upgrades that to `REQUIRED` for baselines. See [issue-122-baseline-variant.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-122-baseline/docs/plans/issue-122-baseline-variant.md:116), [issue-122-baseline-variant.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-122-baseline/docs/plans/issue-122-baseline-variant.md:166). I would make D.4.3 match D.7 so the event contract is stated once.

- Low: the major propagation gap is closed, but a few experiment-shape helper/docstring surfaces are still omitted from `files to touch`. The runtime shape of `read_experiment()` changes, and repo-local contract docs still describe the old shape. See [protocol.py](/Users/ericalt/Documents/eden-worktrees/plan-issue-122-baseline/reference/packages/eden-storage/src/eden_storage/protocol.py:320), [client.py](/Users/ericalt/Documents/eden-worktrees/plan-issue-122-baseline/reference/packages/eden-wire/src/eden_wire/client.py:865), [server.py](/Users/ericalt/Documents/eden-worktrees/plan-issue-122-baseline/reference/packages/eden-wire/src/eden_wire/server.py:1500), [conformance _seed.py](/Users/ericalt/Documents/eden-worktrees/plan-issue-122-baseline/conformance/src/conformance/harness/_seed.py:789). Not a design blocker, but worth adding to avoid stale local contract text.

**Assessment**

The substantive completeness gaps from the last round are mostly closed. The `base_commit_sha` propagation surface is now properly enumerated, the conformance auth cases are in scope, the e2e partitioning issue is addressed, and the baseline idempotency rule is materially stronger.

What remains is mostly cleanup of internal consistency. The only issue I’d treat as more than editorial is the old-experiment/null-`base_commit_sha` behavior, because that changes whether the upgrade path is “acceptable with no backfill” or “hard fail unless recreated.”

**Overall**

The plan is close. I’d fix the pre-existing-experiment rule, align the `variant.started.kind` MUST/SHOULD wording, and optionally add the read-experiment helper/docstring surfaces. After that, I would consider the completeness concerns resolved.