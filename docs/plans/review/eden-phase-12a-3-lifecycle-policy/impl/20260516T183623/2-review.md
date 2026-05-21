**Overall assessment**
The round-1 blocker is resolved. `9b76d50` brings the new `emit_policy_error` wire surface back into spec/schema/test alignment: chapter 07 now codifies the operation and authority gate ([spec/v0/07-wire-protocol.md:44](//Users/ericalt/Documents/eden-worktrees/phase-12a-3-lifecycle-policy/spec/v0/07-wire-protocol.md:44), [spec/v0/07-wire-protocol.md:148](//Users/ericalt/Documents/eden-worktrees/phase-12a-3-lifecycle-policy/spec/v0/07-wire-protocol.md:148)), the request schema now exists and matches the implementation contract ([spec/v0/schemas/wire/policy-error-request.schema.json:1](//Users/ericalt/Documents/eden-worktrees/phase-12a-3-lifecycle-policy/spec/v0/schemas/wire/policy-error-request.schema.json:1)), and parity coverage now explicitly enforces that binding ([reference/packages/eden-wire/tests/test_wire_schema_parity.py:397](//Users/ericalt/Documents/eden-worktrees/phase-12a-3-lifecycle-policy/reference/packages/eden-wire/tests/test_wire_schema_parity.py:397)).

**Blocking issues**
None.

**Should-fix**
None.

**Nice-to-have**
None.

**Praise**
This is the right cleanup. The fix is narrow, authoritative, and closes the exact drift the prior round identified without inventing new surface area: spec prose, schema artifact, and parity tests now all point at the same contract.