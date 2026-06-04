**1. Missing Context**
The problem is mostly defined, but the plan needs one key lifecycle premise stated up front: the current single-experiment orchestrator is not always long-running. It exits cleanly on quiescence in [loop.py](/Users/ericalt/Documents/eden-worktrees/plan-issue-131-auto-checkpointing/reference/services/orchestrator/src/eden_orchestrator/loop.py:146), and Compose intentionally does not restart clean exits in [compose.yaml](/Users/ericalt/Documents/eden-worktrees/plan-issue-131-auto-checkpointing/reference/compose/compose.yaml:320). That means cadence checkpoints only happen while the orchestrator is still alive.

Also, the completion-record section is inconsistent with project guidance: this is a plan in `docs/plans/`, so the roadmap entry should point at the plan, not use the “planless” PR-pointer shape.

**2. Feasibility**
D4 is basically feasible. The spec makes checkpoint export/import admin-gated, and the route enforces `require_admin` in [checkpoints.py](/Users/ericalt/Documents/eden-worktrees/plan-issue-131-auto-checkpointing/reference/packages/eden-wire/src/eden_wire/routers/checkpoints.py:61). `StoreClient` supports `bearer="admin:<token>"`, and Compose already passes `--admin-token` to the orchestrator. No wire authority-table change is needed.

But the plan should phrase this as “no wire/spec authority change,” not “no spec change,” because it does edit `spec/v0/schemas/experiment-config.schema.json`.

The scheduler API is ambiguous enough to cause an implementation bug. Section 3.2 says the scheduler is constructed with an admin-auth export callable, but `maybe_checkpoint_periodic(store, ...)` and the loop call in §3.3 pass the normal worker-auth store. If the method uses that `store`, export will 403. Make the scheduler own an admin export callable/client and remove the `store` parameter from export methods, or name the parameter explicitly as `admin_export`.

D6 is feasible only with careful loop refactoring. The current loop has direct `return`s on quiescence and stop. The plan’s “single exit path” is necessary. Also, the terminal export must be gated by an observed `state == "terminated"` on final exit; otherwise a normal quiescent-but-running experiment would get a misleading `-terminated-` tar. The proposed smoke assertion currently blurs this by expecting a terminal tar after “quiescence/terminate.”

One more feasibility issue: the added terminal `read_experiment_state()` must be best-effort. If that read raises and crashes the loop, auto-checkpointing has violated D5.

**3. Alternatives**
Orchestrator-in-loop is simpler, but the plan’s rationale overstates it because the orchestrator currently exits. If the desired product behavior is “checkpoints keep happening until the experiment is explicitly terminated,” a sibling checkpointer is the cleaner match. If the chosen behavior is “checkpoints happen while the orchestrator is active,” the plan should say that plainly.

`interval_seconds` is the right call; it matches existing config units. Keeping destination out of portable config is also the right call. The plan should add fail-fast behavior for `enabled=true` with no resolved `--auto-checkpoint-dir` outside Compose.

**4. Completeness**
Add these details before implementation:

- Pydantic fields inside `AutoCheckpointConfig` need explicit-null rejection, not just the top-level `auto_checkpoint` field.
- Define first-checkpoint timing: immediately at startup or after one full interval.
- Define retry behavior after export failure. “Leave `last_at` unchanged” may retry every poll tick, not after the interval.
- Ensure temp files are created in the destination directory before `os.replace`.
- Sanitize or hash `experiment_id` for filenames; protocol schemas mostly require only non-empty strings.
- Add tests for no terminal checkpoint on running quiescent exit, terminal checkpoint after admin/policy termination, state-read failure isolation, and worker-bearer export rejection.
- Make the smoke explicitly terminate the experiment before expecting `-terminated-`.

**5. Edge Cases And Risks**
Main risks: retry storms on persistent export failure, terminal checkpoint duplication after orchestrator restart, incomplete checkpoint archives if the task-store-server substrate pieces are not wired, and expanding the orchestrator process’s effective privilege by keeping an admin bearer available beyond startup.

**Overall**
The plan is close in shape, and D4 is defensible. I would not approve it as-is. The blockers are the orchestrator lifecycle mismatch for cadence, the ambiguous scheduler/admin-client API, and the terminal-checkpoint semantics around quiescent exit. Fix those, then the rest is mostly implementation detail and test coverage.