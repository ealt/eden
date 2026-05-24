**Overall Assessment**

Iterate. Most round-1 issues were addressed, and the round-2 direction on the dispatch bridge is defensible, but a few revisions did not fully land and there are still inventory/verification inconsistencies that would make the implementation pass ambiguous.

**Findings**

**Criterion 1 — Dispatch Typing Strategy**
- `Should-fix:` The round-2 recommendation now favors a consumer-side dispatch bridge, but §5.3 still describes call-boundary adapters as “NOT recommended” and still inventories the integrator/dispatch rows as union-plus-`isinstance` work. §7.1 also still overstates the dispatch bridge as 22 methods and speculates `read_dispatch_mode` / `read_task` usage that does not appear in the actual dispatch files. docs/plans/refactor-f4-client-per-resource.md:1272-1291, 1535-1589; reference/packages/eden-dispatch/src/eden_dispatch/driver.py:220,273,318,384,406,426,432,459,493,511,522; reference/packages/eden-dispatch/src/eden_dispatch/sweep.py:40,62; reference/packages/eden-dispatch/src/eden_dispatch/workers.py:114,123,138,139,141,191,200,201,218,219,268,277,278,280; reference/packages/eden-dispatch/src/eden_dispatch/state_view.py:99-126.
- Concrete edit: Make §5.3 consistent with the chosen bridge posture, and replace the dispatch needs-list with the actual grep-derived surface. As written today, the bridge needs 21 unique methods, not 22, and `state_view.py` does not use `read_dispatch_mode` or `read_task`.

**Criterion 2 — Caller Inventory Cleanup**
- `Should-fix:` §5.4.2 is still mixing true `StoreClient` direct-caller files with files that only use in-process stores or `TestClient`. The clearest remaining false positives are `reference/services/control-plane/tests/test_server.py` and `reference/services/task-store-server/tests/test_artifacts_cli.py`, both of which are server-side test surfaces, not `StoreClient(` sites. docs/plans/refactor-f4-client-per-resource.md:1331-1347; reference/services/control-plane/tests/test_server.py:1-18; reference/services/task-store-server/tests/test_artifacts_cli.py:1-17.
- Concrete edit: Either remove those files from §5.4.2 or re-scope the section so it no longer claims to inventory only explicit `StoreClient` direct callers. The current heading/preamble and the listed files disagree.

**Criterion 3 — §4 Method-Count Intro**
- `Should-fix:` The footer recount is fixed, but the §4 intro still opens with the stale “43 methods mapped” wording. docs/plans/refactor-f4-client-per-resource.md:1132-1138.
- Concrete edit: Rewrite the §4 intro to match the corrected footer: 51 `StoreClient` methods map onto 43 server routes plus 8 client-only extras.

**Criterion 4 — §5.5 Import-Site Verification**
- `Should-fix:` The four-file import list looks complete, but the plan’s claimed verification command is not. The quoted `rg "from eden_wire(\\.client)? import.*Indeterminate|from eden_wire import.*Indeterminate"` only catches single-line imports; it does not match the multiline top-level imports in `test_lifecycle_wire.py` and `test_reassign_dispatch_wire.py`. docs/plans/refactor-f4-client-per-resource.md:1390-1399; reference/packages/eden-wire/tests/test_lifecycle_wire.py:27-31; reference/packages/eden-wire/tests/test_reassign_dispatch_wire.py:31-35.
- Concrete edit: Change the verification note to a multiline-capable search or state that the two top-level imports were verified manually from those files.

**Other Criteria**
- Criterion 5: The Wave-3 grep gate is materially improved; the suffix-based pattern does catch the previously missed shapes like `sender_sc.export_checkpoint(...)`, `receiver_sc.import_checkpoint(...)`, `admin_sc.terminate_experiment(...)`, `_probe.whoami(...)`, and `seed_client.integrate_variant(...)`.
- Criterion 6: The `eden-wire` README is correctly added to touched files, and the stated wording problem is real at `reference/packages/eden-wire/README.md:3`.
- Criterion 7: The integrator-specific stale `read_task` claim and the old `test_storeclient_no_longer_implements_store_protocol` name are otherwise gone from the operative plan text.

**Minor**
- `Minor:` The struck-through removed wire-test row still uses the nonexistent filename `test_schema_parity.py`; the real file is `test_wire_schema_parity.py`. docs/plans/refactor-f4-client-per-resource.md:1323.