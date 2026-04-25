**Findings**

- **Medium** — The planned `web-ui` dependency list is now incomplete. The CLI section says the service will import `eden_task_store_server.load_experiment_config` in [eden-phase-9-web-ui.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-9-web-ui.md:235), but the planned `pyproject.toml` dependencies in [eden-phase-9-web-ui.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-9-web-ui.md:225) still omit `eden-task-store-server`. The existing evaluator host does declare that dependency in [reference/services/evaluator/pyproject.toml](/Users/ericalt/Documents/eden/reference/services/evaluator/pyproject.toml:8). Either add the dependency or move the config loader into a shared package.

- **Medium** — One stale experiment-drift claim remains. The main CLI section correctly says the config-file vs `--experiment-id` cross-check is not implementable because `ExperimentConfig` has no `experiment_id` in [eden-phase-9-web-ui.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-9-web-ui.md:243), but the Risks section still says “the start-up `experiment_id` mismatch check catches the loud cases” in [eden-phase-9-web-ui.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-9-web-ui.md:667). That should be removed or reworded.

- **Low** — A few internal references are stale after the renumbering. `--claim-ttl-seconds` points to “§F below” instead of the new sweeper section D in [eden-phase-9-web-ui.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-9-web-ui.md:255). The Phase-3 retry logic cites chapter 07 “§6” for submit idempotency in [eden-phase-9-web-ui.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-9-web-ui.md:457) and [eden-phase-9-web-ui.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-9-web-ui.md:656), but the actual submit-idempotency section is [07-wire-protocol.md](/Users/ericalt/Documents/eden/spec/v0/07-wire-protocol.md:159). These are doc-fidelity fixes, not design blockers.

**Status Against Prior Rounds**

- **Round 0 / stranded claims:** addressed. The new sweeper section is the missing operational piece, and it is now correctly separated from `run_orchestrator_iteration` in [eden-phase-9-web-ui.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-9-web-ui.md:489). That closes the earlier “TTL on paper only” problem.

- **Round 0 / planner inputs:** addressed. The planner page now surfaces `objective`, `metrics_schema`, and recent proposals/trials in [eden-phase-9-web-ui.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-9-web-ui.md:388), which fixes the earlier missing-context issue.

- **Round 0 / partial-write handling:** mostly addressed, with the same explicit caveat you now document. The new retry-before-orphan policy in [eden-phase-9-web-ui.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-9-web-ui.md:457) is the right addition. The residual Phase-2→Phase-3 window remains, but it is now clearly named as an accepted limitation rather than an accidental omission.

- **Round 0 / auth and bearer hygiene:** addressed. The revised auth section remains solid.

- **Round 0 / alternatives and stack rationale:** addressed. The alternatives section now reads as a fair comparison rather than a strawman.

- **Round 1 / error names:** addressed. The canonical vocabulary now matches [07-wire-protocol.md](/Users/ericalt/Documents/eden/spec/v0/07-wire-protocol.md:142).

- **Round 1 / impossible experiment-id cross-check:** mostly addressed, except for the one stale Risks sentence noted above.

- **Round 1 / stale threat-model sentence:** addressed. The “Why this stack” section is now consistent with the explicit threat model in [eden-phase-9-web-ui.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-9-web-ui.md:89).

**Overall**

The substantive round-0 and round-1 findings are now addressed. I do not see a new architectural blocker.

What remains is cleanup: add the missing `eden-task-store-server` dependency, remove the stale experiment-id mismatch sentence in Risks, and fix the renumbered section citations. After those edits, this looks ready to implement with the already-documented caveat that the Phase-2→Phase-3 ready-before-submit window is still a known reference-impl limitation.