One item is still open.

**Open Item**
- The fixture-ownership rewrite does not account for the existing autouse `default_workers` fixture in [plugin.py](/Users/ericalt/Documents/eden-worktrees/plan-issue-156-experiment-durability-conformance/conformance/src/conformance/harness/plugin.py:257). Because that autouse fixture depends on `wire_client`, which depends on `iut`, every durability test will still spin up the base IUT/client path even if the test only uses your self-owned `durable_clients` fixture. That conflicts with the plan’s claim that the durability path stays off the shared base fixtures and that `plugin.py` is untouched: [issue-156-experiment-durability-conformance.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-156-experiment-durability-conformance/docs/plans/issue-156-experiment-durability-conformance.md:205), [issue-156-experiment-durability-conformance.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-156-experiment-durability-conformance/docs/plans/issue-156-experiment-durability-conformance.md:696). The plan needs one explicit suppression strategy:
  - override/shadow `default_workers` for the durability module or durability fixtures,
  - move durability scenarios under a fixture scope that avoids that autouse path,
  - or accept that `plugin.py` must change after all.

Everything else you changed resolves my round-1 items. The fixture-ownership decision is now clear, the control-plane restart policy is pinned, the `verifies-not-imposes` wording guidance is much sharper, and the always-file-backed rationale is now defensible.

Overall assessment: close to ready, but not fully closed until the autouse `default_workers` interaction is addressed explicitly.