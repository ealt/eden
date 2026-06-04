# Issue #166 impl-stage codex-review — round 0 (2026-06-04)

`codex review --base main` against the additive wire-artifact-transfer
branch (Waves 0-2 + 4). Four findings, all addressed in the same PR.

## [P1] §16 backend must not reuse `artifacts_dir`

`make_app` defaulted the §16 blob backend to `FileArtifactBackend(artifacts_dir)`.
But `artifacts_dir` (a) is mounted **read-only** for the legacy `/_reference`
serve route in the reference Compose stack (so every deposit `store()` would
fail), and (b) is shared with legacy `file://` writers + served by the path
route — so a worker who learned an opaque id could read deposited bytes through
`/_reference/...`, bypassing the §16.2 row ACL.

**Resolution:** `_resolve_artifact_backend` no longer derives from `artifacts_dir`;
it returns the explicit `artifact_backend` or an `InMemoryArtifactBackend`. The
task-store-server gains a dedicated **server-private writable** `--artifact-blob-dir`
flag that constructs the `FileArtifactBackend`. The legacy `--artifacts-dir`
(reference route) and the §16 blob root are now decoupled.

## [P2] Don't silently accept durable deposits on an in-memory backend

Without `--artifacts-dir` the old default stored bytes in-memory while persisting
the metadata row in SQLite/Postgres → a returned `artifacts_uri` survives restart
with metadata but no bytes (fetch 404; durability violation).

**Resolution:** the in-memory backend is now an explicit test/in-process default;
the task-store-server CLI **logs a warning** when `--artifact-blob-dir` is unset
that deposits are non-durable. Durable deployments set the flag.

## [P2] Subprocess binding described unshipped behavior

The Wave-0 rewrite of `worker-host-subprocess.md` §2.3/§4/§10 said the host
deposits over the wire + stamps `eden://artifacts/...`, but the hosts still write
`file://` (the cutover is deferred to #290) — the informative binding disagreed
with shipped behavior.

**Resolution:** reverted §2.3/§4/§10 to describe the current `file://` layout,
each with a clearly-marked *"Wire-transfer migration (issue #166)"* forward-note
pointing at the #290 cutover. The normative chapters (07 §16, 02 §1.5, 08 §5)
keep the new wire surface (those endpoints exist and work).

## [P3] Add the `artifact` table to readonly Postgres grants

The v8 `artifact` table wasn't in `_READONLY_GRANT_TABLES`, so `ensure_readonly_role`
would revoke prior privileges and never grant the readonly role SELECT on it.

**Resolution:** added `"artifact"` to `_READONLY_GRANT_TABLES` (metadata is
non-secret: opaque_id / created_by / size / content_type).

## Round 1

Re-ran `codex review --base main` after the fixes — no further findings
(convergence).
