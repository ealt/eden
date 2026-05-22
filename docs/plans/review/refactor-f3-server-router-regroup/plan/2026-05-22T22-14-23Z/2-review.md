**Overall Assessment**

Iterate, but only lightly. The substantive design issues are now resolved. What remains is cleanup of a couple stale risk statements that still reflect the superseded “route ordering is load-bearing / new dispatch-mode test in wave 4” framing.

**Findings**

**Criterion 12 — Internal Consistency**
- `Should-fix:` The risk entry at [§9 item 2](</Users/ericalt/Documents/eden-worktrees/issue-115-server-router-regroup-plan/docs/plans/refactor-f3-server-router-regroup.md:1408>) still says `app.include_router` ordering regression is a correctness risk and that a wave-4 test “locks it in.” That now conflicts with [Decision 5](</Users/ericalt/Documents/eden-worktrees/issue-115-server-router-regroup-plan/docs/plans/refactor-f3-server-router-regroup.md:270>) and [§7.1](</Users/ericalt/Documents/eden-worktrees/issue-115-server-router-regroup-plan/docs/plans/refactor-f3-server-router-regroup.md:1103>), which correctly say ordering is non-load-bearing under single-segment path params. The example at [1411-1412](</Users/ericalt/Documents/eden-worktrees/issue-115-server-router-regroup-plan/docs/plans/refactor-f3-server-router-regroup.md:1411>) is now wrong on its own terms.
- Concrete edit: rewrite risk 2 as “path-segment scoping assumption regression” rather than “include_router ordering regression,” and cite `test_path_segment_scoping_no_shadow` as pinning the non-greedy `{experiment_id}` assumption.

- `Should-fix:` The risk entry at [§9 item 5](</Users/ericalt/Documents/eden-worktrees/issue-115-server-router-regroup-plan/docs/plans/refactor-f3-server-router-regroup.md:1431>) still says “wave-4 test addition is the backstop,” but [§6.2](</Users/ericalt/Documents/eden-worktrees/issue-115-server-router-regroup-plan/docs/plans/refactor-f3-server-router-regroup.md:1065>) now correctly treats the dispatch-mode null case as already covered by the existing test at [test_reassign_dispatch_wire.py:331](</Users/ericalt/Documents/eden-worktrees/issue-115-server-router-regroup-plan/reference/packages/eden-wire/tests/test_reassign_dispatch_wire.py:331>).
- Concrete edit: change risk 5 to cite the existing `test_invalid_value_on_extra_key_rejected` and drop the “wave-4 test addition” wording.

**Criterion 8 — Wave Robustness**
- `Consider:` The new inner-handler naming note at [§3.3 Notes](</Users/ericalt/Documents/eden-worktrees/issue-115-server-router-regroup-plan/docs/plans/refactor-f3-server-router-regroup.md:657>) is the right addition. If you want to make the execution contract tighter, name which mitigation is preferred: `name=` at registration time is a bit clearer than mutating `handler.__name__` after construction.
- No plan change required, but picking the preferred option in the plan would reduce implementation drift.

**What Looks Sound Now**

- `Criterion 1:` Route mapping is still complete and accurate.
- `Criterion 2:` The auth matrix is strong and preserves the important direct-vs-helper distinctions.
- `Criterion 3:` The route-order section is now technically correct, and splitting `experiment_lifecycle` / `experiment_read` is clean.
- `Criterion 4:` Exception-handler scoping remains sound.
- `Criterion 5:` Checkpoints two-prefix handling is still correct.
- `Criterion 6:` Reference-route coverage is now appropriately planned, including the artifact monkeypatch retarget.
- `Criterion 7:` The dispatch-mode section now correctly points at the existing regression test.
- `Criterion 11:` L-E framing is now aligned across the main design sections.

With the two stale risk entries updated, this is ready to ship as a plan.