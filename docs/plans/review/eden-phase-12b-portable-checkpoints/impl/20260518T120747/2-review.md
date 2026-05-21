## Round 2 findings

### Remaining substantive issues
- `reference/packages/eden-wire/src/eden_wire/client.py:1189` — medium — the new import recovery probe fully untars the checkpoint via `extract_checkpoint()` just to read `manifest.json`. For large checkpoints, a dropped-response recovery now doubles local disk/I/O and can fail on temp-space exhaustion, turning an otherwise recoverable transport blip into `IndeterminateImport`. Fix: parse `manifest.json` directly from the tar bytes without extracting the full archive.

### Verified fixes
- Fix 1 verified: `reference/packages/eden-wire/src/eden_wire/server.py:1569` now returns `JSONResponse(status_code=201, ...)`, and the updated wire + conformance assertions for 201 are present and passing.
- Fix 2 verified: `reference/packages/eden-storage/src/eden_storage/_checkpoint.py:741` now calls `list_bundle_refs()` and rejects missing `refs/heads/<branch>` before the SHA reachability pass.
- Fix 3 verified: `reference/packages/eden-storage/src/eden_storage/_base.py:273` adds `_reseed_default_event_counter()`, `reference/packages/eden-storage/src/eden_storage/_checkpoint.py:646` calls it after `_apply_commit()`, and the new regression in `reference/packages/eden-storage/tests/test_checkpoint_storage.py:465` passes.
- Fix 4 verified: `spec/v0/10-checkpoints.md:143` softens §7 from v0 MUSTs to deferred/informative target shape and adds explicit v0 verbatim-passthrough behavior.
- Fix 5 verified: `reference/packages/eden-wire/src/eden_wire/client.py:1122` now catches `httpx.TransportError`, probes `GET /v0/experiments/{target_id}`, and implements the confirmed-success / divergence / indeterminate branches; the three new tests in `reference/packages/eden-wire/tests/test_checkpoint_wire.py:509`, `:543`, and `:618` pass.

### Overall assessment
Ready with small fixes. The five round-1 items are implemented correctly and the checkpoint-targeted suites I ran passed (`45 passed, 14 skipped` across the wire/storage tests; `19 passed, 1 skipped` across `conformance/scenarios/test_checkpoint_*.py`). The only new issue I found is the recovery probe’s full-archive extraction in the client, which is worth tightening before merge because it hits exactly the large-checkpoint, transport-blip path this fix was meant to harden.