**Findings**
- Critical: The proposed startup quiescence guard is scoped at the wrong layer. The plan says to fail orchestrator startup when the resolved policy can never quiesce under the smoke fixture [docs/plans/ci-flake-root-causes.md](/Users/ericalt/Documents/eden-worktrees/ci-flake-root-causes/docs/plans/ci-flake-root-causes.md:136), but the current default ideation policy is intentionally open-ended [policies.py](/Users/ericalt/Documents/eden-worktrees/ci-flake-root-causes/reference/packages/eden-dispatch/src/eden_dispatch/policies.py:118), and the compose deployment explicitly supports long-lived/manual sessions via `EDEN_MAX_QUIESCENT_ITERATIONS` [compose.yaml](/Users/ericalt/Documents/eden-worktrees/ci-flake-root-causes/reference/compose/compose.yaml:270). The smoke harness already makes the fixture finite by appending `fixed_total` [smoke.sh](/Users/ericalt/Documents/eden-worktrees/ci-flake-root-causes/reference/compose/healthcheck/smoke.sh:79). As written, this would change valid product behavior to catch a test-fixture drift.
- Critical: Section 4 understates the cleanup gap and the proposed fix is incomplete. `smoke-manual-mode.sh` and `smoke-multi-orchestrator.sh` already call `setup-experiment.sh` without `--data-root` [smoke-manual-mode.sh](/Users/ericalt/Documents/eden-worktrees/ci-flake-root-causes/reference/compose/healthcheck/smoke-manual-mode.sh:57), [smoke-multi-orchestrator.sh](/Users/ericalt/Documents/eden-worktrees/ci-flake-root-causes/reference/compose/healthcheck/smoke-multi-orchestrator.sh:62), which means they default to persistent host paths under `$HOME/.eden/experiments/<id>` [setup-experiment.sh](/Users/ericalt/Documents/eden-worktrees/ci-flake-root-causes/reference/scripts/setup-experiment/setup-experiment.sh:44). Their cleanup never removes that data root [smoke-manual-mode.sh](/Users/ericalt/Documents/eden-worktrees/ci-flake-root-causes/reference/compose/healthcheck/smoke-manual-mode.sh:40), [smoke-multi-orchestrator.sh](/Users/ericalt/Documents/eden-worktrees/ci-flake-root-causes/reference/compose/healthcheck/smoke-multi-orchestrator.sh:45). Chunk B therefore needs explicit migration to per-run temp data roots, not just “propagate the #94 teardown.”
- Major: Deferrals are not actually tracked. Section 10 says every deferral is filed and referenced, but every entry is still `issue TBD` [docs/plans/ci-flake-root-causes.md](/Users/ericalt/Documents/eden-worktrees/ci-flake-root-causes/docs/plans/ci-flake-root-causes.md:368). That conflicts with the repo rule requiring deferrals to land as GitHub issues at deferral time [AGENTS.md](/Users/ericalt/Documents/eden-worktrees/ci-flake-root-causes/AGENTS.md:33).
- Major: The inventory is not yet fully auditable. The plan claims to inventory every CI failure [docs/plans/ci-flake-root-causes.md](/Users/ericalt/Documents/eden-worktrees/ci-flake-root-causes/docs/plans/ci-flake-root-causes.md:5), but still leaves `other / unclassified | 2` [docs/plans/ci-flake-root-causes.md](/Users/ericalt/Documents/eden-worktrees/ci-flake-root-causes/docs/plans/ci-flake-root-causes.md:73), does not preserve the exact signature set used for classification [docs/plans/ci-flake-root-causes.md](/Users/ericalt/Documents/eden-worktrees/ci-flake-root-causes/docs/plans/ci-flake-root-causes.md:56), and the headline “≈53” residual-failure count does not reconcile with the table [docs/plans/ci-flake-root-causes.md](/Users/ericalt/Documents/eden-worktrees/ci-flake-root-causes/docs/plans/ci-flake-root-causes.md:33).
- Suggestion: Chunk C is directionally right but not fully executable yet. The plan still defers the registry-of-record decision to implementation time (`verify in impl`, GHCR vs Docker Hub) [docs/plans/ci-flake-root-causes.md](/Users/ericalt/Documents/eden-worktrees/ci-flake-root-causes/docs/plans/ci-flake-root-causes.md:222). For a `docs/plans/` contract, that choice should be settled before merge.

**1. Missing Context**
- Assessment: The problem statement is strong, but the classification methodology is not reproducible enough yet.
- Issue: The document needs either an appendix or checked-in artifact listing run IDs by bucket plus the exact grep/signature set used to classify them.
- Issue: The two unclassified runs need explicit disposition, otherwise the “inventory every CI failure” claim is not met.
- Issue: The headline arithmetic should be fixed; right now it weakens trust in the rest of the counts.

**2. Feasibility**
- Assessment: Heartbeat logging and shared smoke helpers are feasible against this codebase; the startup guard is not feasible as currently specified.
- Issue: Structured JSONL service logs already exist [logging.py](/Users/ericalt/Documents/eden-worktrees/ci-flake-root-causes/reference/services/_common/src/eden_service_common/logging.py:15), and `smoke.sh` already asserts the host-side log files exist [smoke.sh](/Users/ericalt/Documents/eden-worktrees/ci-flake-root-causes/reference/compose/healthcheck/smoke.sh:316), so a heartbeat-based diagnosis path is practical.
- Issue: The “fail loud at startup” guard should not be generic orchestrator behavior, because open-ended ideation is a valid deployment mode today.
- Issue: Chunk B must include adopting temp `SMOKE_DATA_ROOT` consistently in manual/multi smokes, or the proposed teardown propagation does not solve the real local-state leak.

**3. Alternatives**
- Assessment: The plan is right to avoid retries/skips, but one proposed fix is aimed at the wrong layer.
- Issue: For #215, the better-fitting guard is smoke/setup validation of the copied experiment config, or a smoke-only orchestrator flag, not unconditional startup rejection for any open-ended policy.
- Issue: For quiescence diagnosis, the plan should explicitly leverage the existing per-service JSONL files rather than vaguely “dump the heartbeat series”; otherwise the current `docker compose logs --tail 30` blind spot is only partially addressed.

**4. Completeness**
- Assessment: The chunking is close, but not yet executable as a plan contract.
- Issue: Chunk C still contains an unresolved design choice.
- Issue: Chunk D’s investigation issues should be filed now, not left as placeholders, to satisfy the repo’s deferral discipline.
- Issue: Chunk B’s scope should explicitly cover harmonizing `--data-root` usage across all seven smoke scripts, not just extracting `cleanup()`.

**5. Edge Cases and Risks**
- Assessment: The plan sees the main categories, but a few implementation risks are still under-specified.
- Issue: The heartbeat needs precise semantics for “progress”; otherwise partial livelock can still look healthy if some counter keeps moving.
- Issue: The shared `lib.sh` has to support overlay-specific compose file sets and script-specific diagnostics; `cleanup()` alone is not the full common surface.
- Issue: Digest-pinning a mirrored Forgejo image needs an update procedure/owner, or the repo trades one kind of drift for another.

**Overall Assessment**
The plan has the right overall diagnosis: most “flakes” here are regressions, incomplete propagation, or external registry dependence, not tests that need retries. But it is not ready as an executable `docs/plans/` artifact yet. I would block on fixing the startup-guard scope, correcting the smoke-script/data-root analysis, replacing `issue TBD` with real issue refs, and making the inventory auditable end to end.