**1. Missing Context**

Assessment: the round-0 blockers are resolved. Splitting durability into `v1+durability` and dropping `dispatch_mode` from the universal snapshot fixes the two main coherence problems from the prior draft: [plan](/Users/ericalt/Documents/eden-worktrees/plan-issue-156-experiment-durability-conformance/docs/plans/issue-156-experiment-durability-conformance.md:127), [plan](/Users/ericalt/Documents/eden-worktrees/plan-issue-156-experiment-durability-conformance/docs/plans/issue-156-experiment-durability-conformance.md:147), [plan](/Users/ericalt/Documents/eden-worktrees/plan-issue-156-experiment-durability-conformance/docs/plans/issue-156-experiment-durability-conformance.md:351).

One context point to keep explicit in the spec edits: `v1+durability` is a verification level, not the source of the durability obligation. The plan mostly says this already, but chapter 9 wording will need to preserve that distinction carefully so `01-concepts §13` does not read as “optional if unclaimed”: [plan](/Users/ericalt/Documents/eden-worktrees/plan-issue-156-experiment-durability-conformance/docs/plans/issue-156-experiment-durability-conformance.md:137), [plan](/Users/ericalt/Documents/eden-worktrees/plan-issue-156-experiment-durability-conformance/docs/plans/issue-156-experiment-durability-conformance.md:368).

**2. Feasibility**

Assessment: the level model is now feasible, but there is one remaining harness-architecture gap to pin down before implementation.

Issue:
- The plan says the durability fixture lives in [conformance/scenarios/conftest.py](/Users/ericalt/Documents/eden-worktrees/plan-issue-156-experiment-durability-conformance/conformance/scenarios/conftest.py:1) and the files-to-touch list does not include [plugin.py](/Users/ericalt/Documents/eden-worktrees/plan-issue-156-experiment-durability-conformance/conformance/src/conformance/harness/plugin.py:223), but the current base fixtures only expose an `IutHandle`; the adapter instance is local to `iut` and is not available to downstream fixtures for `crash_restart()`: [plugin.py](/Users/ericalt/Documents/eden-worktrees/plan-issue-156-experiment-durability-conformance/conformance/src/conformance/harness/plugin.py:223), [plugin.py](/Users/ericalt/Documents/eden-worktrees/plan-issue-156-experiment-durability-conformance/conformance/src/conformance/harness/plugin.py:237). The plan should explicitly choose one of:
  1. extend the base harness to expose the adapter object, or
  2. make `durable_clients` own its own adapter lifecycle entirely, patterned after `receiver_iut`, including its own worker seeding.
  
  Right now D.4 reads as though it can layer on top of the existing `wire_client` / `default_workers` flow, but mechanically that is not true without extra plumbing: [plan](/Users/ericalt/Documents/eden-worktrees/plan-issue-156-experiment-durability-conformance/docs/plans/issue-156-experiment-durability-conformance.md:263), [plan](/Users/ericalt/Documents/eden-worktrees/plan-issue-156-experiment-durability-conformance/docs/plans/issue-156-experiment-durability-conformance.md:630).

**3. Alternatives**

Assessment: the new `v1+durability` level is the right approach. I would not push back on that anymore.

Suggestion:
- Now that durability is no longer mandatory `v1`, the durable-only subclass alternative deserves more weight than the plan currently gives it. The main benefit of “always file-backed” was one-path simplicity, but the cost is changing the substrate for the entire conformance suite just to support an optional level: [plan](/Users/ericalt/Documents/eden-worktrees/plan-issue-156-experiment-durability-conformance/docs/plans/issue-156-experiment-durability-conformance.md:215), [plan](/Users/ericalt/Documents/eden-worktrees/plan-issue-156-experiment-durability-conformance/docs/plans/issue-156-experiment-durability-conformance.md:232), [plan](/Users/ericalt/Documents/eden-worktrees/plan-issue-156-experiment-durability-conformance/docs/plans/issue-156-experiment-durability-conformance.md:661). I would still accept the current recommendation, but I’d strengthen the rationale for why the simpler code path outweighs the optional-level blast radius.

**4. Completeness**

Assessment: mostly solid. The section-by-section chapter 9 edit map is much better now, but I see two places where the plan should be sharper.

Issues:
- The §1 / §4 wording needs to avoid implying that `v1+durability` is where §13 “becomes required.” The current chapter 9 intro already uses “implementations that additionally claim X implement the MUSTs from chapter Y” language for checkpoints and multi-experiment because those are genuinely additional contract surfaces: [09-conformance.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-156-experiment-durability-conformance/spec/v0/09-conformance.md:7). Durability is different: the MUST already binds all deployments. So the new prose should say the level verifies or certifies the wire-observable projection of `01-concepts §13`, not that claimants alone implement it. The plan is conceptually correct here, but the actual wording guidance should be more explicit: [plan](/Users/ericalt/Documents/eden-worktrees/plan-issue-156-experiment-durability-conformance/docs/plans/issue-156-experiment-durability-conformance.md:427).
- The §1 / §7 edits need to cover the exact parallel-level prose, not just the enumerated list. Chapter 9 currently says “The checkpoint and multi-experiment levels are parallel…” and gives example claim strings and a linear reference-posture sentence: [09-conformance.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-156-experiment-durability-conformance/spec/v0/09-conformance.md:9), [09-conformance.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-156-experiment-durability-conformance/spec/v0/09-conformance.md:134). Your plan says “add the level to §1/§7,” which is directionally right, but I’d call out those exact sentences so the implementation doesn’t update only the level list and leave the prose lagging.

**5. Edge Cases and Risks**

Assessment: the risks section is good and catches the big ones. I’d add one more concrete risk.

Issue:
- `crash_restart()` needs an explicit control-plane policy for the reference adapter. The current [ReferenceAdapter](/Users/ericalt/Documents/eden-worktrees/plan-issue-156-experiment-durability-conformance/conformance/src/conformance/adapters/reference/adapter.py:35) always spawns a `ControlPlaneSubprocess` and includes its URL in the returned handle: [ReferenceAdapter](/Users/ericalt/Documents/eden-worktrees/plan-issue-156-experiment-durability-conformance/conformance/src/conformance/adapters/reference/adapter.py:94). A naive restart path that just “calls start again” after killing the task-store-server will either leak a second control-plane subprocess or unexpectedly rotate `control_plane_base_url`. The durability scenarios do not exercise chapter 11, so the plan should explicitly say one of:
  1. restart only the task-store-server and preserve the existing control-plane subprocess and URL, or
  2. restart both and treat the new control-plane URL as part of the fresh handle.
  
  Right now that companion-process behavior is unspecified.

Overall assessment: this revision fixes the core design errors from round 0. The `v1+durability` split is defensible, the `dispatch_mode` removal is correct, and the credential-survival claim is now honestly scoped. I’d treat the plan as close to ready, with the main remaining work being to pin down the durability fixture’s ownership model and to make the chapter 9 wording unmistakable that the level verifies a universal MUST rather than downgrading it into an optional semantic obligation.