# eden-storage

Reference storage backends for the EDEN protocol
([`spec/v0/08-storage.md`](../../../spec/v0/08-storage.md)).

This package defines the [`Store`][store-protocol] structural interface
— the union of the task store, event log, and proposal/trial
persistence that chapter 8 §1, §1.7, and §2 specify — and ships two
backends that satisfy it:

- **`InMemoryStore`** (lives in
  [`eden-dispatch`](../eden-dispatch/), re-exported from here for
  convenience) — single-process, non-durable, suitable for tests and
  the Phase 5 dispatch loop.
- **`SqliteStore`** — single-process, SQLite-backed, **durable**
  across process restarts. The smallest backend that satisfies
  chapter 8 §3 (durability, read-after-write, crash recovery).

Both backends pass the same conformance scenarios
([`tests/`](tests/)); adding a third backend (Postgres, Gitea-adjacent,
…) is a matter of implementing the Protocol and running the suite.

[store-protocol]: src/eden_storage/protocol.py

## Scope (Phase 6)

- One Protocol, two backends, shared conformance.
- SQLite schema + migrations under
  [`_schema.py`](src/eden_storage/_schema.py).
- Restart-safety tests: close and reopen the SQLite store mid-experiment
  and assert all state and events survive.

Non-goals at this phase:

- No Postgres backend (Phase 12).
- No artifact store (Phase 10).
- No cross-process transport (Phase 8 owns the wire protocol).
- No role-scoped handles — a caller with access to the store can call
  any mutation method; role negative rules are enforced by the
  conformance suite (Phase 11), not by the storage layer.
