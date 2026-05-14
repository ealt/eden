**Findings**
1. The manual durability check is still not runnable as written. The plan now says every smoke script should use a temp data root plus an `EXIT` trap that cleans it up [plan](/Users/ericalt/Documents/eden-worktrees/phase-12a-1g-experiment-durability/docs/plans/eden-phase-12a-1g-experiment-durability.md:240), but the manual check still starts with `bash reference/compose/healthcheck/smoke.sh` and then assumes the experiment remains up, the data lives under `~/.eden/experiments/$EDEN_EXPERIMENT_ID`, and `.env` is the active env file [plan](/Users/ericalt/Documents/eden-worktrees/phase-12a-1g-experiment-durability/docs/plans/eden-phase-12a-1g-experiment-durability.md:285). The actual script uses a temp env file and tears the stack down on exit [smoke.sh](/Users/ericalt/Documents/eden-worktrees/phase-12a-1g-experiment-durability/reference/compose/healthcheck/smoke.sh:31) [smoke.sh](/Users/ericalt/Documents/eden-worktrees/phase-12a-1g-experiment-durability/reference/compose/healthcheck/smoke.sh:34) [smoke.sh](/Users/ericalt/Documents/eden-worktrees/phase-12a-1g-experiment-durability/reference/compose/healthcheck/smoke.sh:41). That makes the manual validation recipe self-contradictory.
2. Minor drift: the risks section still says “run all three smoke scripts before push” [plan](/Users/ericalt/Documents/eden-worktrees/phase-12a-1g-experiment-durability/docs/plans/eden-phase-12a-1g-experiment-durability.md:396), but the updated gate correctly requires four.

**1. Missing Context**
Assessment: Sufficient now. The lifetime model, overlay propagation, and `eden-worktrees` classification are all explicit enough.

**2. Feasibility**
Assessment: Feasible as written. The earlier spec-lifetime and overlay-scope blockers are addressed.

**3. Alternatives**
Assessment: Still the right approach. The split between a binding-agnostic spec invariant and a Compose-specific bind-mount implementation remains sound.

**4. Completeness**
Assessment: The validation surface is now much better. Promoting `smoke-subprocess-docker.sh` to a first-class gate closed the main completeness gap from the last round.

**5. Edge Cases and Risks**
Assessment: One substantive issue remains.

- Rewrite the manual durability check so it does not call `smoke.sh`. It should be an explicit sequence built from `setup-experiment.sh --env-file "$ENV_FILE" --data-root "$ROOT"` plus `docker compose --env-file "$ENV_FILE" up/down`, using the same `ROOT` and `ENV_FILE` throughout. As written, it cannot validate durability because the helper script cleans up the very state the recipe wants to inspect.
- Clean up the stale “three smoke scripts” wording in risks so the plan’s prose matches the actual gate set.

**Overall Assessment**
The plan is close. The substantive design issues from earlier rounds are resolved, and the remaining problem is narrow: the manual durability-validation recipe needs to be rewritten to match the new smoke-script behavior. After that, the plan looks ready.