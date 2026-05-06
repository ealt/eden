**Stopped At Level 4: Completeness**

- The authority model in [§3.7](/Users/ericalt/Documents/eden/docs/plans/eden-phase-12a-2-orchestrator-as-role.md:521) is broader than the wire/spec work listed in §5. The plan changes authority on several existing operations, not just the two new endpoints:
  - `create_task(kind=ideation|evaluation)` at [§3.7](/Users/ericalt/Documents/eden/docs/plans/eden-phase-12a-2-orchestrator-as-role.md:530)
  - `accept` / `reject` at [§3.7](/Users/ericalt/Documents/eden/docs/plans/eden-phase-12a-2-orchestrator-as-role.md:534)
  - `integrate_variant` at [§3.7](/Users/ericalt/Documents/eden/docs/plans/eden-phase-12a-2-orchestrator-as-role.md:535)

  But the files-to-touch entries for [§5.1](/Users/ericalt/Documents/eden/docs/plans/eden-phase-12a-2-orchestrator-as-role.md:725) and [§5.4](/Users/ericalt/Documents/eden/docs/plans/eden-phase-12a-2-orchestrator-as-role.md:757) only call out new `reassign_task` / `update_dispatch_mode` endpoint work. Existing wire sections for `POST /tasks`, `POST /tasks/{T}/accept`, `POST /tasks/{T}/reject`, and `POST /variants/{T}/integrate` in [spec/v0/07-wire-protocol.md:31](/Users/ericalt/Documents/eden/spec/v0/07-wire-protocol.md:31), [spec/v0/07-wire-protocol.md:37](/Users/ericalt/Documents/eden/spec/v0/07-wire-protocol.md:37), and [spec/v0/07-wire-protocol.md:86](/Users/ericalt/Documents/eden/spec/v0/07-wire-protocol.md:86) also need explicit authority edits, and the corresponding `eden_wire` server/client work should be named in §5.4. Right now the scope says those behaviors change, but the implementation/spec inventory does not fully carry them.

- The reserved-group bootstrap path is still under-specified in the files-to-touch and test inventory. The design says the `orchestrators` group exists by experiment creation time at [Decision 6](/Users/ericalt/Documents/eden/docs/plans/eden-phase-12a-2-orchestrator-as-role.md:160), and that the reference impl seeds the experiment creator into `admins` at [§3.5](/Users/ericalt/Documents/eden/docs/plans/eden-phase-12a-2-orchestrator-as-role.md:478) and [§3.7](/Users/ericalt/Documents/eden/docs/plans/eden-phase-12a-2-orchestrator-as-role.md:546). But in §5 the only concrete setup mention is the new env var at [§5.7](/Users/ericalt/Documents/eden/docs/plans/eden-phase-12a-2-orchestrator-as-role.md:787). I don’t see a listed code path that actually performs:
  - `register_group("orchestrators")`
  - `register_group("admins")`
  - `add_to_group(initial_admin, "admins")`

  Nor do I see tests that pin those bootstrap invariants. Since §3.8 depends on `add_to_group(self_id, "orchestrators")` succeeding at startup, this should be made explicit in §5 and §6 rather than left implicit in an env var.

**Overall Assessment**

Feasibility and alternatives look coherent now. I stopped at completeness because the plan’s scope is ahead of its implementation inventory: the authority changes to existing endpoints and the reserved-group bootstrap path both need to be carried through §5 and §6 explicitly. Once those are filled in, the next pass should be able to move to edge cases.