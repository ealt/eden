No blocking findings.

**Missing Context**

Assessment: resolved. The plan now defines compatibility scope, inventories the live references, and clearly distinguishes live-source cleanup from historical changelog narration.

**Feasibility**

Assessment: sound. The replay/reconstruction check is now anchored to the reopened `SqliteStore.read_range()` path the current e2e test already uses, so the implementation fits the existing test structure cleanly.

**Alternatives**

Assessment: the chosen approach still looks right. Given the explicit in-tree-only compatibility policy, deleting `run_experiment` is better than preserving a shim that keeps the obsolete parallel path alive.

**Completeness**

Assessment: sufficient. The source, test, and doc edit inventory now matches the verification story, and the previously uncovered lifecycle-reconstruction invariant has a concrete replacement in the e2e test.

**Edge Cases and Risks**

Assessment: acceptable residual risk.

- Keep the `AGENTS.md` and `docs/roadmap.md` wording unmistakably historical so readers do not confuse “deleted in Phase 8c” with a current API.
- In the e2e replay fold, treat non-lifecycle events like `trial.integrated` as ignorable for the final-state map rather than as state transitions.

**Overall Assessment**

The plan is ready to implement. The remaining risk is normal execution discipline, not plan shape.