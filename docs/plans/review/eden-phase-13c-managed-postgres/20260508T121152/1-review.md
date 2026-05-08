**1. Missing Context**

This is materially better. The DSN-format limitation and the Secret-based composition story are now explicit. One context issue remains:

- §3.2.2 still phrases embedded `secrets.existingSecret` as though `EDEN_STORE_URL` were already part of the inherited 13a contract, but §8.3b later says 13a did **not** include that key. Those sections should agree that this is a **new 13c requirement** for the embedded+`existingSecret` path, not an existing contract. ([eden-phase-13c-managed-postgres.md](/Users/ericalt/Documents/eden-worktrees/phase-13cde-substrate-plans/docs/plans/eden-phase-13c-managed-postgres.md:531), [eden-phase-13c-managed-postgres.md](/Users/ericalt/Documents/eden-worktrees/phase-13cde-substrate-plans/docs/plans/eden-phase-13c-managed-postgres.md:1624), [eden-phase-13a-helm-base-chart.md](/Users/ericalt/Documents/eden-worktrees/phase-13cde-substrate-plans/docs/plans/eden-phase-13a-helm-base-chart.md:456))

**2. Feasibility**

I still have a significant blocker here, so I would stop at this level again.

- The migration drain/runbook still assumes TTL-based reclamation for worker-host claims, but the current worker hosts do not claim with `expires_at`. The orchestrator sweeper only reclaims claimed tasks that actually have an expiry ([sweep.py](/Users/ericalt/Documents/eden-worktrees/phase-13cde-substrate-plans/reference/packages/eden-dispatch/src/eden_dispatch/sweep.py:32)). The scripted workers claim without expiry ([workers.py](/Users/ericalt/Documents/eden-worktrees/phase-13cde-substrate-plans/reference/packages/eden-dispatch/src/eden_dispatch/workers.py:123), [workers.py](/Users/ericalt/Documents/eden-worktrees/phase-13cde-substrate-plans/reference/packages/eden-dispatch/src/eden_dispatch/workers.py:201)), and the subprocess hosts do too ([subprocess_mode.py](/Users/ericalt/Documents/eden-worktrees/phase-13cde-substrate-plans/reference/services/executor/src/eden_executor_host/subprocess_mode.py:164), [subprocess_mode.py](/Users/ericalt/Documents/eden-worktrees/phase-13cde-substrate-plans/reference/services/evaluator/src/eden_evaluator_host/subprocess_mode.py:149), [subprocess_mode.py](/Users/ericalt/Documents/eden-worktrees/phase-13cde-substrate-plans/reference/services/ideator/src/eden_ideator_host/subprocess_mode.py:307)). `claim_ttl_seconds` is a Web UI concept, not a general worker-host one ([cli.py](/Users/ericalt/Documents/eden-worktrees/phase-13cde-substrate-plans/reference/services/web-ui/src/eden_web_ui/cli.py:58)). So §3.8.2 / §8.3a’s “wait 2× claim_ttl_seconds and let the sweeper drain claims” is still not executable for claims held by executor/evaluator/ideator hosts. ([eden-phase-13c-managed-postgres.md](/Users/ericalt/Documents/eden-worktrees/phase-13cde-substrate-plans/docs/plans/eden-phase-13c-managed-postgres.md:965), [eden-phase-13c-managed-postgres.md](/Users/ericalt/Documents/eden-worktrees/phase-13cde-substrate-plans/docs/plans/eden-phase-13c-managed-postgres.md:1584))

The plan needs one of these before it is implementable:

- Make the drain procedure explicitly operator-driven for claimed host tasks (`admin reclaim` / API reclaim), and stop describing it as TTL-based.
- Or add claim expiries to the worker-host claim paths as an in-scope code change, which would also invalidate the current “no code changes” framing.

**Overall Assessment**

This revision fixes the earlier DSN/TLS design issues. The remaining blocker is narrower but still material: the migration runbook’s drain strategy does not match how host claims actually work today. Once that is corrected, the plan looks close enough for a fuller pass on alternatives/completeness.