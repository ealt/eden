**Overall Assessment**

Iterate. The round-3 revisions fixed most of the round-2 issues, but the dispatch inventory is still internally inconsistent in §7.1, and one StoreClient false positive remains in the caller inventory. After those are cleaned up, this should be ready to ship.

**Findings**

**Criterion 1 — Dispatch Inventory**
- `Should-fix:` §5.3 and the top of §7.1 now agree on the consumer-side adapter posture, but §7.1 still carries stale round-2 counts in the option analysis: it still says “22 such methods,” “all 22 methods,” and “22+ sites,” even though the actual dispatch surface is 21 methods plus the `experiment_id` property. docs/plans/refactor-f4-client-per-resource.md:1572-1628; reference/packages/eden-dispatch/src/eden_dispatch/driver.py:220,273,318,384,398,406,409,411,426,432,459,493,522; reference/packages/eden-dispatch/src/eden_dispatch/sweep.py:40,62; reference/packages/eden-dispatch/src/eden_dispatch/workers.py:114,123,130,138,139,141,191,200,201,208,218,219,268,277,278,280; reference/packages/eden-dispatch/src/eden_dispatch/state_view.py:99,105,106,126.
- Concrete edit: Recount every remaining “22” reference in §7.1 to “21 methods + 1 property,” and make the option-B bridge description use that same count throughout.

**Criterion 2 — Caller Inventory Cleanup**
- `Should-fix:` The direct-caller inventory still retains `reference/packages/eden-control-plane/tests/test_client.py` as a maybe-zero migration site even though the file is a pure `ControlPlaneClient` test and does not import or construct `StoreClient`. docs/plans/refactor-f4-client-per-resource.md:1387-1394; reference/packages/eden-control-plane/tests/test_client.py:1-57.
- Concrete edit: Remove `reference/packages/eden-control-plane/tests/test_client.py` from §5.4.3 or mark it as a verified removed false positive, matching the treatment used for the other zero-hit files.

**Other Criteria**
- Criterion 3: §5.3 and §7.1 now agree on the operator-approved consumer-side adapter shape.
- Criterion 4: The §4 intro recount is corrected; 51 `StoreClient` methods mapping to 43 routes plus 8 client-only extras matches the current client surface.
- Criterion 5: §5.5 now documents a multiline-capable verification path, and the four `Indeterminate*` import sites listed are correct.
- Criterion 6: The round-2 §5.4.2 false positives (`control-plane/tests/test_server.py` and `task-store-server/tests/test_artifacts_cli.py`) are correctly removed.

**Minor**
- `Minor:` The `test_wire_schema_parity.py` filename cleanup is not fully complete; the §5.4.1 total still says `test_schema_parity.py`. docs/plans/refactor-f4-client-per-resource.md:1357-1359.