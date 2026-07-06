# eden-storage

Reference storage backends for the EDEN protocol ([`spec/v0/08-storage.md`](../../../spec/v0/08-storage.md)).

This package defines the [`Store`][store-protocol] structural interface — the union of the task store, event log, and idea/variant persistence that chapter 8 §1, §1.7, and §2 specify — and ships three backends that satisfy it:

- **`InMemoryStore`** (lives in [`eden-dispatch`](../eden-dispatch/), re-exported from here for convenience) — single-process, non-durable, suitable for tests.
- **`SqliteStore`** — single-process, SQLite-backed, **durable** across process restarts. The smallest backend that satisfies chapter 8 §3 (durability, read-after-write, crash recovery).
- **`PostgresStore`** — the production-shaped backend (psycopg v3, SERIALIZABLE per-op); the Compose and Helm stacks run on it. `ensure_readonly_role` provisions the Phase 12a-1f `eden_readonly` substrate role.

All backends pass the same parametrized conformance scenarios ([`tests/`](tests/)); adding another backend is a matter of implementing the Protocol and running the suite.

The package also owns two adjacent surfaces:

- **Artifact backends** ([`artifact_backend.py`](src/eden_storage/artifact_backend.py)) — the chapter-7 §16 blob store behind the task-store-server's `--blob-backend file|s3|gcs` flag (`FileArtifactBackend`, `S3Backend`, `GcsBackend`; the cloud SDKs are optional extras `eden-storage[s3]` / `[gcs]`).
- **Checkpoint ops** ([`_checkpoint.py`](src/eden_storage/_checkpoint.py)) — the chapter-8 §1.9 `export_checkpoint` / `import_checkpoint` implementations backing the chapter-10 wire endpoints.

[store-protocol]: src/eden_storage/protocol.py

## Non-goals

- No cross-process transport ([`eden-wire`](../eden-wire/) owns the HTTP binding).
- No role-scoped handles — a caller with access to the store can call any mutation method; role negative rules are enforced at the wire/auth layer and by the conformance suite, not by the storage layer.
