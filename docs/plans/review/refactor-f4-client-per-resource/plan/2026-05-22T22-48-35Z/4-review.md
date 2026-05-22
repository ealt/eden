**Overall Assessment**

Converged: ship. The round-3 should-fix items are all closed in the live plan: §7.1 now consistently uses “21 methods + 1 property,” §5.4.3 marks `eden-control-plane/tests/test_client.py` as removed, and §5.4.1’s trailing prose now names `test_wire_schema_parity.py`. I found one small precision nit only.

**Findings**

**Criterion 1 — Dispatch File Count**
- `Minor:` The plan now has the right dispatch method/property count, but three spots still say that surface is spread “across 5 files”; the current direct caller set is 4 source files (`driver.py`, `sweep.py`, `state_view.py`, `workers.py`), with the fifth file only appearing after F-4 adds `_dispatch_store.py`. docs/plans/refactor-f4-client-per-resource.md:1334,1403,1613; reference/packages/eden-dispatch/src/eden_dispatch/driver.py:43; reference/packages/eden-dispatch/src/eden_dispatch/state_view.py:87; reference/packages/eden-dispatch/src/eden_dispatch/sweep.py:32; reference/packages/eden-dispatch/src/eden_dispatch/workers.py:97.
- Concrete edit: Change “across 5 files” / “(5 files)” to “across 4 current caller files” or explicitly say “4 current caller files plus the new `_dispatch_store.py` bridge.”

**Other Criteria**
- Criterion 2: The stale “22” wording is fully cleaned up; §7.1’s live text matches the actual `eden_dispatch` surface of 21 methods plus the `experiment_id` property.
- Criterion 3: `reference/packages/eden-control-plane/tests/test_client.py` is correctly marked REMOVED; the file has no `StoreClient` references.
- Criterion 4: The §5.4.1 trailing prose now correctly names `test_wire_schema_parity.py`.
- Criterion 5: The plan’s touched-file notes for `reference/packages/eden-wire/src/eden_wire/__init__.py` and `reference/packages/eden-wire/README.md` remain source-accurate: both still describe `StoreClient` as `Store`-Protocol-compatible today.

**Minor**
- `Minor:` No new Must-fix or Should-fix issues surfaced; after the optional “4 vs 5 files” wording cleanup, this is ready to ship.