# Plan: Wire-level artifact deposit/retrieve endpoint (issue #166)

> **Issue:** [#166](https://github.com/ealt/eden/issues/166) — Wire-level artifact
> upload endpoint (`POST` / `GET /v0/experiments/<id>/artifacts`) for distributed
> deployments. `priority:2-planned`, `cluster:durability`.

## 1. Context

EDEN's artifact substrate today assumes a **shared filesystem** between every
worker, the task-store-server, and the web-ui: the
`${EDEN_EXPERIMENT_DATA_ROOT}/artifacts/` bind-mount (surfaced as
`/var/lib/eden/artifacts/` inside containers). Each role's host process writes
artifact bytes directly into that mount and stamps a `file://` URI onto the
idea / variant / submission it produces. Reads go through the web-ui's
`GET /artifacts?uri=<file-uri>` route, which resolves the `file://` path against
its own copy of the same mount.

This works for a single-machine Compose demo and breaks for any distributed
deployment, exactly as the issue's table lays out: a worker on another machine
has no shared FS to write to; workers can read and overwrite each other's
submissions; worker-local bytes vanish on instance loss. The model was
discovered to have no answer during the 2026-05-22 manual demo ("what about a
worker on another machine?").

The fix is to move artifacts from "shared filesystem" to **wire-level
deposit/retrieve**: a worker `POST`s bytes to the task-store-server and gets back
an **opaque** `eden://artifacts/<opaque-id>` URI; anyone with read permission
`GET`s the bytes back. Workers never know (or need) the physical storage layout;
the server resolves the opaque id to actual storage behind a `Backend`
abstraction (local file today; S3 / GCS later).

The protocol already anticipates this. [`08-storage.md`](../../spec/v0/08-storage.md)
§5 specifies the **artifact store** abstractly — a conforming store MUST support
**Upload** (bytes + proposed identity → URI) and **Fetch** (URI → bytes), with
durability (§5.2), content-integrity / no-overwrite (§5.3, §5.4) contracts. What
does not yet exist is (a) an HTTP **binding** for those two operations in
[`07-wire-protocol.md`](../../spec/v0/07-wire-protocol.md), (b) a reference
**opaque-URI scheme** and server-side **Backend** abstraction, and (c) the
**auth model** that makes deposit attributable and retrieval access-controlled.
This plan delivers those three.

### Relationship to recently-shipped #168

Issue #168 ("Hierarchical artifacts substrate") shipped on 2026-05-31. It made
the **client-side** `file://` layout entity-hierarchical
(`ideas/<idea_id>/`, `variants/<variant_id>/{executor,evaluator}/`) and unified
the write path behind
[`reference/services/_common/src/eden_service_common/artifacts.py`](../../reference/services/_common/src/eden_service_common/artifacts.py)
(`ArtifactNaming`, `entity_artifact_dir`, `write_artifact_bundle`,
`predict_artifact_uri`). #166 **supersedes the client-stamped `file://` scheme**:
under wire-deposit the physical layout becomes the *server's* private business
(behind the opaque URI), so the hierarchical-path-builder and the
predict-then-write dance are retired client-side. The bundle-packaging logic
(text + uploads → `tar.gz` + `manifest.json`, issue #120) is **kept** but
re-pointed: it builds the blob *in memory* and the client deposits the blob,
rather than writing it to a shared disk. See §6 (Migration) and §8 (Risks).

## 2. Decisions captured before drafting

These three scope forks were surfaced to the operator before drafting (the
issue itself flags the first two as open). Each is recorded here with the
chosen default and its rationale so codex-review and operator-review can
challenge it directly rather than re-deriving it.

- **D-a. Scope vs #102 → deposit/retrieve half only.** This plan delivers the
  deposit/retrieve wire endpoints + opaque scheme + `Backend` abstraction +
  auth + call-site migration. The **content-addressed checkpoint substrate
  rewrite** ([#102](https://github.com/ealt/eden/issues/102):
  `artifacts/sha256/<hex>` materialization, promoting  <!-- rename-discipline:cite -->
  [`10-checkpoints.md`](../../spec/v0/10-checkpoints.md) §7 from deferred to
  MUST, rewriting `artifacts_uri` → `checkpoint:sha256:<hex>` on export) stays a
  **separately-sequenced issue**. Rationale: #102 couples to the git-bundle /
  checkpoint substrate plumbing (chapter 10 §6–§7) and the Phase 13d `Backend`
  work; merging it here would roughly double the chunk and entangle a shippable
  distributed-deployment win with checkpoint-portability work that has its own
  conformance level (`v1+checkpoints+artifacts`). The two share the `Backend`
  Protocol introduced here, which is the natural seam. **#166 makes #102
  strictly easier** (the opaque `eden://` URI + `Backend.load` give the exporter
  a clean "read bytes by URI" primitive to content-address against).

- **D-b. Read ACL → depositor + admin only (cross-role deferred).** The `GET`
  endpoint authorizes the **depositing worker** (matched on the artifact's
  `created_by`) and any **admin / `admins`-group** principal. The issue's
  richer model — "the role operating on the variant the artifact belongs to can
  read it" (e.g. an evaluator reading an executor's artifact for a variant under
  evaluation) — is **deferred** to a follow-up issue gated on
  [#143](https://github.com/ealt/eden/issues/143) (admin-by-promotion ACL  <!-- rename-discipline:cite -->
  scoping). Rationale: the cross-role grant needs a variant↔claim↔artifact
  ACL graph that #143's identity/promotion model has not yet pinned;  <!-- rename-discipline:cite -->
  secure-by-default (deny cross-worker reads except admin) is the correct v0
  posture and matches the issue's own security framing ("workers shouldn't read
  others' submissions unless explicitly granted"). Deferring it keeps this chunk
  free of the unplanned-ACL entanglement. **Note:** the orchestrator/integrator
  already reads variant artifacts during integration via its `orchestrators`
  bearer — that path is admin-class and continues to work; what's deferred is
  *peer-worker* cross-role reads.

- **D-c. Migration → hard cutover, no shim.** The write path stops emitting
  `file://` and stamps `eden://artifacts/<opaque-id>`; **all** reads go through
  the new `GET` endpoint; the web-ui `/artifacts?uri=...` route and the
  `file://` read machinery are **retired in the same plan**. Rationale: EDEN has
  no external users (CLAUDE.md "no backwards-compat shims in greenfield /
  pre-external-user projects"), and the issue's two statements ("`file://`
  continues to work read-side" vs. "the web-ui `/artifacts?uri` route gets
  retired") are reconciled in favor of the cleaner end-state — one URI scheme,
  one read path. Existing `file://` URIs already persisted in a running demo are
  not migrated; the cutover applies to newly-issued URIs and the smoke/e2e
  fixtures start fresh (volume-cleanup discipline already documented in AGENTS.md
  applies). Two URI schemes / two read paths live simultaneously is the
  antipattern we avoid.

- **D-d. `Backend` Protocol now (file + in-memory), S3/GCS deferred.** This plan
  introduces the `ArtifactBackend` Protocol with two reference backends:
  `FileArtifactBackend` (Compose default; writes opaque-id-named blobs under
  `--artifacts-dir`) and `InMemoryArtifactBackend` (tests). S3 / GCS / Azure
  backends are **deferred to Phase 13d** ([blob-backend plan](eden-phase-13d-blob-backend.md)),
  which already names the `eden-blob` package and the `blob.backend ∈ {file, s3,
  gcs}` chart knob. The Protocol shape here is the seam 13d plugs into.
  Rationale: the Protocol is cheap, makes the wire handler backend-agnostic from
  day one, and the in-memory backend is needed for tests regardless; building
  S3 now would block on 13d's substrate work and an AWS-credential CI story.

## 3. Background facts established by exploration

- **The artifact store is the task-store-server's concern.** It already owns
  `--artifacts-dir`
  ([`task-store-server/cli.py:112`](../../reference/services/task-store-server/src/eden_task_store_server/cli.py))
  and a non-normative read route
  `GET /_reference/experiments/{id}/artifacts/{path:path}`
  ([`eden-wire/routers/reference.py:64`](../../reference/packages/eden-wire/src/eden_wire/routers/reference.py))
  backed by the security-hardened descriptor-walk primitive
  [`_artifact_fd.open_and_read_artifact`](../../reference/packages/eden-wire/src/eden_wire/_artifact_fd.py).
  So the normative deposit/retrieve endpoints belong in **eden-wire** /
  task-store-server, and the existing read-hardening (path-traversal / symlink /
  size-cap guards, `Content-Disposition: attachment` + `nosniff`) is reusable.
- **Per-worker bearers already exist on the wire.**
  [`07-wire-protocol.md`](../../spec/v0/07-wire-protocol.md) §13.1 defines
  `<worker_id>:<secret>` and `admin:<token>` bearers; the dispatcher forwards the
  authenticated `worker_id` to the Store and stamps `created_by` from the
  principal
  ([`_dependencies.py:stamp_created_by`](../../reference/packages/eden-wire/src/eden_wire/_dependencies.py)).
  So the deposit endpoint's "record `created_by = worker_id`" is implementable on
  **existing** auth machinery — the #140 dependency in the issue concerns the
  *web-ui operator-as-worker* identity, **not** the wire-level deposit, which any
  registered worker bearer can drive today.
- **Current writers (all become deposit clients):** the web-ui ideator route
  ([`routes/ideator.py`](../../reference/services/web-ui/src/eden_web_ui/routes/ideator.py)),
  web-ui evaluator route
  ([`routes/evaluator.py`](../../reference/services/web-ui/src/eden_web_ui/routes/evaluator.py)),
  the ideator subprocess host
  ([`subprocess_mode.py`](../../reference/services/ideator/src/eden_ideator_host/subprocess_mode.py)),
  the executor/evaluator subprocess hosts, and the standalone `eden-manual` CLI
  ([`reference/scripts/manual-ui/eden-manual`](../../reference/scripts/manual-ui/eden-manual)).
  All call `write_artifact_bundle(...)` → `file://` URI today.
- **The web-ui reader** is
  [`routes/artifacts.py`](../../reference/services/web-ui/src/eden_web_ui/routes/artifacts.py)
  (`GET /artifacts?uri=…[&entry=…]`), plus `_read_inline_artifact` for inline
  markdown previews. Both resolve `file://` against `app.state.artifacts_dir`.
- **Storage chapter already has the abstract ops.**
  [`08-storage.md`](../../spec/v0/08-storage.md) §5.1 = Upload / Fetch with
  RFC-3986 URI requirement; §5.2 durability; §5.3/§5.4 no-overwrite. The §5.1
  "reference deployment note" currently points at the #168 hierarchical
  `file://` layout — that note is what #166 re-points at the opaque scheme.
- **The `eden://` authority is already in use** for the closed error vocabulary
  (`eden://error/<name>`, §9). `eden://artifacts/<opaque-id>` reuses the same
  scheme authority for a *resolvable locator* role. This needs a glossary note
  (the two roles are disjoint: error codes are non-resolvable type URIs; artifact
  URIs resolve via the `GET` endpoint).

## 4. Design

### D.1 The opaque URI scheme

The reference deployment issues `eden://artifacts/<opaque-id>` where
`<opaque-id>` is a server-minted, unguessable token (`secrets.token_hex(16)` →
32 hex chars; argparse-safe, URL-safe, no path separators). The URI is **opaque
to clients**: a client MUST NOT parse it for structure, MUST NOT assume it maps
to a filesystem path, and MUST resolve it only by presenting it to the `GET`
endpoint. The opaque id's grammar is `[0-9a-f]{32}` — single-segment, so it can
never carry a path-traversal payload (contrast the `_reference` route, which
accepts a client-supplied `{path:path}` and needs the full descriptor-walk
defense; the opaque-id route does not, because the client never names a path).

The scheme reuses the `eden://` authority already used by `eden://error/...`.
The two are role-disjoint and the glossary records this (§5 naming map).

### D.2 `ArtifactBackend` Protocol + reference backends

A structural Protocol (matching the `Store` Protocol style in
[`eden-storage/protocol.py`](../../reference/packages/eden-storage/src/eden_storage/protocol.py)):

```python
class ArtifactBackend(Protocol):
    def store(self, opaque_id: str, data: bytes) -> None: ...   # exclusive-create; FileExistsError on reuse
    def load(self, opaque_id: str) -> bytes: ...                # NotFound if absent
```

The Protocol is **bytes-in / bytes-out only**. All metadata (size, content-type,
`created_by`, experiment scoping) lives in the **Store** (D.3), not the backend,
so the backend stays a dumb blob store that S3/GCS can satisfy trivially in 13d.

- `FileArtifactBackend(root: Path)` — `store` writes `root / opaque_id` via the
  exclusive-create / atomic-rename idiom already in
  [`artifacts.py:_materialize_exclusive`](../../reference/services/_common/src/eden_service_common/artifacts.py)
  (no-overwrite per storage §5.4). `load` reads `root / opaque_id` with the
  opaque-id grammar validated first (hex-only ⇒ no traversal) — a *simplified*
  read vs. `_artifact_fd` since there's no client-supplied path, but it keeps the
  `O_NOFOLLOW` + regular-file + size-cap guards as defense-in-depth.
- `InMemoryArtifactBackend()` — a dict for tests / in-process posture.

Home: a new `eden-storage` module (`eden_storage/artifact_backend.py`) or a new
`eden-blob` package stub that 13d grows into. **Recommended:** put it in
`eden-storage` now (alongside the `Store` it pairs with) and let 13d extract
`eden-blob` if the dep surface justifies it — avoids creating a near-empty
package this chunk. (Codex: challenge this if 13d's plan already commits to
`eden-blob` existing.)

### D.3 Store-side artifact metadata

The wire handler needs per-artifact metadata for ACL + content-type on retrieval.
Add to the `Store` Protocol + all three backends (in-memory / sqlite / postgres):

```python
def create_artifact(self, *, opaque_id, created_by, size_bytes, content_type) -> None
def read_artifact(self, opaque_id: str) -> ArtifactMetadata    # NotFound if absent
```

`ArtifactMetadata` = `{opaque_id, created_by, size_bytes, content_type, created_at}`,
experiment-scoped exactly like every other Store row (the Store is per-experiment).
This is a **new protocol-owned row type**; it gets a JSON Schema +
Pydantic binding (schema-parity) and round-trips through checkpoint export/import
**only at the metadata level** (the bytes themselves ride the deferred #102
content-addressing — for v0 the checkpoint carries `artifacts_uri` verbatim per
chapter 10 §7, unchanged).

The metadata row is **not** transactionally bound to any idea/variant — deposit
happens *before* the idea/variant that references the URI exists (D.4). It is
attributed to `created_by` and that is the entire ACL key for D-b.

### D.4 Wire endpoints (new spec section)

Two endpoints, hosted by the task-store-server, added to
[`07-wire-protocol.md`](../../spec/v0/07-wire-protocol.md):

| Operation | HTTP | Path | Auth |
|---|---|---|---|
| `deposit_artifact` | `POST` | `/v0/experiments/{E}/artifacts` | worker |
| `fetch_artifact` | `GET` | `/v0/experiments/{E}/artifacts/{opaque_id}` | either (ACL-gated: depositor or admin) |

**Deposit** (`POST`): `Content-Type: multipart/form-data` with a single `file`
part (filename + content-type + bytes), per the issue's wire shape. The server:

1. Authenticates (worker-gated; admin bearers MAY also deposit and are attributed
   `created_by = "admin"` consistent with `stamp_created_by`).
2. Enforces a **configurable size cap** (`--max-artifact-bytes`, default well
   above the current 1 MiB inline cap — see D.6); rejects over-cap with a new
   `413 eden://error/payload-too-large`.
3. Mints `opaque_id`, calls `backend.store(opaque_id, data)` then
   `store.create_artifact(opaque_id, created_by, size_bytes, content_type)`.
4. Returns `201` with `{"artifacts_uri": "eden://artifacts/<opaque_id>",
   "size_bytes": N, "content_type": "..."}`.

Ordering note: deposit precedes the `create_idea` / `create_variant` /
`submit` that records the URI. A lost deposit response or a failed
subsequent create leaves an **orphaned** artifact (bytes + metadata with no
referencing object). This is not an atomicity violation (no client-asserted
identity to reconcile — contrast `integrate_variant`'s read-back ladder); orphans
accumulate and are swept by a future GC pass (deferred — §6). The deposit needs
**no** read-back ladder.

**Fetch** (`GET`): resolves `opaque_id` → `store.read_artifact` (404 if absent) →
ACL check (`created_by == principal.worker_id` OR principal is admin/`admins`;
else `403 eden://error/forbidden`) → `backend.load` → `200` with the bytes,
`Content-Type` from metadata, and the safe-delivery headers
(`Content-Disposition: attachment`, `X-Content-Type-Options: nosniff`) already
built by
[`_artifact_fd.artifact_response_headers`](../../reference/packages/eden-wire/src/eden_wire/_artifact_fd.py).

**Bundle viewing (`?entry=`).** The existing web-ui viewer streams one entry out
of a `.tar.gz` bundle without unpacking. Under wire-deposit the bundle is one
opaque blob. Two options (codex to weigh): (a) **client-side extraction** — the
web-ui `GET`s the whole blob and extracts entries in-process with the existing
`read_bundle_entry`; keeps the normative wire surface minimal (no `?entry=`); (b)
**server-side `?entry=`** — a non-normative reference query param on the `GET`
that streams one entry, preserving the no-full-download UX for large bundles.
**Recommended (a)** for the size class artifacts realistically hit, with the size
cap (D.6) bounding the fetch; flag (b) as the fallback if cap is raised high.

**Authority classification (§13.3).** Add an "artifacts" entry: `deposit_artifact`
is worker-gated (no group gate); `fetch_artifact` is either-auth with a
**handler-level ACL** (depositor-or-admin) rather than a blanket principal-class
gate. The §13.3 prose gains a sentence noting that `fetch_artifact` is the first
endpoint whose authorization is *row-scoped* (depends on the artifact's
`created_by`), not purely principal-class — distinct from the existing
group-gates. (Codex: this is a small but real extension of the §13.3 model; make
sure the prose frames it as "either-auth + per-row ACL" cleanly.)

### D.5 Spec / contract impact (concrete)

- **[`02-data-model.md`](../../spec/v0/02-data-model.md) §1.5 (URIs).** Add a
  paragraph: `artifacts_uri` is **opaque** from the client's perspective; the
  reference deployment issues `eden://artifacts/<opaque-id>` resolved server-side
  via the chapter-7 artifact endpoints. Keep the existing "scheme is
  implementation-defined; a conforming store MUST document which schemes it
  issues" — #166 satisfies that by documenting `eden://`. The
  `checkpoint:sha256:<hex>` cross-deployment paragraph is unchanged (still §7 /
  #102 territory).
- **[`07-wire-protocol.md`](../../spec/v0/07-wire-protocol.md).** New section
  **Artifact operations** (see §7 below for the numbering decision); new §9 error
  row `eden://error/payload-too-large` (413); new §13.3 classification entry +
  the row-scoped-ACL sentence. Conformance §12 producer list updated.
- **[`08-storage.md`](../../spec/v0/08-storage.md) §5.1 reference note.** Re-point
  from the #168 hierarchical `file://` layout to the opaque `eden://` scheme +
  server-side `Backend`. The abstract Upload/Fetch ops (§5.1) and §5.2–§5.4
  contracts are unchanged (they were written scheme-agnostic). Add a §5.5 (or
  extend §5.1) noting the reference deployment's artifact *metadata* row
  (`created_by` for attribution/ACL) — framed as a reference-binding detail since
  the protocol's artifact-store contract is byte-level.
- **Reference binding
  [`worker-host-subprocess.md`](../../spec/v0/reference-bindings/worker-host-subprocess.md)
  §2.3 / §10.** Today the subprocess writes hierarchical paths into a shared dir
  and the host stamps the `file://` URI. Under #166 the user subprocess writes to
  a **local staging path** (`EDEN_OUTPUT` / artifacts staging dir) and the **host
  deposits** the bytes over the wire, stamping the returned `eden://` URI. Update
  the binding to describe the staging-then-deposit flow and drop the
  hierarchical-layout prose (the layout is now server-internal).
- **Schemas.** New `spec/v0/schemas/wire/deposit-artifact-response.schema.json`
  (`{artifacts_uri, size_bytes, content_type}`, `additionalProperties: false`).
  The `POST` request body is `multipart/form-data` (binary) → no JSON request
  schema. New `spec/v0/schemas/artifact-metadata.schema.json` for the Store row.
  `idea.schema.json` / `variant.schema.json` `artifacts_uri` fields are unchanged
  (still RFC-3986 URI strings — `eden://...` validates).
- **Pydantic.** `DepositArtifactResponse` + `ArtifactMetadata` in `eden-contracts`
  with the `_common.py` strict/format/NotNone discipline; add both to
  `test_roundtrip.py` and the schema-parity corpus (`tests/cases.py`) with
  accept/reject fixtures per constraint.

### D.6 The size cap

The current `MAX_ARTIFACT_BYTES = 1 MiB`
([`_artifact_fd.py:29`](../../reference/packages/eden-wire/src/eden_wire/_artifact_fd.py))
is tuned for *inline markdown preview*, not for real artifacts (build logs,
coverage reports, screenshots, multi-file bundles routinely exceed 1 MiB). The
deposit endpoint needs a **separate, larger, operator-configurable** cap
(`--max-artifact-bytes`, default e.g. 100 MiB) enforced **streaming** (don't
buffer the whole multipart body before checking — read with a running counter and
abort past the cap) to avoid OOM on a hostile upload. The inline-preview 1 MiB
cap stays as-is for the *render* path (the viewer still caps what it inlines).
Over-cap deposit → `413 eden://error/payload-too-large`.

### D.7 Client-side refactor of `eden_service_common/artifacts.py`

The bundle-packaging logic is kept and re-pointed:

- `write_artifact_bundle(target_dir, naming, …) -> file:// URI` (writes to disk)
  → `build_artifact_bundle(naming, *, text_content, uploads) -> (bytes,
  content_type)` (builds the `tar.gz` / single-file / text blob **in memory**,
  returns bytes + MIME). The `tar.gz` + `manifest.json` packaging (issue #120) is
  unchanged; only the sink changes (memory, not disk).
- `predict_artifact_uri(...)` is **retired** — the URI is now the deposit
  response, not a pre-write prediction.
- `entity_artifact_dir(...)`, `idea_naming()` / `submission_naming()` headline
  policy: the **bundle headline** (`content.md` / `evaluation.md` / `variant.md`)
  stays (it's an intra-bundle entry name the viewer reads); the **on-disk
  directory layout** (`ideas/<id>/`, `variants/<id>/{executor,evaluator}/`) is
  retired — physical layout is server-internal now. `ArtifactNaming` shrinks to
  just the in-bundle naming.
- Callers (`deposit_artifact` client helper, used by web-ui routes + hosts + CLI)
  build the blob then `POST` it via the new `StoreClient.deposit_artifact`.

### D.8 StoreClient additions

[`eden-wire`'s client] gains `deposit_artifact(experiment_id, *, data,
filename, content_type) -> DepositArtifactResponse` and
`fetch_artifact(experiment_id, opaque_id) -> bytes`, both carrying the worker
bearer and the `X-Eden-Experiment-Id` header. These are what the hosts / web-ui /
CLI call instead of touching the filesystem.

## 5. Naming map (old → new) — surfaced for operator review

| Old / none | New | Kind | Rationale |
|---|---|---|---|
| `file://<artifacts-dir>/...` (client-stamped) | `eden://artifacts/<opaque-id>` (server-issued) | URI scheme | Opaque, deployment-local, resolved server-side. Reuses `eden://` authority (role-disjoint from `eden://error/`). |
| — | `deposit_artifact` | wire op / `StoreClient` method | Verb `deposit` + artifact noun. Matches issue's "deposit". |
| — | `fetch_artifact` | wire op / `StoreClient` method | Verb `fetch` + artifact noun; mirrors storage §5.1 "Fetch". |
| — | `ArtifactBackend` / `store` / `load` | Protocol + methods | Backend is a dumb blob store; `store`/`load` (not `upload`/`fetch`) signal byte-level, metadata-free. |
| — | `FileArtifactBackend` / `InMemoryArtifactBackend` | classes | Backend noun + substrate qualifier. |
| — | `create_artifact` / `read_artifact` | `Store` ops | Verb + artifact noun; matches existing `create_variant` / `read_variant` Store-op shape. |
| — | `ArtifactMetadata` | dataclass / schema | Artifact noun + metadata. |
| — | `DepositArtifactResponse` | Pydantic / wire schema | Operation + Response (matches `*Response` wire-model convention). |
| — | `eden://error/payload-too-large` | error type | New 413 closed-vocab entry. |
| `write_artifact_bundle` (disk sink) | `build_artifact_bundle` (memory sink) | `_common` helper | Sink changed disk→memory; "build" not "write" since nothing is written. |
| `predict_artifact_uri` | *(retired)* | — | URI now comes from the deposit response. |
| `--max-artifact-bytes` | *(new flag)* | task-store-server CLI | Deposit size cap, distinct from the 1 MiB inline-render cap. |

Glossary discipline: validate every identifier above against
[`docs/glossary.md`](../glossary.md) before introducing it; add a glossary entry
for the `eden://artifacts/` scheme noting role-disjointness from `eden://error/`.

## 6. Migration / cleanup

Hard cutover (D-c), no shim:

- **Retire** [`web-ui/routes/artifacts.py`](../../reference/services/web-ui/src/eden_web_ui/routes/artifacts.py)
  (`GET /artifacts?uri=…`) and the `_read_inline_artifact` `file://` resolver;
  re-home inline preview + entry extraction onto `StoreClient.fetch_artifact` +
  in-memory `read_bundle_entry`.
- **Retire** the `_reference/.../artifacts/{path}` route + most of
  [`_artifact_fd.py`](../../reference/packages/eden-wire/src/eden_wire/_artifact_fd.py)
  once the normative `GET` subsumes it (keep `artifact_response_headers` +
  `_build_content_disposition`; the descriptor-walk `open_and_read_artifact` is
  replaced by the simpler opaque-id read since the client no longer supplies a
  path).
- **Retire** the client-side hierarchical path machinery in
  `eden_service_common/artifacts.py` (`entity_artifact_dir`,
  `predict_artifact_uri`, disk-writing `write_artifact_bundle`) per D.7.
- **CLI flags / Compose / setup-experiment:** task-store-server keeps
  `--artifacts-dir` (now the `FileArtifactBackend` root) and gains
  `--max-artifact-bytes`. **web-ui `--artifacts-dir` is removed** (web-ui no
  longer touches the FS; it deposits/fetches over the wire). The
  `${EDEN_EXPERIMENT_DATA_ROOT}/artifacts/` bind-mount is **no longer shared** —
  only the task-store-server mounts it. Update
  [`setup-experiment.sh`](../../reference/scripts/setup-experiment/setup-experiment.sh),
  the Compose files, and the Helm chart (per the AGENTS.md substrate-migration
  audit rule: `grep -rn artifacts-dir`, `grep -rn EDEN_EXPERIMENT_DATA_ROOT`,
  `grep -rn /var/lib/eden/artifacts` and audit every hit — checkpoint scripts in
  particular, per the #178 silent-degradation lesson).
- **eden-manual CLI** (`reference/scripts/manual-ui/eden-manual`): switch its
  artifact writes from FS to `deposit_artifact` over its bearer.

**Deferrals (each filed as a GH issue at deferral time, per AGENTS.md):**

- #102 content-addressed checkpoint substrate (sequenced after #166; reference
  #102 directly).
- S3 / GCS / Azure `ArtifactBackend` → Phase 13d (reference the
  [blob-backend plan](eden-phase-13d-blob-backend.md)).
- Cross-role peer-worker read ACL → **new issue**, gated on #143.
- Orphaned-artifact GC (deposit-without-reference sweep) → **new issue**.
- web-ui operator-as-registered-worker deposit identity → tracked by #140
  (the web-ui's service bearer is used until #140 lands).

## 7. Conformance impact

- **New scenario group "Artifact transfer" at the `v1` level** (the endpoints are
  pure chapter-7 binding, no role contract needed). Add a row to
  [`09-conformance.md`](../../spec/v0/09-conformance.md) §5 v1 index citing
  `07-wire-protocol.md` (the new artifact section), §13.3, §9 and
  `08-storage.md` §5.1.
  Scenarios:
  - deposit returns `201` + `eden://artifacts/<id>` + correct `size_bytes` /
    `content_type`;
  - fetch by the depositor returns the exact bytes (content-integrity, storage
    §5.3);
  - fetch by admin succeeds;
  - fetch by a **different** worker → `403 eden://error/forbidden`
    (cross-worker isolation — the core security MUST);
  - fetch of an unknown opaque-id → `404 eden://error/not-found`;
  - over-cap deposit → `413 eden://error/payload-too-large`;
  - experiment-id header/path disagreement → `400` (§1.3 parity);
  - no-overwrite: a backend reuse attempt is server-internal — assert via the
    deposit-always-mints-fresh-id behavior, not a client-forced collision.
- **IUT-contract gate (chapter 9 §6).** Every observable above is reachable
  through the chapter-7 HTTP binding (deposit endpoint, fetch endpoint, auth
  headers, problem+json) — **no off-wire MUSTs**. The byte-content-integrity and
  no-overwrite storage MUSTs are asserted only via their *wire-observable
  projection* (fetched bytes == deposited bytes; fresh id per deposit), per the
  chunk-11d lesson that pure storage-internal artifacts aren't conformance-
  assertable. The cross-role-read grant is **explicitly out-of-scope / deferred**
  (D-b) and noted as such in the §5 entry.
- **`check_citations.py` three-legged traceability:** the new scenario file
  declares `CONFORMANCE_GROUP = "Artifact transfer"`, its docstring cites a
  `\bMUST\b`-bearing section of the new artifact wire section, and the citation
  lies within the declared group's §5 entry. Verify all three before merging the
  conformance wave.
- **Error-vocabulary closure** (§7 chapter-9 suite-level check) must observe the
  new `eden://error/payload-too-large` — ensure a scenario emits it.

### Spec section-numbering decision (flag for codex)

The new "Artifact operations" section in chapter 7 should be **appended as a new
§16, renumbering "Implementation latitude" §16 → §17**, to minimize §-xref churn
(only latitude's number moves). The semantically-cleaner placement (a new §5
"Artifact operations" alongside tasks/ideas/variants, pushing §5–§16 down) is
rejected because it renumbers eleven sections and every `§N` cross-reference into
them. Run `python3 scripts/spec-xref-check.py` after the edit regardless. Codex:
challenge the append-vs-insert tradeoff if the xref churn is smaller than
estimated.

## 8. Risks / things to watch

- **Deposit-before-reference ordering & the "starting variant" pitfall.** The
  AGENTS.md rule "any code path that produces a variant MUST `create_variant(
  status='starting')` before any observable repo write" is **not** broken by
  artifact deposit: the artifact store is not the repo, and the deposited
  artifact is attributed to `created_by` independent of any variant. But the
  *executor* flow must still create the starting variant before its observable
  *repo* write; depositing the executor's artifact can happen before or after the
  starting-variant create (it's variant-agnostic in the MVP ACL). Confirm the
  executor host's flow keeps the starting-variant step intact when we re-route
  its artifact write to deposit.
- **Orphan accumulation.** Lost deposit responses / failed subsequent creates
  leave referenced-by-nobody artifacts. Acceptable for v0 (no GC); the size cap
  bounds the blast radius. Deferred GC issue filed. Do **not** add a read-back
  ladder (no client-asserted identity to reconcile — unlike `integrate_variant`).
- **Streaming size enforcement.** A naive `await request.body()` buffers the whole
  upload before the cap check → OOM vector. Enforce the cap **during** multipart
  streaming with a running byte counter. This is the single most important
  correctness detail in the wire handler.
- **web-ui bearer for deposit.** The web-ui must authenticate its deposits. It
  uses its existing `StoreClient` bearer (service identity) until #140 gives
  per-operator worker identity. `created_by` will be that service principal —
  which means *all* web-ui-deposited artifacts share one `created_by`, so the
  depositor-only read ACL is effectively "web-ui can read web-ui's, admin can
  read all" for UI-deposited artifacts. Confirm this is acceptable for the MVP
  (it is, given admin can always read and the UI operator is admin-class in the
  current single-operator model) and note the #140 interaction.
- **The 1 MiB inline cap vs. the deposit cap.** Keep them distinct and clearly
  named so a future reader doesn't "unify" them and either OOM the renderer or
  reject legitimate large artifacts.
- **Smoke/e2e fixtures** carry `file://` URIs from prior runs; the hard cutover
  means fresh volumes. Apply the AGENTS.md volume-cleanup discipline; audit
  `smoke*.sh` / `e2e.sh` for any `file://`-shaped assertions.
- **Schema-parity strictness.** New `ArtifactMetadata` + `DepositArtifactResponse`
  must carry the full `_common.py` discipline (strict, format-on-both-sides,
  NotNone, round-trip, corpus accept/reject) — the schema-parity job is only as
  strong as both sides enforce.
- **Compose multipart through any proxy.** If a reverse proxy / the web-ui
  forwards deposits, confirm multipart bodies pass through without buffering
  limits that silently truncate (FastAPI/Starlette `UploadFile` spools to a temp
  file; verify the spool ceiling vs. the deposit cap).

## 9. Files to touch (by wave)

- **Spec:** `02-data-model.md` §1.5; `07-wire-protocol.md` (new §16 + §9 + §13.3);
  `08-storage.md` §5.1 note (+ §5.5); `09-conformance.md` §5; reference binding
  `worker-host-subprocess.md` §2.3/§10; new schemas under `spec/v0/schemas/` +
  `spec/v0/schemas/wire/`.
- **Contracts:** `eden-contracts` — `ArtifactMetadata`, `DepositArtifactResponse`,
  `_common.py` reuse, `tests/cases.py` + `test_roundtrip.py`.
- **Storage:** `eden-storage` — `artifact_backend.py` (`ArtifactBackend`,
  `FileArtifactBackend`, `InMemoryArtifactBackend`); `Store` Protocol +
  in-memory/sqlite/postgres `create_artifact` / `read_artifact`; migrations.
- **Wire:** `eden-wire` — new `routers/artifacts.py` (deposit + fetch handlers),
  `RouterDeps` gains the backend + cap, `server.py` wiring, §13.3 ACL helper,
  new error class; `StoreClient.deposit_artifact` / `fetch_artifact`; retire
  `_reference` artifact route + trim `_artifact_fd.py`.
- **Service-common:** `artifacts.py` refactor (D.7) → `build_artifact_bundle`,
  shrink `ArtifactNaming`, `deposit_artifact` client helper.
- **Services / CLI:** web-ui ideator/evaluator routes, retire
  `routes/artifacts.py` and the web-ui `--artifacts-dir`; ideator/executor/
  evaluator hosts (staging-then-deposit); task-store-server
  `--max-artifact-bytes`; `eden-manual` CLI.
- **Substrate:** `setup-experiment.sh`, Compose files (unshare the mount), Helm
  chart, `smoke*.sh` / `e2e.sh` audit.
- **Conformance:** new `conformance/scenarios/...` Artifact-transfer file +
  `check_citations` group.
- **Docs:** `CHANGELOG.md`, `docs/roadmap.md`, `docs/glossary.md`, review record.

## 10. Chunked execution plan (per-wave validation gates)

Waves are sequential; each wave is independently reviewable and could be split
into its own PR if the operator prefers (D-a keeps the whole thing scoped to one
issue). **Validation gate per wave runs the literal AGENTS.md "Commands" subset
named — never a narrowed equivalent.**

- **Wave 0 — Spec + schema + contracts.** All spec amendments (§4/§5/§7), new
  schemas, Pydantic models. *Gate:* `markdownlint-cli2`, `check-jsonschema`
  (metaschema + new schemas), `spec-xref-check.py`, `check-rename-discipline.py`,
  `pytest eden-contracts/tests/test_schema_parity.py` + roundtrip.
- **Wave 1 — `ArtifactBackend` + Store metadata.** Protocol + file/in-memory
  backends; `create_artifact` / `read_artifact` across all three Store backends.
  *Gate:* `ruff`, `pyright`, `pytest -q reference/packages/eden-storage/tests`
  (+ Postgres-DSN run), `check-complexity.py`.
- **Wave 2 — Wire endpoints + StoreClient.** Deposit/fetch handlers, streaming
  cap, ACL, error mapping; client methods. *Gate:* `ruff`, `pyright`,
  `pytest -q reference/packages/eden-wire/tests`, `check-complexity.py`.
- **Wave 3 — Client migration + cutover.** `artifacts.py` refactor; migrate all
  writers to deposit + readers to fetch; retire `/artifacts?uri` + `_reference`
  route + `file://` machinery; CLI/Compose/Helm/setup-experiment. *Gate:* **full**
  `uv run pytest -q`, then `smoke.sh`, `smoke-subprocess.sh`, `e2e.sh` (the
  smokes are the ones that catch `.env`/mount-shape regressions — do not skip).
- **Wave 4 — Conformance.** Artifact-transfer scenarios + `check_citations`.
  *Gate:* `pytest -q conformance/ -n auto`, `check_citations.py`, then the full
  lint/typecheck/pytest/smoke quartet.
- **Wave 5 — Docs PR.** `CHANGELOG.md [Unreleased]` entry (with issue links for
  every deferral phrase), `roadmap.md` status flip, `glossary.md` scheme entry,
  commit the impl-stage codex-review record under
  `docs/plans/review/issue-166/impl/<timestamp>/`. File the deferral issues
  (#102 sequencing, S3→13d, cross-role ACL→#143-gated, orphan-GC) **before**
  merging this wave. *Gate:* `markdownlint-cli2`.

## 11. Estimated effort

Large, matching the issue's ~3–4 week estimate, but the deferrals (D-a/D-b/D-d)
cut the critical path substantially: no S3, no checkpoint content-addressing, no
cross-role ACL graph. Waves 0–2 are the spec + substrate core (~1 PR each or one
combined); Wave 3 is the highest-risk (touches every writer/reader + Compose +
Helm) and is where the smoke/e2e budget lives; Waves 4–5 are mechanical. The
single hardest correctness detail is the streaming size-cap in Wave 2; the single
highest-blast-radius change is the mount-unsharing + cutover in Wave 3.
