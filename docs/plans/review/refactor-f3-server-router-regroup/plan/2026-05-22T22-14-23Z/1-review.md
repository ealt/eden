**Overall Assessment**

Iterate. The substantive issues from the first draft are fixed: the router split is now structurally credible, the auth threading is much more explicit, the reference-route test gap is closed, and wave 6 is framed more sanely. What remains is mostly plan hygiene plus one incorrect technical justification in §3.3.

**Findings By Criterion**

**Criterion 8 — Wave Plan Robustness**
- `Should-fix:` The new closure-factory shape in [§3.3](</Users/ericalt/Documents/eden-worktrees/issue-115-server-router-regroup-plan/docs/plans/refactor-f3-server-router-regroup.md:500>) is a real improvement and it does address the function-LEN problem. But the rejection rationale for “methods on a class” at [§3.3](</Users/ericalt/Documents/eden-worktrees/issue-115-server-router-regroup-plan/docs/plans/refactor-f3-server-router-regroup.md:606>) is technically wrong: bound methods do not expose `self` in `inspect.signature(...)`. That means the plan is defending a correct design choice with an incorrect argument.
- Concrete edit: rewrite the “Why closure factories rather than method-on-class” bullet to a true rationale: closure factories keep the module function-shaped, avoid introducing per-router stateful objects, match the current private-helper style, and keep the complexity gate easy to reason about. If you want to keep the `functools.partial` rejection, either verify it with a tiny reproducer during implementation or soften it to “not the chosen baseline.”

**Criterion 11 — L-E Control-Plane Wave**
- `Should-fix:` The new default recommendation in [§7.9](</Users/ericalt/Documents/eden-worktrees/issue-115-server-router-regroup-plan/docs/plans/refactor-f3-server-router-regroup.md:1198>) is good, but the earlier framing in [§1.2](</Users/ericalt/Documents/eden-worktrees/issue-115-server-router-regroup-plan/docs/plans/refactor-f3-server-router-regroup.md:64>) still says the default is to land L-E as wave 6. The plan now contains two different recommendations.
- Concrete edit: update [§1.2](</Users/ericalt/Documents/eden-worktrees/issue-115-server-router-regroup-plan/docs/plans/refactor-f3-server-router-regroup.md:64>) and the L-E row in the table there so they match [§7.9](</Users/ericalt/Documents/eden-worktrees/issue-115-server-router-regroup-plan/docs/plans/refactor-f3-server-router-regroup.md:1198>): default recommendation is split; bundling is operator-contingent.

**Criterion 7 — Spec / Regression Contract Preservation**
- `Should-fix:` [§7.2](</Users/ericalt/Documents/eden-worktrees/issue-115-server-router-regroup-plan/docs/plans/refactor-f3-server-router-regroup.md:1092>) still references “the plan’s test addition (§6.2 `test_dispatch_mode_patch_rejects_extra_allow_null`)” even though that test was correctly removed and replaced with a citation to the existing test at [line 331](</Users/ericalt/Documents/eden-worktrees/issue-115-server-router-regroup-plan/reference/packages/eden-wire/tests/test_reassign_dispatch_wire.py:331>). This leaves the dispatch-mode section internally inconsistent.
- Concrete edit: change [§7.2](</Users/ericalt/Documents/eden-worktrees/issue-115-server-router-regroup-plan/docs/plans/refactor-f3-server-router-regroup.md:1100>) to cite the existing `test_invalid_value_on_extra_key_rejected` and the “Tests already covering invariants” subsection in [§6.2](</Users/ericalt/Documents/eden-worktrees/issue-115-server-router-regroup-plan/docs/plans/refactor-f3-server-router-regroup.md:1028>).

**Criterion 12 — Other Blind Spots**
- `Consider:` The closure-factory pattern in [§3.3](</Users/ericalt/Documents/eden-worktrees/issue-115-server-router-regroup-plan/docs/plans/refactor-f3-server-router-regroup.md:500>) returns inner functions all named `handler`. That is fine for request parsing, but it can collapse Starlette route names / FastAPI-generated operation names unless the registration sets an explicit `name=` or rewrites `handler.__name__`. Nothing in-repo appears to depend on route names today, so this is not a blocker, but it is the main implementation blind spot still not mentioned in the plan.
- `Minor:` [§6.1](</Users/ericalt/Documents/eden-worktrees/issue-115-server-router-regroup-plan/docs/plans/refactor-f3-server-router-regroup.md:1002>) still says “9 eden-wire test files”; wave 5 adds `test_reference_validate.py`, so that count becomes stale.

**What Looks Sound Now**

- `Criterion 1:` The route mapping in [§4](</Users/ericalt/Documents/eden-worktrees/issue-115-server-router-regroup-plan/docs/plans/refactor-f3-server-router-regroup.md:882>) is complete and still matches the 43 live routes in [server.py](</Users/ericalt/Documents/eden-worktrees/issue-115-server-router-regroup-plan/reference/packages/eden-wire/src/eden_wire/server.py:605>).
- `Criterion 2:` The new auth matrix in [§3.7](</Users/ericalt/Documents/eden-worktrees/issue-115-server-router-regroup-plan/docs/plans/refactor-f3-server-router-regroup.md:780>) is the right level of specificity and correctly preserves helper-vs-direct-vs-inline auth patterns.
- `Criterion 3:` The route-order section is now technically correct. Splitting `experiment_lifecycle` and `experiment_read` is a clean way to mirror current registration order without pretending order is load-bearing.
- `Criterion 4:` Exception-handler scope and `_install_exception_handlers(app)` remain sound.
- `Criterion 5:` The checkpoints two-prefix plan still looks correct.
- `Criterion 6:` The `test_artifact_route.py` monkeypatch retarget and new `test_reference_validate.py` close the real test gaps from the prior draft.
- `Criterion 9:` Slop-allow removal timing still looks right in wave 5.

With the three `should-fix` edits above, this is close to ship.