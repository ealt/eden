**Findings**
1. The updated plan now changes the docker-exec path, but the mandatory validation gate still omits `bash reference/compose/healthcheck/smoke-subprocess-docker.sh` [plan](/Users/ericalt/Documents/eden-worktrees/phase-12a-1g-experiment-durability/docs/plans/eden-phase-12a-1g-experiment-durability.md:264) [plan](/Users/ericalt/Documents/eden-worktrees/phase-12a-1g-experiment-durability/docs/plans/eden-phase-12a-1g-experiment-durability.md:293). That is a completeness problem because this chunk explicitly rewrites `compose.docker-exec.yaml` mount forwarding [plan](/Users/ericalt/Documents/eden-worktrees/phase-12a-1g-experiment-durability/docs/plans/eden-phase-12a-1g-experiment-durability.md:234), and the repo’s own command gate treats `smoke-subprocess-docker.sh` as part of the literal pre-push surface [AGENTS.md](/Users/ericalt/Documents/eden-worktrees/phase-12a-1g-experiment-durability/AGENTS.md:83) [AGENTS.md](/Users/ericalt/Documents/eden-worktrees/phase-12a-1g-experiment-durability/AGENTS.md:185).
2. Minor doc drift: the risk note still talks about “until an authorized operator explicitly terminates it” [plan](/Users/ericalt/Documents/eden-worktrees/phase-12a-1g-experiment-durability/docs/plans/eden-phase-12a-1g-experiment-durability.md:388), even though the revised §13 intentionally moved away from that framing [plan](/Users/ericalt/Documents/eden-worktrees/phase-12a-1g-experiment-durability/docs/plans/eden-phase-12a-1g-experiment-durability.md:53).

**1. Missing Context**
Assessment: Much better. The lifetime semantics and the overlay-dependent storage surfaces are now explicit enough for an implementer to understand the real problem.

**2. Feasibility**
Assessment: The earlier feasibility blockers are resolved. The spec wording no longer obviously conflicts with chapter 01’s termination model, and the overlay propagation work is now concrete enough to be implementable.

**3. Alternatives**
Assessment: This still looks like the right approach. Keeping the spec binding-agnostic while making Compose choose bind-mounts, and explicitly classifying `eden-worktrees` as ephemeral shared scratch, is the right split for this chunk.

**4. Completeness**
Assessment: One significant gap remains.

- Add `bash reference/compose/healthcheck/smoke-subprocess-docker.sh` to both the test-design section and the verification-gates section. This chunk now edits the docker-exec path directly, so leaving that smoke out means the most failure-prone mode is not part of the required local validation.
- Tighten the smoke-script inventory. The plan currently says “Smoke scripts (1 file, possibly more...)” [plan](/Users/ericalt/Documents/eden-worktrees/phase-12a-1g-experiment-durability/docs/plans/eden-phase-12a-1g-experiment-durability.md:238) but then requires the same tempdir/trap posture in `smoke-subprocess.sh`, `smoke-subprocess-docker.sh`, and `e2e.sh` [plan](/Users/ericalt/Documents/eden-worktrees/phase-12a-1g-experiment-durability/docs/plans/eden-phase-12a-1g-experiment-durability.md:240). Make those explicit touched files so the execution surface matches the file list.

I’d stop here and fix completeness before spending time on edge cases and risks.

**Overall Assessment**
The update addressed the round-0 blockers well. The plan is close, but it is still missing one load-bearing validation path: the docker-exec smoke must become a required gate now that docker-exec rewiring is in scope.