**Findings**

High: [docs/plans/issue-131-auto-checkpointing.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-131-auto-checkpointing/docs/plans/issue-131-auto-checkpointing.md:363) still says the smoke can terminate via “admin bearer.” That endpoint is worker-gated to `admins` or `orchestrators`; a literal deployment admin bearer is valid for checkpoint export/import, but not for `terminate_experiment`. Use an admins/orchestrators worker bearer, or drive termination through the existing orchestrator path.

High: [docs/plans/issue-131-auto-checkpointing.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-131-auto-checkpointing/docs/plans/issue-131-auto-checkpointing.md:590) overclaims that Compose is already wired for complete repo-bundle checkpoints. Current `task-store-server` Compose wiring does not pass `--repo-path`; the checkpoint endpoint emits an empty repo bundle when `checkpoint_repo_root` is absent. The existing manual smoke round-trip checks wire state, not resume-complete git bundle content. Either add repo-path/mount wiring plus a non-empty/valid bundle assertion to this chunk, or state this as an inherited completeness gap with a tracked follow-up.

Medium: [docs/plans/issue-131-auto-checkpointing.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-131-auto-checkpointing/docs/plans/issue-131-auto-checkpointing.md:294) the filename sanitizer can collide: `a/b` and `a_b` both become `a_b`. Since pruning uses the sanitized prefix, a shared destination could delete another experiment’s archives. Prefer reversible percent-encoding or append a short hash of the raw `experiment_id`.

Medium: [docs/plans/issue-131-auto-checkpointing.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-131-auto-checkpointing/docs/plans/issue-131-auto-checkpointing.md:276) advancing `next_at += interval_seconds` avoids immediate retry on ordinary failures, but after a long export or delayed loop it can remain in the past and produce back-to-back checkpoints until caught up. If the intended invariant is “one attempt per interval after the last attempt,” set `next_at = now_fn() + interval_seconds`.

**Level Assessment**

Missing context is now mostly resolved. The lifecycle premise is clear: this is active-loop checkpointing, not a daemonized durability service after orchestrator quiescence.

Feasibility is good except for the two blockers above: terminate auth in the smoke, and the repo-bundle completeness assumption.

Alternatives are handled well. The orchestrator-in-loop choice is now honestly scoped, and the sibling-container/checkpointer alternative is framed as future work rather than silently rejected.

Completeness is close. Config, schema parity, CLI fail-fast, timer behavior, terminal dedup, destination atomicity, and validation coverage are all much stronger. The remaining completeness gap is whether these archives are actually resume-complete under Compose.

Edge cases and risks are much improved. I would tighten filename identity and timer catch-up behavior, and align the D4 heading with the body’s “no wire/spec authority change” language.

Overall: the plan is materially better and implementable after fixing the terminate-auth and repo-bundle wiring claims.