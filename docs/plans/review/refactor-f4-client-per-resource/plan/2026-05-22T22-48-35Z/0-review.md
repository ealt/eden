**Overall Assessment**

Iterate. The core shape is coherent and the F-3 symmetry story mostly holds, but a few plan-stage claims are materially out of sync with the current code: the bridge sketch is underspecified for the real integrator surface, the protocol-break regression test is not executable as written, and the wave/backstop story overstates what pyright can prove before the flat methods are actually removed.

**Findings**

**Criterion 6 — Integrator + Dispatch Bridge Shape**
- `Must-fix:` §7.1’s `IntegratorStore` sketch does not match the real integrator call surface. `Integrator` currently uses `read_variant`, `read_idea`, `integrate_variant`, and `validate_evaluation` on its store ([integrator.py](/Users/ericalt/Documents/eden-worktrees/issue-116-client-per-resource-plan/reference/packages/eden-git/src/eden_git/integrator.py:137), [integrator.py](/Users/ericalt/Documents/eden-worktrees/issue-116-client-per-resource-plan/reference/packages/eden-git/src/eden_git/integrator.py:138), [integrator.py](/Users/ericalt/Documents/eden-worktrees/issue-116-client-per-resource-plan/reference/packages/eden-git/src/eden_git/integrator.py:238), [integrator.py](/Users/ericalt/Documents/eden-worktrees/issue-116-client-per-resource-plan/reference/packages/eden-git/src/eden_git/integrator.py:421), [integrator.py](/Users/ericalt/Documents/eden-worktrees/issue-116-client-per-resource-plan/reference/packages/eden-git/src/eden_git/integrator.py:592)), but the plan sketch lists `read_variant`, `integrate_variant`, and `read_task` instead ([refactor-f4-client-per-resource.md](/Users/ericalt/Documents/eden-worktrees/issue-116-client-per-resource-plan/docs/plans/refactor-f4-client-per-resource.md:1397)). Implemented literally, that bridge is broken.
- Concrete edit: Replace the sketch with a grep-derived method inventory for `eden_git` and `eden_dispatch`, then define the local Protocols/adapters from those actual method sets.

**Criterion 10 — Test Design**
- `Should-fix:` `test_storeclient_no_longer_implements_store_protocol` is specified as `isinstance(StoreClient(...), Store) is False`, but `Store` is a non-`runtime_checkable` Protocol ([protocol.py](/Users/ericalt/Documents/eden-worktrees/issue-116-client-per-resource-plan/reference/packages/eden-storage/src/eden_storage/protocol.py:76)), so that runtime check would raise `TypeError`, not return `False`. The plan already notes the Protocol is not runtime-checkable ([refactor-f4-client-per-resource.md](/Users/ericalt/Documents/eden-worktrees/issue-116-client-per-resource-plan/docs/plans/refactor-f4-client-per-resource.md:199)).
- Concrete edit: Drop the runtime `isinstance` test. If you want a runtime regression, assert the flat methods are gone; keep the actual Protocol-break check in `pyright` or a small type-only fixture.

**Criterion 9 — Wave Plan Ordering**
- `Should-fix:` The pyright-backstop claim is too strong for Wave 3. Wave 2 explicitly keeps the flat `StoreClient` surface alive ([refactor-f4-client-per-resource.md](/Users/ericalt/Documents/eden-worktrees/issue-116-client-per-resource-plan/docs/plans/refactor-f4-client-per-resource.md:1624)), so a missed `client.claim(...)` or `store: Store = client` site still type-checks until Wave 4 removes those methods ([refactor-f4-client-per-resource.md](/Users/ericalt/Documents/eden-worktrees/issue-116-client-per-resource-plan/docs/plans/refactor-f4-client-per-resource.md:1647)).
- Concrete edit: Amend §§1.6/6.3/8 so pyright is described as the final-wave backstop, not a Wave-3 completeness proof, or add a grep-based gate/reorder the waves so migration completeness is actually checkable before Wave 4.

**Criterion 1 — Resource → Sub-client Mapping Completeness**
- `Should-fix:` The method inventory is stale. The current `StoreClient` has 51 public instance methods excluding lifecycle/helpers, including worker/group APIs ([client.py](/Users/ericalt/Documents/eden-worktrees/issue-116-client-per-resource-plan/reference/packages/eden-wire/src/eden_wire/client.py:555)), reassign/dispatch/lifecycle ([client.py](/Users/ericalt/Documents/eden-worktrees/issue-116-client-per-resource-plan/reference/packages/eden-wire/src/eden_wire/client.py:740)), and checkpoint operations ([client.py](/Users/ericalt/Documents/eden-worktrees/issue-116-client-per-resource-plan/reference/packages/eden-wire/src/eden_wire/client.py:1014)). §1.1 still says “43 public methods,” and §4’s table includes a duplicate placeholder row for `reassign_task` ([refactor-f4-client-per-resource.md](/Users/ericalt/Documents/eden-worktrees/issue-116-client-per-resource-plan/docs/plans/refactor-f4-client-per-resource.md:1076)).
- Concrete edit: Update the counts to the current surface and remove the duplicate placeholder row so §4 is a literal 1-to-1 map.

**Criterion 4 — Caller Migration Enumeration**
- `Should-fix:` The migration inventory includes false positives. `reference/packages/eden-control-plane/tests/test_store_protocol.py` is `ControlPlaneStore` conformance coverage, not a `StoreClient`/`Store`-break site ([test_store_protocol.py](/Users/ericalt/Documents/eden-worktrees/issue-116-client-per-resource-plan/reference/packages/eden-control-plane/tests/test_store_protocol.py:1)), and repo grep does not show `StoreClient` usage under `reference/packages/eden-checkpoint` or `reference/packages/eden-control-plane/tests`.
- Concrete edit: Rebuild §§5.3–5.4 from explicit grep buckets and remove speculative/non-StoreClient entries from the main migration list.

**Criterion 7 — Exception Re-export Discipline**
- `Should-fix:` The plan conflates `eden_wire.client`, top-level `eden_wire`, and actual import sites. Current repo imports these exceptions from `eden_wire.client` only in [test_checkpoint_wire.py](/Users/ericalt/Documents/eden-worktrees/issue-116-client-per-resource-plan/reference/packages/eden-wire/tests/test_checkpoint_wire.py:31) and [test_wire_roundtrip.py](/Users/ericalt/Documents/eden-worktrees/issue-116-client-per-resource-plan/reference/packages/eden-wire/tests/test_wire_roundtrip.py:36). Separately, `eden_wire.__init__` re-exports four indeterminate exceptions and still documents `StoreClient` as Store-compatible ([__init__.py](/Users/ericalt/Documents/eden-worktrees/issue-116-client-per-resource-plan/reference/packages/eden-wire/src/eden_wire/__init__.py:5), [__init__.py](/Users/ericalt/Documents/eden-worktrees/issue-116-client-per-resource-plan/reference/packages/eden-wire/src/eden_wire/__init__.py:21)).
- Concrete edit: Narrow the import-preservation claim to `eden_wire.client` unless you explicitly want a top-level `eden_wire` surface change, and add `eden_wire/__init__.py` to the touched-files list for docstring cleanup.

**Other Criteria**
- Criterion 2: The F-4 resource boundaries do match the current F-3 split, including the `experiment_lifecycle` / `experiment_read` and `reference` carve-outs.
- Criterion 3: The underlying claim that `StoreClient` stops structurally satisfying `Store` once the flat methods are removed is correct.
- Criterion 5: `ClientTransport` is a reasonable factoring of the current `_request` / bearer-check / JSON helpers; I did not spot a load-bearing runtime issue in the proposed shape.
- Criterion 8: The chosen sub-client names and verb choices are mostly coherent; the plan already surfaces the few arguable ones as operator-call tradeoffs.
- Criterion 11: The risk section covers the main implementation hazards once the findings above are corrected.
- Criterion 12: The file-size and function-size targets look realistic.
- Criterion 13: The `import_`, `events`/`replay`, and `update_experiment_state` handling are internally consistent with the chosen surface.
- Criterion 14: The 2.5–3 day estimate is justified by the caller migration cost; the audit’s 0.5-day estimate assumed a delegation-preserving facade.
- Criterion 15: I did not spot a spec-contract or wire-shape pitfall from `CLAUDE.md` beyond the review findings above.

**Minor**
- `Minor:` §7.7 looks stale: the cited wire tests use injected `httpx.Client`/`MockTransport` instances rather than monkeypatching `StoreClient._request`, so that section should describe the real retarget shape more precisely.