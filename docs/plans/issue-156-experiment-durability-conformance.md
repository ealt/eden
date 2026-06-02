# Issue #156 — Experiment-durability conformance harness + scenarios

## 1. Context

[`spec/v0/09-conformance.md`](../../spec/v0/09-conformance.md) §5 carries a
**placeholder row** in the v1 scenario-index table:

> | Experiment durability | Aggregate-over-substrates durability of protocol-owned
> state across process / host / substrate restart. **Scenario authoring deferred
> to a follow-up chunk** (a "stop-stack / kill-volume-mount / start-stack /
> replay" harness driving any conforming IUT). The placeholder row anchors the
> citation so future scenarios slot in here. | [`01-concepts.md`](../../spec/v0/01-concepts.md) §13 |

The row exists; the scenarios behind it do not. Today the chapter 01 §13
aggregate-durability invariant ("the union of protocol-owned state … MUST
collectively survive process restart, host restart, and individual
substrate-component restart") is asserted only by **reference-impl unit tests**
([`reference/packages/eden-storage/tests/test_restart_safety.py`](../../reference/packages/eden-storage/tests/test_restart_safety.py)),
NOT by the black-box conformance suite. A third-party IUT that silently lost
state on process restart would pass v1 conformance.

This chunk authors the deferred scenarios: a black-box **crash-restart** harness
that drives any conforming IUT through *seed → snapshot → SIGKILL → restart →
re-snapshot → assert-identical*, plus 4–6 scenarios that exercise the invariant
across the wire-observable protocol-owned state classes.

### 1.1 The unit-level model already exists — this is its wire-level projection

[`test_restart_safety.py`](../../reference/packages/eden-storage/tests/test_restart_safety.py)
is the proven model. It closes a `SqliteStore` mid-experiment, reopens it against
the same file, and asserts:

| Unit-test assertion | Wire-observable projection (this chunk) |
|---|---|
| `test_tasks_ideas_variants_and_events_survive` | tasks / ideas / variants / events survive crash-restart, read back through chapter-7 GETs |
| `test_claim_token_survives_reopen_and_authorizes_submit` | a `claimed` task stays claimed; the persisted credential authorizes a fresh `submit` post-restart |
| `test_event_order_total_and_preserved` / `test_event_id_counter_resumes_across_reopen` | the event log replays identically from cursor 0 and the counter resumes (new events append after the preserved prefix) |
| `register_worker` persistence + `credential_hash` survival | worker / group registry survives; a previously-issued worker bearer still authenticates |

The conformance scenarios are these same assertions driven **only** through the
chapter-7 HTTP binding, with the restart performed out-of-band by the adapter.

### 1.2 The IUT-contract restriction bounds what is assertable (chapter 9 §6)

Per [`spec/v0/09-conformance.md`](../../spec/v0/09-conformance.md) §6, the
chapter-7 HTTP binding is the **only** IUT contract a conformance harness can
rely on for *assertions*. The §13 invariant names state classes that are **not**
wire-observable:

- **git-side artifacts the integrator publishes** — same carve-out as
  v1+roles+integrator (§5 integrator rows; §4 line 36): git refs are not exposed
  through the chapter-7 binding.
- **artifact bytes** referenced by `artifacts_uri` — the URI is an opaque
  deployment-local string ([`02-data-model.md`](../../spec/v0/02-data-model.md)
  §1.5); the suite can assert the URI string round-trips, not that the bytes do.
- **host restart / individual-substrate-component restart** — a wire-only
  Python harness can SIGKILL the IUT process it spawned, but cannot reboot a
  host or pull a volume out from under a deployment.

So this chunk asserts the **wire-observable projection of §13 under process
restart**. Host-restart, substrate-component-loss, and git-side durability are
explicitly deferred to the **operations test layer** (the Compose/k8s smoke
scripts), exactly as the issue's "Out of scope" section directs. This is the
same scoping discipline applied when v1+roles+integrator scoped chapter 06 to
its wire-observable projection (the AGENTS.md "Conformance-plan MUSTs must be
filtered through the IUT contract" pitfall).

### 1.3 The restart is an adapter lifecycle action, not an asserted wire behavior

A subtlety worth pinning before drafting: the durability scenarios *rely on* a
capability beyond the wire — "restart the IUT." This does **not** violate §6's
"chapter-7 binding is the only contract" rule, because §6 governs what the
harness *asserts*, and the adapter already owns out-of-band lifecycle control
(`start` / `stop`). `crash_restart` is one more lifecycle primitive of the same
kind; every *assertion* in these scenarios is a chapter-7 read. The issue
anticipates exactly this: "The harness needs an IUT-adapter contract for 'stop
the IUT' + 'restart the IUT' beyond the existing claim/submit-driving
primitives."

## 2. Decisions captured before drafting

Listed so codex-review and future maintainers see what was deliberate:

1. **Wire-observable projection under process restart only.** Host-restart /
   substrate-loss / git-side artifact durability are out of scope (§1.2);
   deferred to operations smokes. The conformance level asserts the §13
   projection the chapter-7 binding can observe.

2. **`crash_restart` is an *optional* adapter capability, skip-if-absent.**
   Mirrors the control-plane optionality pattern
   ([`scenarios/conftest.py`](../../conformance/scenarios/conftest.py)
   `control_plane_base_url` → `pytest.skip`). An IUT whose adapter does not
   implement the capability skips the durability group rather than failing it —
   the capability is a test-harness convenience, not a normative wire contract.
   (Decision to surface: see §3 D.1 for the Protocol shape.)

3. **The reference adapter switches from `:memory:` to a per-instance
   file-backed SQLite.** A restart cannot recover in-memory state, so the
   reference adapter MUST start against a durable store. Recommendation:
   always-file-backed (simplest; removes dual-mode branching), with a measured
   suite-wall-clock gate to confirm no material regression. Alternative
   (durable-only subclass) is weighed in §3 D.2 and §11 risks — **surfaced for
   operator/codex review.**

4. **Durability ≠ checkpoint round-trip.** The v1+checkpoints round-trip group
   deliberately *normalizes* claimed→pending and *reissues* credentials
   ([`10-checkpoints.md`](../../spec/v0/10-checkpoints.md) §8/§9). A crash-restart
   against the same substrate does the **opposite** — it preserves the claim
   record verbatim (including claimant + expiry) and the credential hash. The
   durability scenarios assert *verbatim preservation*, which is a distinct (and
   in places stricter) contract than the checkpoint round-trip. See §3 D.6.

5. **Seed via direct wire writes; keep the group at v1.** The §5 placeholder
   sits in the **v1** table, so the durability group stays v1 (every v1 IUT must
   pass it — maximal coverage). Representative state is seeded through v1 wire
   surfaces (tasks, claims, workers, groups, events, dispatch_mode,
   experiment-state). Ideas/variants are seeded through their direct
   `POST /ideas` / `POST /variants` endpoints (the same endpoints the parallel
   v1+checkpoints round-trip group already drives) to make the snapshot
   representative; the assertion keys off §13 (state survives), not off role
   semantics. **Surfaced for codex** in case it reads idea/variant seeding as
   role-level — fallback is to drop idea/variant from the strict-v1 scenarios
   and keep them in a v1+roles-tagged scenario (§9 notes the split point).

6. **The chunk also discharges (or trims) the lifecycle row's unbacked
   restart claim.** Chapter 9 §5's v1+roles "Experiment lifecycle" row already
   lists "State survives store restart" in its scope — but
   [`test_experiment_lifecycle.py`](../../conformance/scenarios/test_experiment_lifecycle.py)
   contains **no** restart assertion (verified: no `restart`/`reopen`/`survive`
   token in the file). That is a pre-existing scope-claim drift. This chunk
   resolves it: a "terminated-experiment durability" durability scenario backs
   the survival claim, and the lifecycle row's "State survives store restart"
   clause is **trimmed** (consolidating all restart-durability assertions under
   the durability group as the single source of truth). See §5, §9.

## 3. Design

### D.1 Adapter capability extension — `crash_restart`

The [`IutAdapter`](../../conformance/src/conformance/harness/adapter.py)
Protocol gains an **optional** restart capability. Two equivalent shapes; the
plan recommends the marker-Protocol form for `isinstance`-clean detection:

```python
@runtime_checkable
class RestartableIutAdapter(IutAdapter, Protocol):
    """Adapter that can crash-restart its IUT against the same durable substrate.

    Optional capability for the chapter-01 §13 durability scenarios. An
    adapter that does not implement it causes the durability group to
    skip (the capability is a harness convenience, not a wire contract —
    chapter 9 §6).
    """

    def crash_restart(self) -> IutHandle:
        """SIGKILL the IUT and bring it back up against the SAME durable
        substrate. Returns a fresh handle (the base_url MAY change — a
        re-bound ephemeral port — but experiment_id, admin_token, and the
        worker-credential validity are preserved across the restart)."""
        ...
```

Detection in the fixture: `isinstance(adapter, RestartableIutAdapter)` → drive
the scenario; else `pytest.skip("IUT adapter does not support crash_restart")`.

This is a **harness-internal** change. Per chapter 9 §6 the adapter shape is
informative; adding a capability is not a normative wire-contract change. §6's
informative prose gains one paragraph documenting the optional capability (§5
spec impact).

### D.2 Persistent substrate for the reference adapter

The reference adapter
([`adapters/reference/adapter.py`](../../conformance/src/conformance/adapters/reference/adapter.py))
currently spawns `eden_task_store_server --store-url :memory:`. In-memory state
cannot survive the process death a restart requires. The task-store-server CLI
already accepts a durable URL
([`cli.py`](../../reference/services/task-store-server/src/eden_task_store_server/cli.py)
`--store-url 'sqlite:///<path>'`).

**Recommended: always file-backed.** Replace `:memory:` with a per-instance
SQLite file under an adapter-owned temp dir (`tempfile.mkdtemp()`), cleaned in
`stop()`. `crash_restart()` then re-spawns against the *same* `--store-url`,
`--experiment-id`, `--experiment-config`, and `--admin-token`. This removes any
dual-mode branching — every scenario gets a durable store; restart is "kill +
re-spawn against the same file."

Rationale and the tradeoff to weigh in review:

- **Pro:** one code path; no per-scenario mode flag; mirrors how
  `eden-storage`'s own parametrized conformance tests run against `SqliteStore`
  (a file) rather than memory.
- **Con:** every one of the ~56 existing scenario files now uses a file-backed
  store instead of `:memory:`. For the tiny per-scenario datasets the SQLite
  cost is negligible, but this perturbs the whole suite's behavior, so it MUST
  be validated: measure `uv run pytest -q conformance/ -n auto` wall-clock
  before/after; if the regression is material, fall back to the alternative.
- **Alternative (durable-only subclass):** keep `:memory:` as the default
  `ReferenceAdapter`; add a `DurableReferenceAdapter(ReferenceAdapter)` that
  overrides the store URL and implements `crash_restart`, selected by the
  durability scenarios via a dedicated fixture. Downside: a non-reference IUT
  pointed at via `--iut-adapter` only gets the durability scenarios if *its*
  adapter implements `crash_restart` anyway, so the subclass split only helps
  the reference adapter — and it reintroduces dual-mode. **Recommend
  always-file-backed; surface the subclass option for the operator.**

The `:memory:`-for-speed comment in the adapter docstring is a v0 choice with no
external users; per the CLAUDE.md no-backwards-compat-shims posture we rewrite it
rather than preserve a memory/file fork.

### D.3 Crash semantics — SIGKILL, not graceful terminate

`stop()` today does graceful `terminate()` → `kill()` escalation. The restart
MUST use **SIGKILL directly** (`proc.kill()`), no graceful drain — the §13
invariant is durability *under crash*, not durability under orderly shutdown.
Testing graceful shutdown would let a buffer-flush-on-SIGTERM implementation
pass while still losing data on a real crash. The durability guarantee is that
*acknowledged writes are durable at the storage layer* (SQLite's per-transaction
journaling makes committed transactions survive process death), so SIGKILL is
the meaningful signal.

Implementation note: re-use the existing `_read_port_announcement` /
`_start_stderr_drain` machinery for the re-spawn (the AGENTS.md subprocess-adapter
lifecycle pitfalls — `token_hex` not `token_urlsafe`, daemon-thread port read,
partial-startup cleanup — all apply identically to the re-spawn path).

### D.4 Port-change rebind + worker-bearer carry-over

`crash_restart()` re-spawns with `--port 0`, so the new IUT binds a **fresh
ephemeral port**; the returned `IutHandle.base_url` changes. The scenario's
`WireClient` is bound to the old URL and must be rebuilt against the new handle.
The trap: the worker bearers the `default_workers` autouse fixture registered
live on the *old* client, and re-registration post-restart is idempotent and
returns **no** token on the second call (the worker already exists), so the new
client cannot recover the bearer by re-registering.

The mechanism already exists:
[`WireClient.copy_worker_bearers_from(other)`](../../conformance/src/conformance/harness/wire_client.py).
The crash-restart fixture:

1. captures the pre-restart `WireClient` (carrying the registered bearers),
2. calls `adapter.crash_restart()` → new handle,
3. constructs a new `WireClient` against the new `base_url` + same
   `experiment_id` + same `extra_headers` (admin bearer — `admin_token`
   unchanged across restart),
4. `new_client.copy_worker_bearers_from(old_client)` so `as_worker=<wid>` calls
   keep authenticating (the server's persisted `credential_hash` still matches
   the plaintext bearer the old client held),
5. yields `(old_snapshot_client, new_client)` to the scenario.

This is the wire-level analogue of the unit test's "persisted token authorizes a
fresh submit on the reopened store."

### D.5 State-capture / replay model

A shared snapshot helper (new module
[`conformance/src/conformance/harness/_durability.py`](../../conformance/src/conformance/harness/_durability.py),
or an addition to [`_seed.py`](../../conformance/src/conformance/harness/_seed.py))
captures the full wire-observable protocol-owned state into a normalized,
comparable structure:

```python
def capture_wire_state(client: WireClient) -> dict[str, Any]:
    """Snapshot every wire-observable protocol-owned state class.

    Reads (all chapter-7 GETs): /tasks (+ each task's claim), /ideas,
    /variants, /workers, /groups, /events (from cursor 0), /dispatch_mode,
    experiment /state. Returns a dict keyed by class, with lists sorted by
    id so the comparison is order-independent where the spec does not
    mandate order, and order-preserving for /events (which the spec DOES
    order — chapter 05 §2.2).
    """
```

The durability assertion is `capture_wire_state(pre) == capture_wire_state(post)`
for the capstone scenario, and targeted per-class equality for the focused
scenarios. Care points the helper must encode (surfaced for review):

- **Event log is order-significant**; everything else compares as id-keyed sets
  (the wire does not promise list order on `/tasks` etc.).
- **No normalization.** Unlike the checkpoint round-trip helper, the durability
  comparison does **not** strip claims or remap ids — it asserts byte-for-byte
  field equality of the wire projection (§3 D.6).
- **`registration_token` is never in a read body** (the §6.2 read endpoints omit
  credentials — chapter 9 §5 "read / list endpoints don't leak credentials"), so
  the snapshot naturally excludes secrets; no special handling needed.

### D.6 Durability ≠ checkpoint: claims and credentials preserved verbatim

The single most important contrast to keep straight, because a reviewer who
pattern-matches to the checkpoint round-trip group will get it backwards:

| | v1+checkpoints round-trip | v1 durability (this chunk) |
|---|---|---|
| Claimed task | normalized → `pending` (§8) | **preserved** — stays `claimed`, same claimant + expiry |
| Worker credential | **reissued** (new token on import, §9) | **preserved** — same `credential_hash`; old bearer still valid |
| Event ids | MAY differ across impls | **preserved** — same store, same ids; counter resumes |
| experiment_id | MAY be rewritten via `as_experiment_id` | **preserved** — same store identity (reopen-with-wrong-id is *rejected*, [`test_restart_safety.py`](../../reference/packages/eden-storage/tests/test_restart_safety.py) `test_reopen_with_wrong_experiment_id_rejected`) |

The durability contract is "same substrate, nothing lost, nothing transformed."
The checkpoint contract is "portable copy, deliberately transformed." Authoring
the durability scenarios by copy-pasting the checkpoint helpers and forgetting to
*remove* the normalization would silently weaken the durability assertion — call
this out in the scenario docstrings.

### D.7 Conformance level placement

The durability group is **v1** (§2 decision 5): it sits in the §5 v1 table and
every v1 IUT must pass it. The seed path stays within v1 wire surfaces; the
idea/variant seeding (used to make the snapshot representative) rides the same
direct-POST endpoints the parallel v1+checkpoints group already exercises. The
plan flags (§9) the one scenario that leans hardest on idea/variant seeding so
codex can rule on whether it should be v1 or carry a `pytest.mark` for a
roles-gated subset.

## 4. Scope

**In scope:**

- An optional `crash_restart` capability on the conformance adapter Protocol
  (§3 D.1) + reference-adapter implementation (§3 D.2–D.4): persistent SQLite
  substrate, SIGKILL re-spawn against the same store, fresh-handle rebind.
- A crash-restart pytest fixture (skip-if-unsupported) + worker-bearer
  carry-over via `copy_worker_bearers_from` (§3 D.4).
- A `capture_wire_state` snapshot helper (§3 D.5).
- 4–6 durability scenarios in
  [`conformance/scenarios/test_experiment_durability.py`](../../conformance/scenarios/test_experiment_durability.py),
  replacing the single skipped placeholder (§9).
- Spec edits: chapter 9 §5 durability-row prose (scope to the wire-observable
  projection under process restart, defer the rest); chapter 9 §3 vocabulary-list
  fix (add 01-concepts §13); chapter 9 §6 informative note on the optional
  restart capability; trim the lifecycle row's unbacked "State survives store
  restart" clause (§5).
- `check_citations` continues to pass (the durability group's §5 row already
  cites 01 §13, which carries a MUST — §6 verifies this).
- CHANGELOG `[Unreleased]` entry + `docs/roadmap.md` pointer at impl-completion.

**Out of scope (deferred — file as GH issues per the CLAUDE.md deferral rule if a
deferral phrase lands in the CHANGELOG entry):**

- **Host-restart / substrate-component-loss / `rm -rf data root` durability.**
  Operations-test-layer concern (Compose/k8s smoke scripts), per the issue's
  "Out of scope" and §1.2. A wire-only Python harness cannot reboot a host.
  *(Candidate follow-up issue: an operations-layer durability smoke that brings
  the Compose stack down ungracefully, wipes a non-data volume, and re-asserts
  state — distinct from the existing checkpoint smoke.)*
- **Git-side integrator-artifact durability** (squash refs, evaluation manifests,
  `work/*` branches). Not wire-observable — same carve-out as
  v1+roles+integrator (chapter 9 §4 line 36). Deferred to a hypothetical
  "conformance + git access" binding.
- **Artifact-byte durability.** `artifacts_uri` is opaque; the suite asserts the
  URI round-trips, not the bytes (§1.2).
- **Performance / time-bounded-recovery assertions.** The issue scopes this as a
  correctness scenario, not a latency one.
- **Multi-experiment / control-plane lease durability across restart.** A
  v1+multi-experiment concern (chapter 11 lease state); if worth asserting, it is
  a parallel-level durability scenario, not part of the v1 group. Note for a
  possible follow-up.

## 5. Spec / contract impact

All edits are in [`spec/v0/09-conformance.md`](../../spec/v0/09-conformance.md)
plus a one-clause trim; **no JSON-schema, Pydantic-model, or wire-binding field
changes** (the chapter-7 surface is unchanged — durability is asserted entirely
through existing GETs + an out-of-band restart).

1. **§5 durability-row prose (rewrite the placeholder row).** Replace the
   "deferred to a follow-up chunk" text with the now-authored scope, scoped to
   the wire-observable projection under process restart, explicitly deferring
   host-restart / substrate-loss / git-side artifacts. Model the wording on the
   v1+roles+integrator row (line 36 / lines 91–92), which already scopes a group
   to "the wire-observable projection of …, the [off-wire bits] are not asserted
   by a wire-only suite." Citation stays `01-concepts.md §13`.

2. **§3 assertion-vocabulary list (add 01-concepts §13).** §3 currently says
   "Every assertion in the v1 suite is keyed off a normative MUST in 02 / 04 /
   05 / 07 / 08." The durability group keys off **01-concepts §13**, which is not
   in that list — a latent inconsistency the moment the scenarios land. Amend the
   sentence to name 01-concepts §13 as the one v1 assertion source outside the
   task-store/wire chapters, with a half-line explaining why (the *aggregate*
   invariant lives in concepts; its *per-store components* live in 08 §3, which
   IS already in the vocabulary). This is the spec-consistency fix the
   CLAUDE.md inter-chapter-restatement discipline calls for.

3. **§6 informative adapter prose (one paragraph).** Document the optional
   `crash_restart` capability: an IUT MAY implement it for the durability
   scenarios; IUTs that don't cause the group to skip. Frame it as a lifecycle
   primitive of the same kind as start/stop, with all *assertions* remaining
   chapter-7 reads (§1.3). **This is an informative addition consistent with §6,
   NOT a change to the §6 IUT-contract boundary** (the "chapter-7 binding is the
   only contract we *assert* against" statement is untouched). Per the task's
   stop conditions, a §6 *boundary* change would require surfacing to the
   operator before finalizing — this edit is deliberately *not* that; flagged
   here so review can confirm the distinction holds.

4. **§5 lifecycle-row trim (consolidation).** Remove "State survives store
   restart" from the v1+roles "Experiment lifecycle" row scope (it is currently
   unbacked — §2 decision 6). The terminated-experiment durability scenario in
   this chunk's group is the single backing assertion; cross-reference it in the
   durability row rather than duplicating the claim in two §5 rows (the
   inter-chapter/​intra-chapter restatement-drift trap).

**Spec-amendment stop-condition check:** none of edits 1–4 changes the chapter 9
§6 IUT-contract boundary (the chapter-7-only-contract rule). Edit 3 *adds to* the
informative adapter surface, consistent with §6. If review concludes edit 3 reads
as a §6 boundary change, **stop and surface to the operator** before finalizing
(per the task's stop conditions).

## 6. Naming map

Per the CLAUDE.md naming discipline, validated against
[`docs/glossary.md`](../../docs/glossary.md). This chunk introduces **harness**
identifiers only — no protocol vocabulary, no wire enum / CLI flag / env var, so
the `rename-discipline` gate is unaffected.

| Concept | Identifier | Basis |
|---|---|---|
| Optional restart-capable adapter Protocol | `RestartableIutAdapter` | extends `IutAdapter`; "Restartable" adjective + existing `IutAdapter` noun |
| Crash-restart lifecycle method | `crash_restart()` | verb-coherent with `start` / `stop`; "crash" qualifies the restart as ungraceful (§3 D.3) |
| Full-state snapshot helper | `capture_wire_state()` | verb `capture` + the thing captured (`wire_state`); not `snapshot` (noun-as-verb) |
| Crash-restart fixture | `crash_restart` (fixture) / `durable_clients` | fixture yields the pre/post client pair |
| New harness module (if split from `_seed`) | `harness/_durability.py` | underscore-prefixed harness-internal, parallel to `_seed.py` |

Naming note for review: `crash_restart` (not `restart` alone) is deliberate — the
ungraceful-SIGKILL semantics are the *point* (§3 D.3); a bare `restart` would
read as a graceful bounce. No glossary term is added (these are harness internals,
not protocol concepts); if codex judges any of these warrant a glossary line, add
it under the conformance-harness vocabulary.

## 7. Conformance impact

This chunk *is* a conformance-suite change, so "impact" here means the §-reference
and `check_citations` accounting:

- **`check_citations`** ([`tools/check_citations.py`](../../conformance/src/conformance/tools/check_citations.py)):
  the durability scenario file declares `CONFORMANCE_GROUP = "Experiment
  durability"` (already the placeholder's value) and each test docstring's first
  line cites `spec/v0/01-concepts.md §13`. §13 contains a `MUST`
  ("MUST collectively survive process restart …"), and the §5 durability row's
  cited section is exactly `01-concepts.md §13`, so all three citation legs
  (group identity + MUST citation + group-relevance) pass. **No `check_citations`
  code change** — the existing placeholder already establishes the group↔citation
  mapping; we add real tests under the same group.
- **No new `CONFORMANCE_GROUP`** — the "Experiment durability" group already
  exists in §5; we populate it.
- **Level**: v1 (§3 D.7). CI's `conformance` job already runs the v1 group; the
  new scenarios run automatically. The skip on the placeholder is removed.
- **The `_meta/test_self_validation.py`** harness-teeth check is unaffected (the
  misbehaving adapter does not implement `crash_restart`, so it skips the
  durability group cleanly — confirm in validation).

## 8. Migration / cleanup

Per the CLAUDE.md no-backwards-compat-shims posture (the conformance suite has no
external consumers pinned to its adapter internals):

- **Replace** the single skipped placeholder test in
  `test_experiment_durability.py` with the real scenarios — no dual-keep.
- **Rewrite** the reference adapter's `:memory:` store URL to file-backed
  (§3 D.2); delete the "conformance does not test durability" docstring line that
  this chunk falsifies.
- **Trim** the lifecycle row's unbacked restart clause (§5 edit 4) rather than
  leaving a duplicate claim.
- **No** dual-mode `:memory:`/file adapter fork unless the §3 D.2 perf gate
  forces the subclass alternative — and if it does, that is a deliberate split,
  documented, not a compat shim.

Nothing in the reference impl or wire surface is retired; this is additive at the
suite layer plus a small spec-consistency cleanup.

## 9. Scenarios (the 4–6 the issue asks for)

All live in
[`conformance/scenarios/test_experiment_durability.py`](../../conformance/scenarios/test_experiment_durability.py),
`CONFORMANCE_GROUP = "Experiment durability"`, each docstring's first line citing
`spec/v0/01-concepts.md §13`. Each follows *seed → snapshot → crash_restart →
re-snapshot → assert*. The fixture skips the whole module if the adapter is not
`RestartableIutAdapter`.

1. **Claim durability + post-restart authorization** *(headline)*. Seed a
   `claimed` (not submitted) task. Crash-restart. Assert: task still `claimed`,
   same claimant, claim record (incl. `expires_at` if set) intact; a fresh
   `submit` by the same claimant (via the carried bearer) succeeds. Wire-level
   `test_claim_token_survives_reopen_and_authorizes_submit`.

2. **Event-log durability + append continuity.** Seed several events (e.g. a few
   `create_task` calls). Snapshot `/events` from cursor 0 (types + order +
   payloads + ids). Crash-restart. Re-read from cursor 0 → identical prefix; then
   create one more task and assert the new event appends *after* the preserved
   prefix (counter resumed). Wire-level `test_event_order_total_and_preserved` +
   `test_event_id_counter_resumes_across_reopen`.

3. **Worker/group registry durability + credential survival.** Register an extra
   worker and group beyond the default set. Crash-restart. Assert `/workers`,
   `/groups` intact; and the pre-restart-issued worker bearer still authenticates a claim
   post-restart (persisted `credential_hash`). Wire-level worker-persistence +
   credential-hash survival.

4. **Idea / variant durability.** Seed an idea (→ `ready`) and a `starting`
   variant carrying a `commit_sha`. Crash-restart. Assert idea state / slug /
   `parent_commits` / `artifacts_uri` and variant status / `commit_sha` /
   `idea_id` survive. Wire-level `test_tasks_ideas_variants_and_events_survive`.
   **This is the scenario that leans on direct `POST /ideas` / `POST /variants`
   seeding (§2 decision 5 / §3 D.7) — the v1-vs-v1+roles boundary call; surfaced
   for codex.**

5. **Experiment-state + dispatch_mode durability.** PATCH a non-default
   `dispatch_mode`; terminate the experiment. Crash-restart. Assert
   `/dispatch_mode` persists; experiment `/state` still `terminated`; a
   `create_task` against the terminated experiment is rejected with
   `409 eden://error/illegal-transition`. **This scenario discharges the
   lifecycle row's trimmed "State survives store restart" clause** (§5 edit 4).

6. **Full representative-state snapshot/replay** *(capstone)*. Drive a
   representative mix (workers, groups, tasks in mixed states incl. one claimed,
   an idea + variant, events, a dispatch_mode patch). `capture_wire_state(pre)`,
   crash-restart, `capture_wire_state(post)`, assert deep equality (verbatim — no
   normalization, §3 D.6). This is the aggregate §13 assertion; scenarios 1–5 are
   the focused per-class projections that localize a failure.

(6 scenarios; the issue's "4–6". If codex rules scenario 4 must be v1+roles, the
group still ships 5 strict-v1 scenarios and scenario 4 carries the role-level
tag — coverage of the §5 row is preserved either way.)

## 10. Files to touch

**Harness (Python):**

- [`conformance/src/conformance/harness/adapter.py`](../../conformance/src/conformance/harness/adapter.py)
  — add the `RestartableIutAdapter` Protocol (§3 D.1).
- [`conformance/src/conformance/adapters/reference/adapter.py`](../../conformance/src/conformance/adapters/reference/adapter.py)
  — file-backed store + temp-dir lifecycle; `crash_restart()` (SIGKILL +
  re-spawn + fresh handle); reuse `_read_port_announcement` /
  `_start_stderr_drain`; update docstring (§3 D.2–D.4).
- [`conformance/src/conformance/harness/_durability.py`](../../conformance/src/conformance/harness/_durability.py)
  *(new — or add to `_seed.py`)* — `capture_wire_state()` (§3 D.5).
- [`conformance/scenarios/conftest.py`](../../conformance/scenarios/conftest.py)
  — `crash_restart` / `durable_clients` fixture: skip-if-unsupported,
  bearer carry-over via `copy_worker_bearers_from` (§3 D.4).
- [`conformance/scenarios/test_experiment_durability.py`](../../conformance/scenarios/test_experiment_durability.py)
  — replace the placeholder with scenarios 1–6 (§9).

**Spec:**

- [`spec/v0/09-conformance.md`](../../spec/v0/09-conformance.md) — §5 durability
  row rewrite; §3 vocabulary-list fix; §6 informative restart-capability note;
  §5 lifecycle-row trim (§5 edits 1–4).

**Docs:**

- `CHANGELOG.md` `[Unreleased]` + `docs/roadmap.md` pointer at completion (the
  chunk has a plan under `docs/plans/`, so the roadmap entry points at the
  **plan path**, not a PR link — AGENTS.md).
- [`docs/glossary.md`](../../docs/glossary.md) — only if codex judges a
  harness-vocabulary line is warranted (§6).

**Validation-only (read, expect no edit unless a regression surfaces):**

- [`conformance/scenarios/_meta/test_self_validation.py`](../../conformance/scenarios/_meta/test_self_validation.py)
  — confirm the misbehaving adapter (no `crash_restart`) skips the durability
  group cleanly (§7).
- [`reference/packages/eden-storage/tests/test_restart_safety.py`](../../reference/packages/eden-storage/tests/test_restart_safety.py)
  — the unit-level model; not edited, referenced.

## 11. Risks / things to watch

- **`:memory:` → file-backed perturbs the whole suite (§3 D.2).** The
  recommended always-file-backed change touches every scenario's store. Gate:
  measure `pytest -q conformance/ -n auto` wall-clock before/after; material
  regression → fall back to the durable-only subclass. Load-bearing decision —
  surfaced for operator.
- **Port-change + bearer carry-over is the easy-to-miss bug (§3 D.4).** Forget
  `copy_worker_bearers_from` and every `as_worker` call post-restart 401s with a
  misleading "not-a-real-token" bearer. The fixture owns this; the headline
  scenario (1) is the canary.
- **SIGKILL vs graceful (§3 D.3).** If the re-spawn path accidentally routes
  through the graceful `stop()` first, the scenario tests the wrong thing.
  `crash_restart` must `proc.kill()` directly.
- **Durability ≠ checkpoint (§3 D.6).** Copy-pasting checkpoint helpers drags in
  claimed→pending normalization + credential reissue, silently weakening the
  durability assertion. The scenario docstrings must state "verbatim, no
  normalization."
- **§3 vocabulary inconsistency is silent (§5 edit 2).** `check_citations`
  passes with a 01 §13 citation regardless of §3's prose, so a missed §3 fix
  would not fail CI — only a human reading §3 would catch the drift. Do not skip
  edit 2 on the grounds that "the gate is green."
- **§6 boundary stop-condition (§5 edit 3).** If review reads the restart-capability
  note as a §6 IUT-contract boundary change, **stop and surface to the operator**
  before finalizing (task stop condition).
- **v1 vs v1+roles for scenario 4 (§3 D.7 / §9).** Idea/variant seeding may read
  as role-level to codex; the fallback (role-tag scenario 4) preserves §5
  coverage either way, but the level claim must be internally consistent —
  don't ship a "v1" group whose seeding silently needs roles.
- **xdist interaction.** Each durability scenario spawns its own restartable IUT
  with its own temp SQLite file; under `-n auto` the files must be per-worker /
  per-scenario unique (the adapter owns `mkdtemp`, so they are). Confirm no
  cross-scenario file reuse and that temp dirs are cleaned in `stop()`.
- **`subprocess-adapter lifecycle` pitfalls re-apply to the re-spawn**
  (AGENTS.md): `token_hex` reuse, daemon-thread port read, partial-startup
  cleanup on the re-spawn. The re-spawn is a second `start`-shaped path; it
  inherits every trap the first one has.

## 12. Chunked execution plan + validation gates

Single impl PR (medium; the issue estimates ~1–2 weeks but the unit-level model
already exists, so the real work is the adapter capability + 6 scenarios +
4 spec edits). Internal waves:

**Wave 1 — adapter capability + fixture.**

- `RestartableIutAdapter` Protocol; reference-adapter file-backed store +
  `crash_restart()`; `crash_restart`/`durable_clients` fixture with bearer
  carry-over.
- `capture_wire_state()` helper.

*Gate:* `uv run ruff check . && uv run pyright && python3 scripts/check-complexity.py`;
a throwaway smoke scenario proving one crash-restart round-trips a single task.

**Wave 2 — scenarios.**

- Scenarios 1–6 (§9); remove the placeholder skip.

*Gate:* `uv run pytest -q conformance/ -n auto` (durability group green; full
suite green — confirms the file-backed switch didn't regress other scenarios) +
`uv run python conformance/src/conformance/tools/check_citations.py` (durability
group's three citation legs pass) + the §3 D.2 wall-clock measurement.

**Wave 3 — spec + docs.**

- §5 durability-row rewrite; §3 vocabulary fix; §6 restart-capability note; §5
  lifecycle-row trim; glossary line if warranted.

*Gate:* `python3 scripts/spec-xref-check.py`, `markdownlint-cli2`,
`python3 scripts/check-rename-discipline.py`.

**Wave 4 — full validation.**

Run the literal CLAUDE.md "Commands" gate (not a narrowed subset):

```text
uv sync
uv run ruff check .
uv run pyright
uv run pytest -q
uv run pytest -q conformance/ -n auto
uv run python conformance/src/conformance/tools/check_citations.py
npx --yes markdownlint-cli2@0.14.0 "**/*.md" "#node_modules" "#.venv" "#docs/archive/**" "#docs/plans/review/**"
pipx run 'check-jsonschema==0.29.4' --check-metaschema spec/v0/schemas/*.schema.json
python3 scripts/spec-xref-check.py
python3 scripts/check-rename-discipline.py
```

The Compose smokes are **not** load-bearing for this chunk (it touches only the
conformance suite + spec prose, no service/Compose surface), but run
`bash reference/compose/healthcheck/smoke.sh` once as a no-regression check since
the AGENTS.md guidance treats the smokes as the canonical pre-push gate for any
substantive change.

- CHANGELOG `[Unreleased]` entry + roadmap one-liner at the plan path.
- Commit the impl-stage codex-review record under
  `docs/plans/review/issue-156/impl/<timestamp>/`.

## 13. Estimated effort

| Activity | Estimate |
|---|---|
| Adapter capability (Protocol + file-backed + `crash_restart`) + fixture + bearer carry-over | ~1 day |
| `capture_wire_state` helper | ~0.5 day |
| 6 scenarios | ~1.5 days |
| Spec edits (4) + glossary | ~0.5 day |
| Perf gate + full validation + codex iterations (plan + impl) | ~1.5 days |
| **Total** | **~5 days** |

Lands at the low end of the issue's "Medium, ~1–2 weeks" — the existing
unit-level model (`test_restart_safety.py`) and the existing bearer-carry-over
primitive (`copy_worker_bearers_from`) remove most of the discovery risk.
</content>
</invoke>
