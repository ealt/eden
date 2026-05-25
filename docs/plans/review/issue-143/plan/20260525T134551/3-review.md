**Earlier Levels**

Missing context: no blocking concerns.

Feasibility: no blocking concerns. The §13.3 restatement now matches the shape the dispatcher will actually need.

Alternatives: the chosen wire-level dual-gate for `add_to_group` / `remove_from_group` still looks like the right approach.

Completeness: no new blocking concerns after the round-3 cleanup. I’m comfortable moving down to edge-case/risk review.

**Edge Cases And Risks**

1. The self-protection rail is still exposed to semantic drift if it is implemented as a route-local graph simulation rather than a store-authoritative helper. [docs/plans/issue-143-non-admin-default-signup.md](/Users/ericalt/Documents/eden-worktrees/issue-143-plan/docs/plans/issue-143-non-admin-default-signup.md:159) allows a local post-mutation walk, but that duplicates the authoritative `resolve_worker_in_group` semantics the store already owns. That is risky around dangling identifiers, deleted nested groups, future traversal-rule changes, and cycle-defense behavior. Suggestion: prefer a shared helper with exactly the same semantics as store resolution, or at minimum add parity-style tests covering direct self-revoke, nested-group self-revoke, dangling member ids, and deleted nested groups.

2. The auth-disabled dispatcher posture for the new dual-gate branch is still underspecified. The plan correctly focuses on the authenticated case at [docs/plans/issue-143-non-admin-default-signup.md](/Users/ericalt/Documents/eden-worktrees/issue-143-plan/docs/plans/issue-143-non-admin-default-signup.md:67)-[70], but the current server has explicit “auth off” behavior for worker/group-gated helpers in [server.py](/Users/ericalt/Documents/eden-worktrees/issue-143-plan/reference/packages/eden-wire/src/eden_wire/server.py:452) and [server.py](/Users/ericalt/Documents/eden-worktrees/issue-143-plan/reference/packages/eden-wire/src/eden_wire/server.py:465). The new `admin-OR-worker` branch needs the same explicit treatment or tests will end up relying on an accidental fallback. Suggestion: add one sentence to the plan saying what happens when `admin_token is None` for `add_to_group` / `remove_from_group`, and add a unit test for that posture.

3. The “deployment-wide operation” banner mitigation is a real UX safeguard, but it is easy to ship only partially because the plan does not yet enumerate all affected templates. The mitigation is in scope at [docs/plans/issue-143-non-admin-default-signup.md](/Users/ericalt/Documents/eden-worktrees/issue-143-plan/docs/plans/issue-143-non-admin-default-signup.md:400), but the files-to-touch list at [docs/plans/issue-143-non-admin-default-signup.md](/Users/ericalt/Documents/eden-worktrees/issue-143-plan/docs/plans/issue-143-non-admin-default-signup.md:263)-[273] does not identify the worker/group/control-plane templates where those banners would actually render. Suggestion: either enumerate the specific templates now, or explicitly downgrade the banner to a follow-up so the plan does not imply a mitigation it has not fully scoped.

4. The migration note is directionally fine, but the failure mode for partial upgrades is worth stating concretely. [docs/plans/issue-143-non-admin-default-signup.md](/Users/ericalt/Documents/eden-worktrees/issue-143-plan/docs/plans/issue-143-non-admin-default-signup.md:244)-[247] says operators should re-bootstrap for the new shape. The risky case is an existing experiment upgraded to the new code where the post-#140 sign-in identity does not match the historically seeded admin worker id; the new route guard then 403s even though the old deployment “used to work.” Suggestion: call that exact symptom out in the migration/user-guide text so operators know this is an identity-shape mismatch, not a broken auth stack.

**Overall Assessment**

The plan is now in good shape structurally. The remaining issues are implementation and operational risks rather than design or completeness blockers. I’d treat this as ready for an implementation-oriented pass once those edge-case clarifications are either folded in or consciously accepted.