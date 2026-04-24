**Missing Context**

Assessment: much better. The updated draft now defines compatibility policy, enumerates the live `run_experiment` references, and distinguishes live docs from frozen plan/review artifacts. I don’t see a blocking level-1 context gap.

**Feasibility**

Assessment: the cut-over itself is feasible. Deleting `run_experiment`, narrowing `eden-dispatch` to `run_orchestrator_iteration` plus scripted workers, and migrating the remaining in-tree callers can all be implemented cleanly from the current codebase.

**Alternatives**

Assessment: the chosen approach still looks right. Given the explicit in-tree-only compatibility policy, removing the obsolete driver is cleaner than keeping a deprecated shim that preserves the very parallel path this phase is trying to eliminate.

**Completeness**

Assessment: significant concerns remain here, so I would stop at this level.

- The plan still cannot satisfy its own grep gate as written. In the live-doc rewrite section it proposes replacement text for `AGENTS.md` and `docs/roadmap.md` that still literally includes `run_experiment` in historical narration, for example at [docs/plans/eden-phase-8c-cutover.md](</Users/ericalt/Documents/eden/docs/plans/eden-phase-8c-cutover.md:126>), [docs/plans/eden-phase-8c-cutover.md](</Users/ericalt/Documents/eden/docs/plans/eden-phase-8c-cutover.md:127>), [docs/plans/eden-phase-8c-cutover.md](</Users/ericalt/Documents/eden/docs/plans/eden-phase-8c-cutover.md:133>), [docs/plans/eden-phase-8c-cutover.md](</Users/ericalt/Documents/eden/docs/plans/eden-phase-8c-cutover.md:134>), and [docs/plans/eden-phase-8c-cutover.md](</Users/ericalt/Documents/eden/docs/plans/eden-phase-8c-cutover.md:135>). But Verification §7 requires zero `run_experiment` matches in those exact live-doc targets at [docs/plans/eden-phase-8c-cutover.md](</Users/ericalt/Documents/eden/docs/plans/eden-phase-8c-cutover.md:163>). You need to choose one policy:
  - allow historical mentions in live docs and relax the grep gate, or
  - keep the zero-hit gate and rewrite those passages without the symbol name, e.g. “the former in-process driver”.

- The claimed coverage transfer for the deleted lifecycle-reconstruction test is overstated. The plan says the e2e test plus `test_orchestrator_iteration` preserve the same invariant in observable form at [docs/plans/eden-phase-8c-cutover.md](</Users/ericalt/Documents/eden/docs/plans/eden-phase-8c-cutover.md:54>). That does not match the current tests:
  - the e2e test only checks counts of `task.completed`, `trial.integrated`, and `trial.succeeded` at [test_e2e.py](/Users/ericalt/Documents/eden/reference/services/orchestrator/tests/test_e2e.py:266),
  - `test_orchestrator_iteration` covers a few orchestrator-side transitions and one promotion path, not replay-based reconstruction of a mixed worker+orchestrator transcript at [test_orchestrator_iteration.py](/Users/ericalt/Documents/eden/reference/packages/eden-dispatch/tests/test_orchestrator_iteration.py:58),
  - the deleted test is the only place that walks the full log and maps events like `task.claimed`, `proposal.drafted`, and `trial.started` back to final lifecycle state at [test_end_to_end.py](/Users/ericalt/Documents/eden/reference/packages/eden-dispatch/tests/test_end_to_end.py:171).
  
  If that invariant still matters, the plan needs a targeted replacement test. If it does not, the plan should say coverage is intentionally reduced here rather than claiming it is preserved.

I did not evaluate edge cases and risks because the completeness issues above are substantive enough to resolve first.

**Overall Assessment**

This draft is materially stronger than the previous one. The main remaining problem is that it still overclaims completion: the live-doc rewrite policy and the test-coverage transfer story are not yet internally consistent. Once those two points are tightened, the plan should be close to ready.