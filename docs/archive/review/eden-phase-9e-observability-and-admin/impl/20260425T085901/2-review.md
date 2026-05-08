No substantive findings in this pass.

[reference/services/web-ui/src/eden_web_ui/routes/admin.py](/Users/ericalt/Documents/eden/reference/services/web-ui/src/eden_web_ui/routes/admin.py:615) now has the right three-way split:
- `expected ... but is ...` → `?error=ref-changed`
- `unable to resolve reference` → `?error=not-found`
- anything else → re-raise

The partial-write coverage is now adequate in [reference/services/web-ui/tests/test_admin_partial_write.py](/Users/ericalt/Documents/eden/reference/services/web-ui/tests/test_admin_partial_write.py:196): it exercises CAS mismatch, vanished-ref-after-read, unexpected git failure, plus the earlier pre-delete not-found and not-eligible branches.

I reran `reference/services/web-ui/tests/test_admin_partial_write.py`; all 10 tests passed. The remaining risk is only the usual one for stderr-based classification: if git changes these messages, the tests will need updating, but that’s acceptable for this reference-stack surface.