**1. Missing Context**

Assessment: mostly resolved. The plan now explains the critical substrate fact that blocked the prior draft: exp-2 needs its own exec-mode overlays, and the lease drill must run mid-flight. A reader can understand the problem and the proposed shape without pulling too much from prior discussion.

Suggestions:
- Tighten the remaining stale wording in Decision 1. It still says §3.1.1 offers “preferred” wrapper vs “fallback” overlays, but §3.1.1 has now made dedicated `-2` overlays the actual design ([issue-183-cross-mode-ci-smoke-matrix.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-183-cross-mode-ci-smoke-matrix/docs/plans/issue-183-cross-mode-ci-smoke-matrix.md:66), [issue-183-cross-mode-ci-smoke-matrix.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-183-cross-mode-ci-smoke-matrix/docs/plans/issue-183-cross-mode-ci-smoke-matrix.md:113)).

**2. Feasibility**

Assessment: the main feasibility blocker is fixed. Dedicated `compose.multi-experiment.subprocess.yaml` and `compose.multi-experiment.docker.yaml` match the repo’s real mode-selection model and respect the current DooD privilege boundary.

Specific issue:
- The shared-library contract still carries the dead wrapper-era interface: `SETUP_EXTRA_ARGS=(--exec-mode subprocess)` and “set `OVERLAYS` + `SETUP_EXTRA_ARGS`” are no longer consistent with the design, because `setup-experiment` does not accept `subprocess` and the subprocess cell example no longer uses that flag ([issue-183-cross-mode-ci-smoke-matrix.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-183-cross-mode-ci-smoke-matrix/docs/plans/issue-183-cross-mode-ci-smoke-matrix.md:96), [issue-183-cross-mode-ci-smoke-matrix.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-183-cross-mode-ci-smoke-matrix/docs/plans/issue-183-cross-mode-ci-smoke-matrix.md:109), [setup-experiment.sh](/Users/ericalt/Documents/eden-worktrees/plan-issue-183-cross-mode-ci-smoke-matrix/reference/scripts/setup-experiment/setup-experiment.sh:419)). That should be removed or rewritten before merge so the plan does not reintroduce the abandoned path.

**3. Alternatives**

Assessment: this now looks like the right approach. Making `-2` mode selection symmetric with exp-1 overlay composition is simpler and more defensible than inventing a second mode-selection mechanism for only half the stack.

Suggestion:
- None beyond cleaning the stale “preferred/fallback” phrasing so the plan consistently presents overlays as the chosen design.

**4. Completeness**

Assessment: close, but there are still a couple of consistency gaps.

Specific issues:
- The estimate section still says wave 1 includes “the env-driven wrapper (or fallback overlays),” which no longer matches the design ([issue-183-cross-mode-ci-smoke-matrix.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-183-cross-mode-ci-smoke-matrix/docs/plans/issue-183-cross-mode-ci-smoke-matrix.md:339)).
- The plan should be a bit more explicit about the positive control for exp-B. “Host logs show the subprocess JSON-line protocol / real DooD sibling” is directionally right, but the gate should name a concrete observable that is unlikely to drift, such as `docker inspect` on exp-B host command/flags for subprocess mode and sibling-container labels for docker mode ([issue-183-cross-mode-ci-smoke-matrix.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-183-cross-mode-ci-smoke-matrix/docs/plans/issue-183-cross-mode-ci-smoke-matrix.md:275), [issue-183-cross-mode-ci-smoke-matrix.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-183-cross-mode-ci-smoke-matrix/docs/plans/issue-183-cross-mode-ci-smoke-matrix.md:281)).

**5. Edge Cases And Risks**

Assessment: the risk section is much stronger now, especially risk 6 on silent scripted fallback.

Suggestion:
- Add one explicit note that the new `-2` overlays must follow the same Compose-merge discipline as the existing exp-1 overlays, especially around additive `volumes:` merging. The repo already documents how duplicate mounts at the same target can silently produce the wrong winner in merged overlays; calling that out here would make the implementation guardrail sharper ([compose.subprocess.yaml](/Users/ericalt/Documents/eden-worktrees/plan-issue-183-cross-mode-ci-smoke-matrix/reference/compose/compose.subprocess.yaml:136)).

**Overall Assessment**

This is now substantially stronger and mostly implementation-ready. The core design is aligned with the real Compose substrate, and the previous blockers are addressed. I would not hold the plan on architecture anymore; I would just clean the remaining stale wrapper references and sharpen the positive-control wording before treating it as final.