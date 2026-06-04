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

Re-ran `codex review --base main` after the round-0 fixes — five more findings,
all addressed:

- **[P1] `Store` protocol widening broke `StoreClient` conformance.** Adding
  `create_artifact`/`read_artifact` to the shared `Store` Protocol made
  `StoreClient` (the HTTP client, which has no wire surface for those methods)
  structurally non-conforming → 12 `uv run pyright` errors at the
  ideator/executor/evaluator/orchestrator/web-ui assignment sites. (The per-package
  pyright runs in rounds 0-2 missed these — only the full-repo `uv run pyright`
  gate catches cross-package assignment.) **Fix:** moved the two methods to a
  separate `ArtifactStore` Protocol; the three concrete backends satisfy both, and
  the wire handler — which always operates against a concrete backend, never a
  `StoreClient` — `cast`s `deps.store` to `ArtifactStore`.
- **[P2] `error.schema.json` enum.** Added `eden://error/payload-too-large` to the
  wire error-envelope enum (it was in §9 prose + the impl map but not the schema, so
  the 413 problem body would fail schema validation).
- **[P2] `python-multipart` dependency.** Declared it explicitly in `eden-wire`'s
  `pyproject.toml` (Starlette's `request.form()` needs it; it was only present
  transitively via `eden-web-ui`, so a standalone wire-server install would fail
  every deposit at runtime).
- **[P2] Multipart parser errors → problem+json.** Wrapped `reparsed.form()` in
  try/except so a malformed multipart body (e.g. no boundary) raises `BadRequest`
  (problem+json `eden://error/bad-request`) instead of FastAPI's default
  `{"detail": ...}`. Added a regression test.
- **[P2] Stale hard-link temp file.** `FileArtifactBackend.store` now uses a unique
  `tempfile.mkstemp` temp instead of a fixed `.{id}.tmp`, so crash residue from a
  prior `store` can never be a hard link to a committed inode that a later write
  would truncate.

## Round 2

Re-ran `codex review --base main` after the round-1 fixes — two more findings,
both addressed:

- **[P2] Check the cap before appending the chunk.** `_read_body_capped` appended
  each chunk then checked the total, so a single over-limit chunk was buffered
  before rejection. Now checks `len(body) + len(chunk) > limit` before `extend`.
- **[P2] Preserve the recorded content type on fetch.** `Response(media_type=…)`
  makes Starlette append `; charset=utf-8` to a `text/*` type, mutating the
  recorded `content_type` §16.2 requires returning verbatim. Now sets the
  `Content-Type` header directly (no `media_type`). Added a regression test.

## Round 3

Re-ran `codex review --base main` after the round-2 fixes — one more finding,
addressed:

- **[P2] Reject ambiguous multi-part deposits.** §16.1 defines the body as exactly
  one `file` part; `form.get("file")` would silently pick one if a client sent
  multiple parts (or a `file` part plus stray fields), risking persisting bytes the
  caller didn't intend. The handler now requires `form.multi_items()` to be exactly
  one entry keyed `file` → else `BadRequest`. Added a regression test.

## Round 4

Re-ran `codex review --base main` after the round-3 fix — two findings:

- **[P1, fixed] `events()` after the `ArtifactStore` cast.** `test_no_event_emitted_for_artifact`
  cast `store` to `ArtifactStore` (which only exposes `create_artifact`/`read_artifact`)
  then called `store.events()` → `uv run pyright` fails `Attribute "events" is unknown`.
  (The per-file `pyright .../src` run missed it; the full-repo gate includes tests.)
  Fixed: keep `store` typed as `Store` for the event reads, `cast` only the
  `create_artifact` call.
- **[P1, declined-with-rationale] "require/disable deposits without a durable backend."**
  codex wants the CLI to error (or disable deposits) when `--artifact-blob-dir` is
  unset rather than warn-and-continue. Declined for this additive PR: (a) no writer
  deposits over the wire yet (the cutover is deferred to #290), so the stack never
  issues a 201 to lose; (b) erroring on a missing flag would break the existing
  Compose / manual `task-store-server` startup, which the additive PR must not
  regress; (c) the durable-blob volume is cutover wiring that belongs to #290. The
  warning is strengthened to spell out the 404-after-restart failure mode and that a
  durable deployment MUST pass the flag. Re-evaluate (require the flag) when #290
  wires the writers + the volume.

## Round 5

Re-ran `codex review --base main` after the round-4 fixes — two `StoreClient`
API-robustness findings, both addressed:

- **[P2] Multipart Content-Type for an injected client.** `StoreClient.deposit_artifact`
  used `files=`; a caller-injected `httpx.Client` with a default
  `Content-Type: application/json` would prevent httpx from emitting the multipart
  boundary (same trap as the conformance `WireClient`). Now encodes the multipart via
  a standalone `httpx.Request` and sends the raw content with the boundary
  Content-Type set explicitly (overriding any client default). Added a regression test.
- **[P2] `fetch_artifact` accepts the full opaque URI.** It required the bare id and
  spliced it into the path, so a caller passing the returned `eden://artifacts/<id>`
  URI got a 404. Now extracts the id as the URI's final path segment, accepting both
  the full URI and the bare id. Added a regression test.

## Round 6

Re-ran `codex review --base main` after the round-5 fixes — no further actionable
findings (convergence; remaining work is the deferred cutover tracked in #290).
