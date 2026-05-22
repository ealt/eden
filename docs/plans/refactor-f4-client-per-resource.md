# Refactor F-4 — `eden-wire/client.py` per-resource client split

**Status.** Codex-converged (round 4, "Converged: ship"). Plan-stage; awaiting operator approval before impl spawn.

**Round-4 review incorporated** (1 Minor):

- Three "5 files" references rewritten as "4 current caller files
  plus the new `_dispatch_store.py` bridge added by F-4" (round 4
  caught that the 5th dispatch file doesn't exist yet — F-4 adds
  it).

**Round-3 review incorporated** (2 Should-fix + 1 Minor):

- §7.1 stale "22" counts replaced with "21 methods + 1 property"
  in the option analysis subsection (the recount landed in the
  preamble in round 2 but the option-comparison body still carried
  the round-1 figure).
- §5.4.3 `test_client.py` (eden-control-plane) explicitly verified
  and marked REMOVED (round 2 left it as a maybe-zero placeholder;
  the file tests `ControlPlaneClient` against `MockTransport`, no
  StoreClient instantiation).
- §5.4.1 wire-tests total prose: `test_schema_parity.py` →
  `test_wire_schema_parity.py` (the round-2 fix landed in the row
  but not in the trailing prose).

**Round-2 review incorporated** (4 Should-fix + 1 Minor):

- §5.3 dispatch row rewritten to match §7.1 option-B reversal —
  consumer-side adapter via `eden_dispatch/_dispatch_store.py`,
  not unions + isinstance branching. §5.3 preamble also clarified
  to call out consumer-side adapters (in the consumer package,
  not on `StoreClient`) as the explicit (b) disposition for
  library packages.
- §7.1 dispatch needs-list recounted from actual grep: **21
  unique methods + 1 property**, not 22. Speculative
  `read_dispatch_mode` / `read_task` removed (those names
  appear nowhere in `eden_dispatch/`).
- §5.4.2 false positives removed: `control-plane/tests/test_server.py`
  is the control-plane SERVER test (TestClient), and
  `task-store-server/tests/test_artifacts_cli.py` is a CLI test
  (subprocess + curl-style). Neither instantiates `StoreClient`.
- §4 intro rewritten ("51 StoreClient methods map onto 43 server
  routes plus 8 client-only extras") — round-1's fix only landed
  in the §4 footer, not the intro.
- §5.5 verification-command note: single-line `rg` doesn't match
  the multiline `from eden_wire import (…)` blocks in
  test_lifecycle_wire.py and test_reassign_dispatch_wire.py;
  documented the multiline-capable command (`rg --multiline -U`)
  and noted the round-1 sites were verified manually.
- Minor: round-1's struck-through entry named `test_schema_parity.py`;
  actual filename is `test_wire_schema_parity.py`. Corrected.

**Round-1 review incorporated** (5 Should-fix + 1 Minor):

- §7.1 dispatch handling reversed — round 2 recommends option B
  (thicker `_StoreClientBridge` in `eden_dispatch/_dispatch_store.py`)
  for symmetry with the integrator bridge. The 22-method needs-list
  is too wide for option-A package-level isinstance branching to
  be ergonomic; the bridge concentrates the wiring in one file and
  honors the surface-lock posture (the lock is about StoreClient's
  public surface, not consumer-side adapters). Operator can override
  via §10.
- Wave-3 grep gate identifier-whitelist removed (round-1 noted it
  missed real sites like `seed.foo(...)` / `verify.bar(...)`). New
  gate greps for the method-suffix pattern across `reference` +
  `conformance` regardless of receiver name; triage discipline added.
- §4 intro recount: "51 StoreClient methods → 43 server routes + 8
  client-only extras" (round 1 noted §4 still opened with the stale
  "43 methods mapped" claim).
- §5.4.1 caller list cleanup: `test_artifact_route.py` removed (uses
  `TestClient(make_app(...))`, not StoreClient); `test_schema_parity.py`
  removed (parity checker, no StoreClient).
- §5.5 import-site list rewritten with the 4 verified import lines
  (2 direct from `eden_wire.client`, 2 via top-level `eden_wire`);
  speculative external service/package rows removed.
- §5.2 + Decision 6: added `reference/packages/eden-wire/README.md`
  to touched-files (its line-3 claim that "StoreClient satisfies the
  same Store Protocol from the opposite side" becomes false under F-4).
- §5.6 stale `test_store_protocol.py` conditional removed; the §6.2
  test name reference updated to the renamed
  `test_storeclient_drops_flat_method_surface`.
- Stale `read_task` references in the integrator-bridge §7.1 preamble
  and §5.4.3 swept.

**Round-0 review incorporated** (see [`docs/plans/review/refactor-f4-client-per-resource/plan/<timestamp>/0-review.md`](review/refactor-f4-client-per-resource/plan/)):
codex flagged 1 Must-fix + 5 Should-fix + 1 Minor. Applied changes:

- §7.1 integrator bridge sketch was wrong (`read_task` is not
  called by the integrator) — now derived from grep against
  [`integrator.py`](../../reference/packages/eden-git/src/eden_git/integrator.py)
  and lists the actual 4-method set
  (`read_variant`, `read_idea`, `integrate_variant`,
  `validate_evaluation`). eden-dispatch was treated as a single
  shared Protocol; now split out as its own decision (22-method
  surface; recommendation: option A isinstance branching, not
  a thick bridge).
- §6.2 the Protocol-break regression test was specified with
  `isinstance(sc, Store)` which would raise `TypeError` (Store
  isn't `runtime_checkable`). Renamed to
  `test_storeclient_drops_flat_method_surface` and replaced with
  `hasattr(sc, "claim")`-style absent-attribute assertions.
- §§1.6/6.3/8 pyright-as-backstop framing: pyright catches missed
  migrations at Wave 4 (when flat methods are removed), NOT at
  Wave 3 (when flat methods still exist as dead code). Added an
  explicit Wave-3 grep-based audit gate.
- §1.1 method count corrected (51 not 43); duplicate `reassign_task`
  row removed from §4 table; §4.1 summary recounted (43 routes ↔
  51 client methods with 8 documented extras).
- §§5.3-5.4 false positives removed
  (`eden-control-plane/tests/test_store_protocol.py` is
  ControlPlaneStore conformance, not StoreClient;
  `eden-checkpoint/tests/` has no StoreClient sites).
- Decision 6 / §5.2 / §5.5: clarified actual import-site surface
  for `Indeterminate*` (2 verified eden-wire test files, not the
  speculative external services); flagged
  [`eden_wire/__init__.py`](../../reference/packages/eden-wire/src/eden_wire/__init__.py)
  as a touched-file (re-exports 4 of 5 Indeterminate classes;
  module docstring still claims Store-Protocol satisfaction).
- §7.7 monkeypatch claim corrected — tests use injected
  `httpx.Client(transport=MockTransport(...))`, not `_request`
  monkeypatches; transport-injection pattern is invariant under
  F-4.

**Tracks issue.** [ealt/eden#116](https://github.com/ealt/eden/issues/116).

**Predecessors.** PR #105 (Code-quality audit Phase A + C) landed
the Tier-1 complexity gate
([`scripts/check-complexity.py`](../../scripts/check-complexity.py))
and the per-file `# slop-allow-file:` annotation at the top of
[`reference/packages/eden-wire/src/eden_wire/client.py`](../../reference/packages/eden-wire/src/eden_wire/client.py)
that defers F-4 to this issue. The file is 1385 lines today (MI
~21.5, just above the gate floor). The audit's §F-4 entry proposes
the per-resource split; this is the chunk that delivers it.

**Sibling chunk.** [ealt/eden#115](https://github.com/ealt/eden/issues/115)
(F-3) is the symmetric per-resource regroup of
[`eden-wire/server.py`](../../reference/packages/eden-wire/src/eden_wire/server.py).
The two share the `eden-wire` package but touch disjoint files; no
direct file conflict. By design, the **resource boundaries on the
client side mirror F-3's router boundaries 1-to-1** (§4) so that
operators reading client and server code together see the same
shape. Either chunk can land first; if F-3 lands first, F-4 cites
F-3's already-merged router shape as the reference.

**Naming.** Pre-draft check against [`docs/glossary.md`](../glossary.md)
and AGENTS.md "Naming discipline":

- The refactor introduces eleven new internal class names
  (`TasksClient`, `IdeasClient`, `VariantsClient`,
  `DispatchModeClient`, `ExperimentLifecycleClient`,
  `ExperimentReadClient`, `EventsClient`, `WorkersClient`,
  `GroupsClient`, `CheckpointsClient`, `ReferenceClient`). All use
  the existing resource nouns from the glossary (tasks, ideas,
  variants, events, workers, groups) plus the wire-binding
  vocabulary already on the server side (`dispatch_mode`,
  `experiment_lifecycle`, `experiment_read`, `checkpoints`,
  `reference`). No new EDEN-domain identifiers, no role-verb
  collisions. The plural "Tasks"/"Ideas"/"Variants" form mirrors
  F-3's `routers/tasks.py` / `routers/ideas.py` /
  `routers/variants.py` directory names; an "EventsClient" pairs
  with `routers/events.py`, etc.
- The `ClientTransport` dataclass introduced in §3.2 is a
  wire-binding internal carrier (not exported through
  `eden_wire.__init__`); the name does not collide with any spec
  or domain identifier. Specifically chosen NOT to be
  "ClientDeps" (the noun "deps" is generic; the carrier holds
  the shared HTTP session + headers + URL bases, which is exactly
  a transport).
- No method names on the sub-clients change vs the existing
  monolithic surface — only the accessor path. `claim` is still
  `claim`, just under `.tasks` (`client.tasks.claim(...)` vs
  `client.claim(...)`).
- No route paths, request/response body fields, error types, or
  status codes change — F-4 is structural-internal AND
  accessor-shape-changing on the public client surface, but
  HTTP-wire-invariant.

## 1. Context

### 1.1 What F-4 is

[`reference/packages/eden-wire/src/eden_wire/client.py`](../../reference/packages/eden-wire/src/eden_wire/client.py)
is the HTTP client that talks to the chapter-7 wire binding. At
PR #105 the file carries:

- **1385 lines total** (MI ≈ 21.5, just above the file-MI gate
  floor of 20 — one more passive accretion would trip the file-MI
  threshold).
- **One class `StoreClient`** holding **51 public instance methods**
  (verified: `grep -cE '^    def [a-z]' client.py` returns 53; subtract
  the two lifecycle entries `close` and the `experiment_id` property
  to get 51 wire-facing methods), plus 4 read-back helpers
  (`_try_read_variant`, `_try_read_task`, `_try_read_dispatch_mode`,
  `_try_read_experiment_state`) and the private import-recovery probe
  (`_import_recovery_probe` + `_parse_recovery_manifest` +
  `_fetch_recovery_probe`), plus 4 module-level utilities
  (`_task_targets_equal`, `_submission_to_wire`,
  `_submission_from_wire`, `_now`, `_as_wire_datetime`).
- **One `# slop-allow-file:` annotation** at line 1 marking the
  deferral to this issue.
- **Function-LEN violator L-M** (`_import_recovery_probe`, was
  141 LOC at PR #105; refactored under threshold in #105's Phase
  C wave to ~80 LOC via `_parse_recovery_manifest` +
  `_fetch_recovery_probe` extraction — verify before impl).

The audit disposition
([`docs/audits/2026-05-20-phase-c-disposition.md`](../audits/2026-05-20-phase-c-disposition.md)
§F-4) sketches one shape — "keep `StoreClient` as the public facade
but compose it from per-resource clients in `_client/`" — with
"public surface stays identical" as the risk-floor. **This plan
ships a different shape per operator direction** (§1.5): the
composed sub-clients are the public surface; the flat methods on
`StoreClient` are removed entirely; every existing
`store_client.<method>(...)` call site migrates to
`store_client.<resource>.<method>(...)` in the same PR. The
audit's risk note ("public surface stays identical") is therefore
explicitly waived — see §1.5 / §1.6 for the consequences.

### 1.2 What the audit measured

| ID | File:fn | Metric | Disposition |
|---|---|---:|---|
| F-4 | `eden-wire/client.py` (file) | 1385 SLOC, MI 21.54 | This chunk — per-resource split |
| L-M | `eden-wire/client.py:_import_recovery_probe` | 141 LOC, CC 19 | Already addressed in PR #105's Phase C wave (extracted helpers). The remaining body is under threshold; F-4 moves it into `CheckpointsClient` unchanged. |

The `# slop-allow-file:` annotation at the top of client.py is
the only F-4-deferred annotation in the file. Wave 4's polish
removes it.

### 1.3 Why now

Mirrors F-3 §1.3: the audit's slop-allow annotations are explicit
IOUs against issues #114 / #115 / #116. Delaying further risks:

- **Annotation drift.** Each new wire method added to `StoreClient`
  (e.g., a future worker-affinity endpoint added in parallel with a
  server-side route) makes the eventual split harder because the new
  method's read-back ladder has to be re-derived in the new home.
- **Slop-prevention discipline erosion.** AGENTS.md §Slop
  prevention is explicit: a long-lived `# slop-allow:` with a
  deferred resolution becomes a license to grow the violator
  further. Resolving F-4 reinforces that the audit's IOUs are
  honored.
- **Naming-symmetry with F-3.** F-3 (server.py) is plan-stage in
  parallel ([`docs/plans/refactor-f3-server-router-regroup.md`](refactor-f3-server-router-regroup.md)).
  Landing F-4 with the same resource boundaries gives operators
  cross-file navigation by analogy ("the variant route is in
  `routers/variants.py`; the client method is in
  `_client/variants.py`").

### 1.4 Behavior preservation contract

F-4 changes the **client API shape** (flat → composed accessor
path) but is otherwise **purely structural**. The chunk MUST NOT
change:

- Any HTTP route path, method, query-parameter shape, header
  contract, request/response body shape, or status code that
  the client emits.
- Any read-back ladder (`integrate_variant`, `reassign_task`,
  `update_dispatch_mode`, `terminate_experiment`,
  `import_checkpoint`) — each ladder's three outcomes
  (confirmed success / confirmed divergence / indeterminate)
  must be preserved byte-for-byte. These are normative per
  [`spec/v0/07-wire-protocol.md`](../../spec/v0/07-wire-protocol.md)
  §5, §2.7, §2.8, §2.9, §14.
- Any exception class (`IndeterminateImport`,
  `IndeterminateIntegration`, `IndeterminateReassign`,
  `IndeterminateDispatchModeUpdate`, `IndeterminateTermination`)
  — names, public-API export status, and `raise … from original`
  chains all preserved. They stay exported from `eden_wire.client`
  for backward-compatibility of imports (the import path doesn't
  change even though the methods move).
- The `StoreClient(...)` constructor signature: `(base_url,
  experiment_id, *, bearer=None, token=None, client=None,
  timeout=30.0, read_back_attempts=3)`. The lifecycle methods
  (`close`, `__enter__`, `__exit__`, `experiment_id` property)
  stay on `StoreClient`.
- The bearer-vs-worker-id preflight semantics (`StoreClient._assert_bearer_matches_worker_id`)
  — moves to the shared transport per §3.3 but the check itself
  is invariant.
- Conformance: [`uv run pytest -q conformance/`](../../conformance/)
  passes pre- and post-refactor. The reference adapter at
  [`conformance/src/conformance/adapters/reference/adapter.py`](../../conformance/src/conformance/adapters/reference/adapter.py)
  constructs a StoreClient (verify and update if it touches
  removed-flat methods); the call shape moving to composed form
  is the same kind of mechanical migration as any other caller.

The contract is "same HTTP traffic, same exception shapes, same
read-back semantics; different accessor path on the client
object." A passing conformance suite + a passing
`eden-wire/tests/` suite is the necessary acceptance gate for the
wire surface.

### 1.5 Surface decision — composed-public (operator-locked)

**Decision.** The public surface of `StoreClient` after F-4 is
**composed-only**: callers access methods through per-resource
attributes (`client.tasks.claim(...)`, `client.variants.create(...)`,
`client.checkpoints.export(...)`, etc.). The flat-surface methods
(`client.claim(...)`, `client.create_variant(...)`, …) are
**removed**. There is no delegation shim, no facade preservation,
no transitional period.

**Per the operator's lock in the F-4 spawn prompt** (paraphrased):
"This aligns with CLAUDE.md's 'no backwards-compat shims in
greenfield' — clean break is the project posture. StoreClient
itself becomes ~30 lines holding sub-client instances + shared
transport."

**This departs from the audit disposition** (which described
"keep StoreClient as the public facade … delegates to each
section's methods"). The disposition is amended by this plan
(documented here so the deviation is auditable). Issue #116's
acceptance criterion "All existing callers work unchanged
(StoreClient surface preserved)" is also amended — see §1.7.

### 1.6 The Store-Protocol-conformance consequence

`StoreClient` today **structurally implements** the
[`Store` Protocol](../../reference/packages/eden-storage/src/eden_storage/protocol.py)
(chapter-8 §1; `class Store(Protocol)`, not runtime-checkable —
structural-typing-only). The module docstring at
[`client.py:3-9`](../../reference/packages/eden-wire/src/eden_wire/client.py)
states this explicitly:

> "``StoreClient`` makes the EDEN wire binding look exactly like
> a direct ``Store`` to callers. The dispatch driver, integrator,
> and conformance scenarios all work against it unchanged:
> structural Protocol conformance means 'talks to a store'
> doesn't commit to a transport."

**After F-4 (composed-public shape), `StoreClient` no longer
satisfies the `Store` Protocol.** The flat methods Store requires
(`claim`, `submit`, `create_variant`, `integrate_variant`, …)
move to sub-client attribute paths; pyright's structural-typing
check will flag every site that assigns a `StoreClient` instance
into a `: Store`-typed parameter slot.

This is the **load-bearing reason the plan can rely on type
checking as a backstop for missed migrations** (the operator's
prompt §9: "pyright will surface every still-flat-surface
reference"). **Important timing caveat (round-0 review):** pyright
only catches missed migrations once the flat methods are actually
removed from `StoreClient` — Wave 4 in §8. During Wave 3 the flat
methods still exist (intentionally — see §8's coexistence rule),
so a missed `store_client.claim(...)` site type-checks green
through Wave 3 and only fails at Wave 4. The Wave 3 completeness
gate is therefore a **grep-based audit**, not pyright; see §8 for
the specific gate command. Two failure modes pyright catches at
Wave 4:

1. **Direct flat-method calls.** `store_client.claim(task_id, …)`
   typed as `StoreClient` → "Cannot access member `claim` for
   type `StoreClient`."
2. **Store-typed slot assignments.** `def foo(store: Store):` →
   `foo(store_client)` → "Argument of type `StoreClient` cannot
   be assigned to parameter of type `Store`" (because the flat
   methods Store advertises are no longer on StoreClient).

Both must be resolved in the F-4 PR before merge. The resolution
matrix is §5.4.

### 1.7 Why not preserve flat methods via delegation

The operator-locked decision rules out a delegation shim. For the
record, the alternatives considered and rejected:

- **Alternative A (audit disposition's shape).** Sub-clients are
  internal (`_client/tasks.py`), StoreClient keeps flat methods
  that delegate one-line each to the sub-clients. Public surface
  unchanged; no caller migration needed; ~0.5 day per the audit.
  **Rejected by operator** as a backwards-compat shim that the
  greenfield posture explicitly disallows. Also: the operator's
  intent is for the composed surface to be the canonical client
  shape going forward, not an implementation detail; delegation
  would obscure that.
- **Alternative B (bundle with Store-protocol migration).**
  Migrate `Store` Protocol itself to a composed shape (`Store.tasks.claim`,
  …), then every `_StoreBase` mixin in F-1 (#114) mirrors the
  composed shape, then every Store caller migrates. Single
  consistent surface across in-process + wire. **Out of scope
  for F-4**: this would collapse F-1 / F-3 / F-4 into one
  multi-day chunk and re-architect the storage interface. Tracked
  in #114 separately; F-4 does not depend on it. If #114
  eventually adopts a composed Store shape, F-4's composed
  StoreClient already matches.
- **Alternative C (composed StoreClient only, no Store change).**
  This is the chosen shape. Store stays flat; StoreClient becomes
  composed-only; the structural-conformance link between them
  breaks. Pyright is the migration backstop (§1.6). Caller sites
  that wanted to pass StoreClient through a `: Store`-typed
  parameter must either retype to `StoreClient` directly or be
  refactored to construct an adapter (rare; in practice, retyping
  is the right answer — wire-bound services usually want to be
  explicit about the wire transport).

### 1.8 Risks the chosen shape introduces

(Beyond the per-decision risks called out in §§2–8.)

1. **`Store`-typed call sites that flow a `StoreClient` at runtime
   need retype-or-refactor.** §5.4 enumerates them
   exhaustively from grep. The fix in each case is one of:
   - Retype the parameter from `: Store` to `: StoreClient` (when
     the call site is wire-specific by construction —
     e.g. `_common/auth.py`'s `_resolve_bearer` constructs a
     transient StoreClient to call `whoami`).
   - Switch the in-process Store implementation (rare today; would
     only matter for tests that fake out the wire binding).
   - Keep `: Store` and adapt the StoreClient via a small
     `_FlatStoreView(store_client)` adapter (NOT recommended;
     this re-creates the delegation shim the operator rejected
     and would proliferate). **Default is retype.**
2. **The conformance reference adapter
   ([`conformance/src/conformance/adapters/reference/adapter.py`](../../conformance/src/conformance/adapters/reference/adapter.py))
   constructs StoreClient.** Update its caller sites to the
   composed form. The adapter is a wire-IUT shim; its consumers
   in [`conformance/scenarios/`](../../conformance/scenarios/) use
   a typed `IutAdapter` Protocol (not `Store`), so the change is
   bounded to the adapter file itself.
3. **`store_client(...)` context-manager factory at line 1377 of
   the current file stays as-is** (same signature, same
   semantics; just constructs the new-shape `StoreClient`). The
   `with store_client(base_url, exp_id) as sc:` pattern in tests
   still works; only the methods called on `sc` change.
4. **Public-import preservation for the exception classes.** The
   `IndeterminateImport`, `IndeterminateIntegration`,
   `IndeterminateReassign`, `IndeterminateDispatchModeUpdate`,
   `IndeterminateTermination` classes stay exported from
   `eden_wire.client` so existing imports (e.g. `from eden_wire.client
   import IndeterminateIntegration` in
   [`reference/packages/eden-git/src/eden_git/integrator.py`](../../reference/packages/eden-git/src/eden_git/integrator.py))
   keep working. Each class's body moves into the per-resource
   client module that raises it; `client.py` re-exports them in
   its `__all__` for back-compat. This is a re-export (an import
   alias), NOT a delegation shim — the classes themselves live
   in one place each.
5. **`from eden_wire.client import StoreClient` import path
   stays valid** — the class still lives in `client.py`, just with
   a different body. No caller's import line changes.

## 2. Decisions

### Decision 1 — Per-resource client modules under `_client/`

**Decision.** Introduce
`reference/packages/eden-wire/src/eden_wire/_client/` as the
package home for per-resource sub-client modules. Each module
defines one class `<Resource>sClient` (plural; matches F-3's
router-module nouns). Each method on the class is one HTTP call
plus its read-back ladder (where applicable).

Top-level `StoreClient` in `client.py` becomes a thin assembler:
construct a shared `ClientTransport`, instantiate one of each
sub-client over it, expose them as public attributes. Public
attribute names are lowercase singular-or-plural matching the
F-3 router accessor convention:

| Attribute | Class | Source module |
|---|---|---|
| `client.tasks` | `TasksClient` | `_client/tasks.py` |
| `client.ideas` | `IdeasClient` | `_client/ideas.py` |
| `client.variants` | `VariantsClient` | `_client/variants.py` |
| `client.dispatch_mode` | `DispatchModeClient` | `_client/dispatch_mode.py` |
| `client.experiment_lifecycle` | `ExperimentLifecycleClient` | `_client/experiment_lifecycle.py` |
| `client.experiment_read` | `ExperimentReadClient` | `_client/experiment_read.py` |
| `client.events` | `EventsClient` | `_client/events.py` |
| `client.workers` | `WorkersClient` | `_client/workers.py` |
| `client.groups` | `GroupsClient` | `_client/groups.py` |
| `client.checkpoints` | `CheckpointsClient` | `_client/checkpoints.py` |
| `client.reference` | `ReferenceClient` | `_client/reference.py` |

**Eleven sub-clients exactly match F-3's eleven routers** (tasks,
ideas, variants, dispatch_mode, experiment_lifecycle,
experiment_read, events, workers, groups, checkpoints, reference).
The 1-to-1 mapping is intentional and verified per route in §4.

**Why over alternatives.**

- **Alternative A: per-method modules.** Rejected — 43 files of
  ~30 LOC each is fragmentation. Audit's predicted post-refactor
  shape is "each section 100-200 LOC"; per-resource grouping
  matches.
- **Alternative B: keep one file with internal section
  organization (comment banners only).** Rejected — does not
  drop the file-SLOC violation. Audit gates are file-level; the
  internal-only reorganization keeps the slop-allow.
- **Alternative C: single `Resources` namespace object** (one
  class with all 11 sub-clients as nested classes). Rejected —
  adds an indirection layer (`client.resources.tasks.claim`)
  without organizational benefit, and the operator's example
  `client.tasks.claim` rules it out anyway.

### Decision 2 — Shared transport in a `ClientTransport` dataclass

**Decision.** All per-resource sub-clients share a single
`ClientTransport` instance carrying:

```python
@dataclass(frozen=False, slots=True)
class ClientTransport:
    """Shared HTTP plumbing for all per-resource sub-clients.

    One instance per StoreClient; all sub-clients hold a reference
    to it and call its ``request(...)`` method for every HTTP
    round trip. Owns the underlying ``httpx.Client`` (or borrows
    one injected by the caller) so a single connection pool
    serves every resource's traffic.
    """

    base_url: str            # rstripped of trailing slash
    base: str                # f"{base_url}/v0/experiments/{exp_id}"
    ref_base: str            # f"{base_url}/_reference/experiments/{exp_id}"
    experiment_id: str
    headers: dict[str, str]  # X-Eden-Experiment-Id + optional Authorization
    bearer: str | None       # for the _assert_bearer_matches_worker_id check
    client: httpx.Client     # owned or injected
    owns_client: bool        # close() iff True
    timeout: float
    read_back_attempts: int
```

`ClientTransport` exposes three methods:

- `request(method, path, *, params=None, json=None, extra_headers=None) -> httpx.Response`
  — the current `_request` method body verbatim, including the
  error-envelope-aware 4xx/5xx branch.
- `assert_bearer_matches_worker_id(worker_id: str) -> None` —
  the current `_assert_bearer_matches_worker_id` body verbatim.
- `maybe_json(resp: httpx.Response) -> Any` — the current
  static helper.

Frozen=False (slots=True for type-checked attribute discipline):
`headers` is dict-mutable in principle but the implementation
never mutates it post-construction; the dataclass tracks the
shape for type-checking, not enforcement.

**Why over alternatives.**

- **Alternative A: a `_RequestMixin` base class that every
  sub-client inherits.** Rejected — inheritance for code reuse
  is less explicit than a shared dependency; each sub-client's
  `__init__(self, transport: ClientTransport)` makes the
  dependency visible. Also: multiple inheritance MRO concerns
  if we ever add a second concern (auth, logging, retry); a
  composed transport sidesteps that entirely.
- **Alternative B: module-level globals updated per-instance.**
  Outright wrong — breaks the existing multi-client posture
  (one process can hold `StoreClient(url_a)` + `StoreClient(url_b)`
  simultaneously; this is exercised in
  [`test_wire_roundtrip.py::test_replay_from_zero`](../../reference/packages/eden-wire/tests/test_wire_roundtrip.py)
  and across all multi-experiment orchestrator tests).
- **Alternative C: pass `httpx.Client` to each sub-client
  individually + duplicate the URL bases + headers per sub-client.**
  Rejected — duplicates state across 11 sub-clients and risks
  drift (e.g. one sub-client's header dict diverging from
  another's after a hypothetical credential-rotation API
  amendment).

### Decision 3 — `_assert_bearer_matches_worker_id` migrates to transport

**Decision.** The current
`StoreClient._assert_bearer_matches_worker_id(worker_id)` is
shared by `claim` and `submit` (both in TasksClient post-split).
The check itself is a per-call preflight on the bearer; move it
to `ClientTransport.assert_bearer_matches_worker_id(worker_id)`
so the TasksClient calls `self._transport.assert_bearer_matches_worker_id(worker_id)`
from both methods. Body unchanged.

**Why.** The check reads `self._bearer` — which lives on the
transport in the new shape — so co-locating it with the
transport keeps the bearer field's accessors narrowly scoped.
The two call sites are both in TasksClient, so an alternative is
to keep it as a TasksClient helper; the transport is the better
home because (a) future sub-clients that mutate per-worker state
may need the same check, and (b) the implementation reads transport
state, not task state.

### Decision 4 — Module-level helpers stay at module-level

**Decision.** The four current module-level helpers
(`_task_targets_equal`, `_submission_to_wire`,
`_submission_from_wire`, `_now`, `_as_wire_datetime`) move per
sole-consumer:

- `_task_targets_equal` (used by `reassign_task` read-back) →
  `_client/tasks.py` as module-private.
- `_submission_to_wire` / `_submission_from_wire` (used by
  `submit` + `read_submission`) → `_client/tasks.py` (TasksClient
  is the only consumer; matches F-3's choice for the symmetric
  server-side helpers).
- `_now` (used by `create_*_task` factories + the
  `terminate_experiment` indeterminate synthetic-Experiment
  return + `_as_wire_datetime` defaulting) — used by tasks +
  experiment-lifecycle. Place at `_client/_common.py` as a
  shared helper.
- `_as_wire_datetime` (used by `claim`'s `expires_at` coercion)
  — TasksClient-only consumer. Move to `_client/tasks.py`.

**Why.** Co-location matches usage. `_client/_common.py` exists
for helpers shared across two or more sub-clients (`_now` is the
only one today; future helpers can be added there). The
two-consumer threshold mirrors F-3's approach in
`_dependencies.py`.

### Decision 5 — Sub-client instantiation order in `StoreClient.__init__`

**Decision.** `StoreClient.__init__` constructs the
`ClientTransport` once, then instantiates each sub-client over it
in **the order they appear in §4's mapping table** — which mirrors
the F-3 server include-order, which mirrors the current file's
method order. The order is:

```python
self.tasks = TasksClient(self._transport)
self.ideas = IdeasClient(self._transport)
self.variants = VariantsClient(self._transport)
self.dispatch_mode = DispatchModeClient(self._transport)
self.experiment_lifecycle = ExperimentLifecycleClient(self._transport)
self.events = EventsClient(self._transport)
self.workers = WorkersClient(self._transport)
self.groups = GroupsClient(self._transport)
self.experiment_read = ExperimentReadClient(self._transport)
self.checkpoints = CheckpointsClient(self._transport)
self.reference = ReferenceClient(self._transport)
```

**Why.** Order is stylistic (sub-client construction is
side-effect-free; each just stores a transport reference). Matching
the F-3 / current-file order makes diff review easier and gives
operators a consistent mental model across `client.py` and
`server.py`.

### Decision 6 — Exception classes are re-exported from `client.py`

**Decision.** The five `Indeterminate*` exception classes live
each in the sub-client module that raises them:

- `IndeterminateIntegration` → `_client/variants.py`
- `IndeterminateReassign` → `_client/tasks.py`
- `IndeterminateDispatchModeUpdate` → `_client/dispatch_mode.py`
- `IndeterminateTermination` → `_client/experiment_lifecycle.py`
- `IndeterminateImport` → `_client/checkpoints.py`

`client.py` re-imports and re-exports them in `__all__`:

```python
from ._client.variants import IndeterminateIntegration
from ._client.tasks import IndeterminateReassign
# ... etc.

__all__ = [
    "IndeterminateDispatchModeUpdate",
    "IndeterminateImport",
    "IndeterminateIntegration",
    "IndeterminateReassign",
    "IndeterminateTermination",
    "StoreClient",
]
```

**Why.** External callers import these via
`from eden_wire.client import Indeterminate…`. Round-0 review
narrowed the verified import-site list to two: only
[`reference/packages/eden-wire/tests/test_checkpoint_wire.py:31`](../../reference/packages/eden-wire/tests/test_checkpoint_wire.py)
and
[`reference/packages/eden-wire/tests/test_wire_roundtrip.py:36`](../../reference/packages/eden-wire/tests/test_wire_roundtrip.py)
import the `Indeterminate*` names directly from
`eden_wire.client`. (External services like `eden_git/integrator.py`
do not re-import these classes — they catch the exceptions by
walking the wire client's method-level docstrings, but the
import path itself is not load-bearing for them; verify via
`rg "from eden_wire.client import" reference conformance` during
impl.) Re-export still has near-zero cost and keeps the two
test imports valid; the cost-benefit ratio favors preservation.

Separately, [`eden_wire/__init__.py`](../../reference/packages/eden-wire/src/eden_wire/__init__.py)
re-exports **four** of the five exception classes at the
top-level surface (the fifth, `IndeterminateImport`, was added
in chunk 12b but never propagated up). That file's module
docstring also still says "`StoreClient`: httpx-backed client
that satisfies the `Store` Protocol" — which is invalidated by
the F-4 Protocol-break. **Wave 4 must update
`eden_wire/__init__.py`**:

- Update the module docstring to drop the "satisfies the `Store`
  Protocol" claim.
- Decide explicitly whether to add `IndeterminateImport` to the
  top-level surface (recommend: yes, for symmetry with the
  other four; flag in PR description so reviewers can object).
- Otherwise leave the imports/exports identical.

§5.2 below adds `eden_wire/__init__.py` to the touched-files
list.

### Decision 7 — Tests follow callers; no tests-tree-restructure in F-4

**Decision.** Wire-binding tests in
[`reference/packages/eden-wire/tests/`](../../reference/packages/eden-wire/tests/)
are already split by topic (auth, checkpoints, groups, lifecycle,
reassign+dispatch, workers, roundtrip, schema-parity, artifacts).
Each test's HTTP behavior is invariant under F-4; the only
in-test change is **call-site migration** — `sc.claim(...)`
becomes `sc.tasks.claim(...)`. That migration is mechanical.

**Why no tests-tree-restructure.** Mirrors F-3 Decision 6:
behavior-level tests passing through the refactor are the
strongest validation of "no semantic change." Splitting tests
*during* the refactor obscures that signal. If post-F-4 the team
wants per-sub-client test alignment, that's a clean follow-up
PR with no F-4 dependency.

**Per-test caller-migration churn.** Estimated ~250–400 individual
call sites across the eden-wire test files (each
`StoreClient` instance × 1–10 methods per test × ~80 tests).
Migration is grep-driven; pyright is the safety net (§1.6 #1).

### Decision 8 — `StoreClient` post-refactor target shape

**Decision.** Post-F-4 `client.py` is approximately:

- ~40-line `IndeterminateError` re-export block (5 classes
  re-imported from sub-modules + `__all__`).
- The `StoreClient` class itself: docstring + `__init__` (the
  transport setup + sub-client instantiation block) + four
  lifecycle methods (`close`, `__enter__`, `__exit__`,
  `experiment_id` property). ~80 lines.
- The `store_client(...)` context-manager factory: ~10 lines,
  unchanged.

**Target file SLOC: ~130–160.** Well under the 800 file-SLOC
gate; no function exceeds the 100-LEN gate. The
`# slop-allow-file:` annotation at line 1 is removed in the same
wave that lands the refactor.

**Why.** The audit's predicted post-refactor shape was "~100 LOC";
the actual target is slightly higher because of the
exception-re-export block + per-sub-client `__init__` lines, neither
of which the audit anticipated. Both are necessary; both are
linear (CC=1 lines). Far below threshold.

## 3. Design

### 3.1 Target file layout

```text
reference/packages/eden-wire/src/eden_wire/
├── __init__.py              # public surface — unchanged exports
├── auth.py                  # unchanged
├── client.py                # ~130-160 LOC after refactor (the StoreClient assembler + re-exports)
├── errors.py                # unchanged
├── models.py                # unchanged
├── server.py                # untouched by F-4 (F-3 territory)
└── _client/                 # NEW
    ├── __init__.py          # empty (or re-exports the *Client classes for type-check ergonomics)
    ├── _transport.py        # ClientTransport dataclass + request/maybe_json/assert_bearer helpers
    ├── _common.py           # _now (shared helper); future shared helpers go here
    ├── tasks.py             # TasksClient (10 routes incl. reassign) + _submission_to/from_wire + _task_targets_equal + _as_wire_datetime + IndeterminateReassign
    ├── ideas.py             # IdeasClient (4 routes)
    ├── variants.py          # VariantsClient (5 routes) + IndeterminateIntegration
    ├── dispatch_mode.py     # DispatchModeClient (2 routes) + IndeterminateDispatchModeUpdate
    ├── experiment_lifecycle.py  # ExperimentLifecycleClient (3 routes: terminate / policy-errors / state + update_experiment_state stub) + IndeterminateTermination
    ├── experiment_read.py   # ExperimentReadClient (1 route)
    ├── events.py            # EventsClient (read_range + subscribe + events/replay aliases)
    ├── workers.py           # WorkersClient (5 routes: register + reissue + verify + whoami + read + list)
    ├── groups.py            # GroupsClient (6 routes + resolve_worker_in_group transitive walk)
    ├── checkpoints.py       # CheckpointsClient (export + import + _parse_recovery_manifest + _fetch_recovery_probe + _import_recovery_probe) + IndeterminateImport
    └── reference.py         # ReferenceClient (validate_acceptance + validate_terminal + validate_evaluation)
```

Total estimated SLOC (rough): `_transport.py` 90 + `_common.py`
20 + `tasks.py` 220 + `ideas.py` 60 + `variants.py` 110 +
`dispatch_mode.py` 100 + `experiment_lifecycle.py` 130 +
`experiment_read.py` 50 + `events.py` 70 + `workers.py` 140 +
`groups.py` 120 + `checkpoints.py` 230 + `reference.py` 60 ≈ 1400
SLOC across 14 files (vs **1385 LOC in 1 file** today, ~equivalent
net). The dominant change is distribution: **no file exceeds the
800-SLOC threshold; no function exceeds the 100-LEN or 20-CC
thresholds.**

### 3.2 `ClientTransport` shape + lifecycle

```python
# eden_wire/_client/_transport.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Any

import httpx

from ..errors import raise_for_envelope


@dataclass(slots=True)
class ClientTransport:
    """Shared HTTP plumbing for every per-resource sub-client.

    One instance per :class:`StoreClient`; each sub-client holds a
    reference to it. Owns (or borrows) the underlying
    ``httpx.Client`` so a single connection pool serves all
    resources.
    """
    base_url: str
    base: str
    ref_base: str
    experiment_id: str
    headers: dict[str, str]
    bearer: str | None
    client: httpx.Client
    owns_client: bool
    timeout: float
    read_back_attempts: int

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        extra_headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        # ... (current _request body verbatim)

    def assert_bearer_matches_worker_id(self, worker_id: str) -> None:
        # ... (current _assert_bearer_matches_worker_id body verbatim)

    @staticmethod
    def maybe_json(resp: httpx.Response) -> Any:
        try:
            return resp.json()
        except Exception:
            return None
```

The fields mirror the current `StoreClient.__init__` state. The
helper methods are byte-equivalent to the current `_request` /
`_assert_bearer_matches_worker_id` / `_maybe_json` bodies — no
behavior change.

### 3.3 Sub-client skeleton (per-module)

Each per-resource sub-client follows the same shape: a small
class taking a `ClientTransport`, with one method per HTTP
operation. Example for `TasksClient`:

```python
# eden_wire/_client/tasks.py
from __future__ import annotations
from datetime import datetime
from typing import Any

import httpx
from eden_contracts import (
    EvaluationTask, ExecutionTask, IdeationTask,
    ReclaimCause, Task, TaskAdapter, TaskClaim, TaskTarget, FailReason,
)
from eden_storage.errors import InvalidPrecondition
from eden_storage.submissions import (
    Submission, submission_from_payload, submission_to_payload,
)

from ._transport import ClientTransport
from ._common import _now

__all__ = ["IndeterminateReassign", "TasksClient"]


class IndeterminateReassign(RuntimeError):
    """A ``reassign_task`` call's outcome cannot be determined.

    ... (docstring verbatim from current client.py)
    """


class TasksClient:
    """Wire client for ``/v0/experiments/{E}/tasks`` routes.

    Mirrors :class:`eden_wire.routers.tasks` (F-3) one-to-one.
    """

    def __init__(self, transport: ClientTransport) -> None:
        self._transport = transport

    # Reads --------------------------------------------------------
    def read(self, task_id: str) -> Task:
        resp = self._transport.request("GET", f"{self._transport.base}/tasks/{task_id}")
        return TaskAdapter.validate_python(resp.json())

    def list(
        self, *, kind: str | None = None, state: str | None = None
    ) -> list[Task]:
        params: dict[str, Any] = {}
        if kind is not None:
            params["kind"] = kind
        if state is not None:
            params["state"] = state
        resp = self._transport.request(
            "GET", f"{self._transport.base}/tasks", params=params
        )
        return [TaskAdapter.validate_python(item) for item in resp.json()]

    def read_submission(self, task_id: str) -> Submission | None:
        # ... (current read_submission body, using self._transport)

    # Lifecycle ---------------------------------------------------
    def create(self, task: Task) -> Task:
        # ... (current create_task body)

    def create_ideation(self, task_id: str) -> IdeationTask:
        # ... (current create_ideation_task body — note: uses _now from _common)

    def create_execution(
        self,
        task_id: str,
        idea_id: str,
        *,
        target: TaskTarget | None = None,
    ) -> ExecutionTask:
        # ... (current create_execution_task body)

    def create_evaluation(self, task_id: str, variant_id: str) -> EvaluationTask:
        # ... (current create_evaluation_task body)

    def claim(
        self,
        task_id: str,
        worker_id: str,
        *,
        expires_at: datetime | str | None = None,
    ) -> TaskClaim:
        # ... (current claim body, calling self._transport.assert_bearer_matches_worker_id)

    def submit(
        self, task_id: str, worker_id: str, submission: Submission
    ) -> None:
        # ... (current submit body)

    def accept(self, task_id: str) -> None:
        # ... (current accept body)

    def reject(self, task_id: str, reason: FailReason) -> None:
        # ... (current reject body)

    def reclaim(self, task_id: str, cause: ReclaimCause) -> None:
        # ... (current reclaim body)

    def reassign(
        self,
        task_id: str,
        new_target: TaskTarget | None,
        *,
        reason: str,
        reassigned_by: str,
    ) -> Task:
        """Reassign a task's target over the wire (§2.7).

        ... (current reassign_task docstring + body verbatim — read-back ladder preserved)
        """
        # ... (full method body unchanged except `self._request` → `self._transport.request`
        #      and `self._try_read_task` → self-method-internal helper)

    # Reference-route delegators (kept on TasksClient for ergonomics) --
    def validate_acceptance(self, task_id: str) -> str | None:
        # ... (current body)

    def validate_terminal(self, task_id: str) -> tuple[str, str | None]:
        # ... (current body)

    # Helpers (module-private) ------------------------------------
    def _try_read_task(self, task_id: str) -> Task | None:
        for _ in range(self._transport.read_back_attempts):
            try:
                return self.read(task_id)
            except Exception:
                continue
        return None


# Module-level utilities co-located with their sole consumer:

def _task_targets_equal(a: TaskTarget | None, b: TaskTarget | None) -> bool:
    # ... (verbatim from current client.py)


def _submission_to_wire(submission: Submission) -> dict[str, Any]:
    kind, payload = submission_to_payload(submission)
    return {"kind": kind, **payload}


def _submission_from_wire(kind: str, payload: dict[str, Any]) -> Submission:
    return submission_from_payload(kind, payload)


def _as_wire_datetime(value: datetime | str) -> str:
    # ... (verbatim from current client.py)
```

**Method naming inside sub-clients.** The flat method `claim` on
`StoreClient` becomes `TasksClient.claim`; `create_variant` on
`StoreClient` becomes `VariantsClient.create`. The
`<verb>_<noun>` shape on the monolith collapses into `<verb>`
on the noun-scoped sub-client — `client.variants.create(variant)`
reads as well as `client.create_variant(variant)` and is shorter.
For methods whose verb is implicit ("read" the variant), the
sub-client uses the explicit verb (`client.variants.read(variant_id)`),
which is unambiguous in context. Specific name mappings per
method are listed in §4 (resource → method mapping).

**Reference routes (validate_acceptance / validate_terminal /
validate_evaluation) split across two sub-clients.**
`validate_acceptance` and `validate_terminal` are
task-scoped (they take a `task_id`); they live on `TasksClient`.
`validate_evaluation` is experiment-scoped (no task or variant
id; takes a raw evaluation dict for shape-check); it lives on
`ReferenceClient`. F-3's server-side routers split these the same
way (`/_reference/.../tasks/{id}/validate-terminal` is in
`routers/reference.py` server-side, but the task_id parameter
makes it ergonomically a task operation on the client side; the
client doesn't observe the server's routing decisions).
Alternative: put both `validate_*` on `ReferenceClient` for tight
symmetry with the server's `routers/reference.py`. Tradeoff
discussed in §7.2.

**Why classes per sub-client rather than module-level
functions.**

A bare-function shape (`def claim(transport, task_id, ...)`)
would work for the wire dispatching but would put every method's
name in `_client/tasks.py`'s top-level namespace, where they'd
have to be prefixed (`def tasks_claim` to avoid colliding with
`def ideas_claim` if both shared `_client/__init__.py`). A class
gives each sub-client its own namespace at no extra cost. Three
additional benefits: (a) `__init__(transport)` is the natural
place to store the transport reference; (b) the attribute-access
shape on `StoreClient` (`client.tasks.claim`) requires an
attribute holding a value — a class instance is the obvious
choice; (c) future state per sub-client (per-resource caches,
per-resource retry budgets) has a natural home without
re-architecting.

### 3.4 `StoreClient` post-refactor shape

```python
# eden_wire/client.py — post-refactor
"""``StoreClient`` — composed HTTP client over the EDEN wire binding.

After F-4 (#116), the client is a thin assembler that constructs
a shared :class:`ClientTransport` and instantiates one of each
per-resource sub-client over it. Callers reach methods through
attribute paths:

    client.tasks.claim(task_id, worker_id)
    client.variants.create(variant)
    client.checkpoints.export(stream)

The flat-method surface that previously satisfied the
:class:`eden_storage.Store` Protocol is gone; the wire binding
no longer claims Store-Protocol structural conformance (see
[`docs/plans/refactor-f4-client-per-resource.md`](../../docs/plans/refactor-f4-client-per-resource.md)
§1.6 for the rationale).

The five ``Indeterminate*`` exception classes are re-exported
from this module for back-compatibility of imports.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx

from ._client._transport import ClientTransport
from ._client.checkpoints import CheckpointsClient, IndeterminateImport
from ._client.dispatch_mode import DispatchModeClient, IndeterminateDispatchModeUpdate
from ._client.events import EventsClient
from ._client.experiment_lifecycle import (
    ExperimentLifecycleClient, IndeterminateTermination,
)
from ._client.experiment_read import ExperimentReadClient
from ._client.groups import GroupsClient
from ._client.ideas import IdeasClient
from ._client.reference import ReferenceClient
from ._client.tasks import IndeterminateReassign, TasksClient
from ._client.variants import IndeterminateIntegration, VariantsClient
from ._client.workers import WorkersClient

__all__ = [
    "IndeterminateDispatchModeUpdate",
    "IndeterminateImport",
    "IndeterminateIntegration",
    "IndeterminateReassign",
    "IndeterminateTermination",
    "StoreClient",
    "store_client",
]


class StoreClient:
    """Composed HTTP client. Methods live on per-resource sub-clients."""

    def __init__(
        self,
        base_url: str,
        experiment_id: str,
        *,
        bearer: str | None = None,
        token: str | None = None,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
        read_back_attempts: int = 3,
    ) -> None:
        base_url_stripped = base_url.rstrip("/")
        headers: dict[str, str] = {"X-Eden-Experiment-Id": experiment_id}
        effective_bearer = bearer or token
        if effective_bearer is not None:
            headers["Authorization"] = f"Bearer {effective_bearer}"
        owns_client = client is None
        http_client = client if client is not None else httpx.Client(timeout=timeout)

        self._transport = ClientTransport(
            base_url=base_url_stripped,
            base=f"{base_url_stripped}/v0/experiments/{experiment_id}",
            ref_base=f"{base_url_stripped}/_reference/experiments/{experiment_id}",
            experiment_id=experiment_id,
            headers=headers,
            bearer=effective_bearer,
            client=http_client,
            owns_client=owns_client,
            timeout=timeout,
            read_back_attempts=read_back_attempts,
        )

        # Sub-clients in F-3 include-order.
        self.tasks = TasksClient(self._transport)
        self.ideas = IdeasClient(self._transport)
        self.variants = VariantsClient(self._transport)
        self.dispatch_mode = DispatchModeClient(self._transport)
        self.experiment_lifecycle = ExperimentLifecycleClient(self._transport)
        self.events = EventsClient(self._transport)
        self.workers = WorkersClient(self._transport)
        self.groups = GroupsClient(self._transport)
        self.experiment_read = ExperimentReadClient(self._transport)
        self.checkpoints = CheckpointsClient(self._transport)
        self.reference = ReferenceClient(self._transport)

    def close(self) -> None:
        if self._transport.owns_client:
            self._transport.client.close()

    def __enter__(self) -> StoreClient:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    @property
    def experiment_id(self) -> str:
        return self._transport.experiment_id


@contextmanager
def store_client(
    base_url: str, experiment_id: str, **kwargs: Any
) -> Iterator[StoreClient]:
    """Context-manager convenience for :class:`StoreClient`."""
    sc = StoreClient(base_url, experiment_id, **kwargs)
    try:
        yield sc
    finally:
        sc.close()
```

The `# slop-allow-file:` annotation at line 1 of the current
client.py is removed in this rewrite. No new slop-allow needed
(every file under 800 SLOC; every function under 100 LOC).

## 4. Resource → sub-client method mapping

Complete mapping from every current `StoreClient.<method>` to its
post-refactor accessor path. Verified by reading
[`reference/packages/eden-wire/src/eden_wire/client.py`](../../reference/packages/eden-wire/src/eden_wire/client.py)
top to bottom at HEAD `f3f7932`. **51 `StoreClient` methods map
onto 43 server routes plus 8 client-only extras** (the 8 are
enumerated in §4.1 below; verified one-to-one against F-3 in
§4.1).

| Current method | New accessor path | Module |
|---|---|---|
| `read_task(task_id)` | `client.tasks.read(task_id)` | `_client/tasks.py` |
| `read_idea(idea_id)` | `client.ideas.read(idea_id)` | `_client/ideas.py` |
| `read_variant(variant_id)` | `client.variants.read(variant_id)` | `_client/variants.py` |
| `read_submission(task_id)` | `client.tasks.read_submission(task_id)` | `_client/tasks.py` |
| `list_tasks(...)` | `client.tasks.list(...)` | `_client/tasks.py` |
| `list_ideas(...)` | `client.ideas.list(...)` | `_client/ideas.py` |
| `list_variants(...)` | `client.variants.list(...)` | `_client/variants.py` |
| `events()` | `client.events.list()` *(aliased via `events.read_range(0)`)* | `_client/events.py` |
| `replay()` | `client.events.replay()` *(synonym of `events.list`)* | `_client/events.py` |
| `read_range(cursor)` | `client.events.read_range(cursor)` | `_client/events.py` |
| `subscribe(cursor, *, timeout)` | `client.events.subscribe(cursor, timeout=...)` | `_client/events.py` |
| `create_task(task)` | `client.tasks.create(task)` | `_client/tasks.py` |
| `create_ideation_task(task_id)` | `client.tasks.create_ideation(task_id)` | `_client/tasks.py` |
| `create_execution_task(task_id, idea_id, *, target)` | `client.tasks.create_execution(task_id, idea_id, target=...)` | `_client/tasks.py` |
| `create_evaluation_task(task_id, variant_id)` | `client.tasks.create_evaluation(task_id, variant_id)` | `_client/tasks.py` |
| `claim(task_id, worker_id, *, expires_at)` | `client.tasks.claim(task_id, worker_id, expires_at=...)` | `_client/tasks.py` |
| `submit(task_id, worker_id, submission)` | `client.tasks.submit(task_id, worker_id, submission)` | `_client/tasks.py` |
| `accept(task_id)` | `client.tasks.accept(task_id)` | `_client/tasks.py` |
| `reject(task_id, reason)` | `client.tasks.reject(task_id, reason)` | `_client/tasks.py` |
| `reclaim(task_id, cause)` | `client.tasks.reclaim(task_id, cause)` | `_client/tasks.py` |
| `reassign_task(...)` | `client.tasks.reassign(...)` | `_client/tasks.py` |
| `validate_acceptance(task_id)` | `client.tasks.validate_acceptance(task_id)` | `_client/tasks.py` |
| `validate_terminal(task_id)` | `client.tasks.validate_terminal(task_id)` | `_client/tasks.py` |
| `create_idea(idea)` | `client.ideas.create(idea)` | `_client/ideas.py` |
| `mark_idea_ready(idea_id)` | `client.ideas.mark_ready(idea_id)` | `_client/ideas.py` |
| `create_variant(variant)` | `client.variants.create(variant)` | `_client/variants.py` |
| `declare_variant_evaluation_error(variant_id)` | `client.variants.declare_evaluation_error(variant_id)` | `_client/variants.py` |
| `integrate_variant(variant_id, sha)` | `client.variants.integrate(variant_id, sha)` | `_client/variants.py` |
| `validate_evaluation(evaluation)` | `client.reference.validate_evaluation(evaluation)` | `_client/reference.py` |
| `register_worker(worker_id, *, labels)` | `client.workers.register(worker_id, labels=...)` | `_client/workers.py` |
| `reissue_credential(worker_id)` | `client.workers.reissue_credential(worker_id)` | `_client/workers.py` |
| `verify_worker_credential(worker_id, token)` | `client.workers.verify_credential(worker_id, token)` | `_client/workers.py` |
| `whoami()` | `client.workers.whoami()` | `_client/workers.py` |
| `read_worker(worker_id)` | `client.workers.read(worker_id)` | `_client/workers.py` |
| `list_workers()` | `client.workers.list()` | `_client/workers.py` |
| `register_group(group_id, *, members)` | `client.groups.register(group_id, members=...)` | `_client/groups.py` |
| `add_to_group(group_id, member_id)` | `client.groups.add_member(group_id, member_id)` | `_client/groups.py` |
| `remove_from_group(group_id, member_id)` | `client.groups.remove_member(group_id, member_id)` | `_client/groups.py` |
| `delete_group(group_id)` | `client.groups.delete(group_id)` | `_client/groups.py` |
| `read_group(group_id)` | `client.groups.read(group_id)` | `_client/groups.py` |
| `list_groups()` | `client.groups.list()` | `_client/groups.py` |
| `resolve_worker_in_group(worker_id, group_id)` | `client.groups.resolve_worker_in_group(worker_id, group_id)` | `_client/groups.py` |
| `read_dispatch_mode()` | `client.dispatch_mode.read()` | `_client/dispatch_mode.py` |
| `update_dispatch_mode(updates, *, updated_by)` | `client.dispatch_mode.update(updates, updated_by=...)` | `_client/dispatch_mode.py` |
| `read_experiment()` | `client.experiment_read.read()` | `_client/experiment_read.py` |
| `read_experiment_state()` | `client.experiment_lifecycle.read_state()` | `_client/experiment_lifecycle.py` |
| `update_experiment_state(new_state)` | `client.experiment_lifecycle.update_state(new_state)` *(still NotImplementedError; preserved for Store-Protocol parity if future amendment exposes it)* | `_client/experiment_lifecycle.py` |
| `emit_policy_error(*, policy_kind, error_type, error_message)` | `client.experiment_lifecycle.emit_policy_error(policy_kind=..., error_type=..., error_message=...)` | `_client/experiment_lifecycle.py` |
| `terminate_experiment(*, reason, terminated_by)` | `client.experiment_lifecycle.terminate(reason=..., terminated_by=...)` | `_client/experiment_lifecycle.py` |
| `export_checkpoint(stream, ...)` | `client.checkpoints.export(stream, ...)` | `_client/checkpoints.py` |
| `import_checkpoint(stream, *, as_experiment_id, ...)` | `client.checkpoints.import_(stream, as_experiment_id=...)` *(trailing underscore — see §7.3)* | `_client/checkpoints.py` |

**51 StoreClient methods map onto 43 server routes plus 8
client-only extras** (the 8 are enumerated in §4.1 below). No
method is unmapped; no method maps to two sub-clients.
Independently verified by `grep -cE '^    def [a-z]' client.py`
→ 53, minus `close` + the `experiment_id` property = 51.

### 4.1 Server-side router ↔ client-side sub-client symmetry

Every server router from F-3 §4 has a matching client sub-client
in §4. Verification:

| F-3 router | F-4 sub-client | Method count |
|---|---|---:|
| `routers/tasks.py` (10 routes) | `TasksClient` | 10 lifecycle + 2 reference-route delegators (validate_terminal / validate_acceptance) + 3 task-creation conveniences (create_ideation/execution/evaluation) = 13 methods *(server-route count + client convenience methods that compose multiple server calls or model setup)* |
| `routers/ideas.py` (4 routes) | `IdeasClient` | 4 methods |
| `routers/variants.py` (5 routes) | `VariantsClient` | 5 methods |
| `routers/dispatch_mode.py` (2 routes) | `DispatchModeClient` | 2 methods |
| `routers/experiment_lifecycle.py` (3 routes) | `ExperimentLifecycleClient` | 4 methods (3 routes + `update_experiment_state` NotImplementedError stub) |
| `routers/experiment_read.py` (1 route) | `ExperimentReadClient` | 1 method |
| `routers/events.py` (2 routes) | `EventsClient` | 4 methods (read_range + subscribe + 2 aliases `events`/`replay` kept for Store-protocol-name parity even though Protocol claim drops) |
| `routers/workers.py` (5 routes) | `WorkersClient` | 6 methods (5 routes + `verify_worker_credential` which is a `whoami`-with-bearer-swap probe, no new route) |
| `routers/groups.py` (6 routes) | `GroupsClient` | 7 methods (6 routes + `resolve_worker_in_group` transitive walk = repeated `read_group` calls client-side) |
| `routers/checkpoints.py` (2 routes) | `CheckpointsClient` | 2 methods (export + import) |
| `routers/reference.py` (3 routes) | `ReferenceClient` + `TasksClient.validate_*` | 1 method on Reference (validate_evaluation); 2 on Tasks (validate_terminal + validate_acceptance) — see §7.2 for the split rationale |

**Net: 43 server routes ↔ 51 client methods.** The 8 "client-side"
extras over the 43-route count are:

1. 3 convenience task-creation factories
   (`create_ideation_task` / `create_execution_task` /
   `create_evaluation_task`) — each composes a `Task.model_validate(...)`
   followed by `create_task(...)`; one server route, three client conveniences.
2. 2 aliases on EventsClient (`events()` and `replay()`, both
   forwarders for `read_range(0)`) — kept for spec-vocabulary
   parity with chapter 8 §2.1.
3. The synthetic `update_experiment_state` stub (raises
   NotImplementedError; preserved for Store-Protocol-signature
   parity — see §7.5).
4. The client-side transitive `resolve_worker_in_group` walk
   (repeated `read_group` calls; chapter 02 §7.2 closure
   computed in the client, not on the wire).
5. The client-side `verify_worker_credential` probe (one bearer-
   swap call against `GET /whoami`; no new server route).

All extras exist in the current monolithic client and are
preserved verbatim by F-4.

## 5. Files to touch

### 5.1 New files

| File | Change |
|---|---|
| `reference/packages/eden-wire/src/eden_wire/_client/__init__.py` (new) | Package marker. Optionally re-exports each `*Client` class for downstream type imports; recommend empty to keep internal-only posture (callers should not need to import sub-client classes — they reach them through `client.tasks` / etc.). |
| `reference/packages/eden-wire/src/eden_wire/_client/_transport.py` (new) | `ClientTransport` dataclass + `request` / `assert_bearer_matches_worker_id` / `maybe_json` helpers (Decision 2 + 3). |
| `reference/packages/eden-wire/src/eden_wire/_client/_common.py` (new) | `_now()` (shared by TasksClient + ExperimentLifecycleClient). |
| `reference/packages/eden-wire/src/eden_wire/_client/tasks.py` (new) | `TasksClient` + `IndeterminateReassign` + module-local helpers (`_task_targets_equal`, `_submission_to_wire`, `_submission_from_wire`, `_as_wire_datetime`). |
| `reference/packages/eden-wire/src/eden_wire/_client/ideas.py` (new) | `IdeasClient`. |
| `reference/packages/eden-wire/src/eden_wire/_client/variants.py` (new) | `VariantsClient` + `IndeterminateIntegration`. |
| `reference/packages/eden-wire/src/eden_wire/_client/dispatch_mode.py` (new) | `DispatchModeClient` + `IndeterminateDispatchModeUpdate`. |
| `reference/packages/eden-wire/src/eden_wire/_client/experiment_lifecycle.py` (new) | `ExperimentLifecycleClient` + `IndeterminateTermination`. |
| `reference/packages/eden-wire/src/eden_wire/_client/experiment_read.py` (new) | `ExperimentReadClient`. |
| `reference/packages/eden-wire/src/eden_wire/_client/events.py` (new) | `EventsClient`. |
| `reference/packages/eden-wire/src/eden_wire/_client/workers.py` (new) | `WorkersClient`. |
| `reference/packages/eden-wire/src/eden_wire/_client/groups.py` (new) | `GroupsClient`. |
| `reference/packages/eden-wire/src/eden_wire/_client/checkpoints.py` (new) | `CheckpointsClient` + `IndeterminateImport` + `_parse_recovery_manifest` + `_fetch_recovery_probe` + `_import_recovery_probe`. |
| `reference/packages/eden-wire/src/eden_wire/_client/reference.py` (new) | `ReferenceClient` (`validate_evaluation` route; the two task-scoped validate routes live on `TasksClient`). |

### 5.2 Modified files (eden-wire package)

| File | Change |
|---|---|
| `reference/packages/eden-wire/src/eden_wire/client.py` | Reduced to ~130–160 LOC: docstring + 5 exception re-exports + `__all__` + the slim `StoreClient` (init + lifecycle only) + `store_client` factory. Drops the `# slop-allow-file:` (line 1). |
| `reference/packages/eden-wire/src/eden_wire/__init__.py` | **Touched** (verified round-1): currently re-exports 4 of the 5 `Indeterminate*` classes at the top-level (`IndeterminateDispatchModeUpdate`, `IndeterminateIntegration`, `IndeterminateReassign`, `IndeterminateTermination`) and documents `StoreClient` as "satisfies the `Store` Protocol" in the module docstring. Wave 4 (a) updates the docstring to remove the Store-Protocol claim, (b) decides explicitly whether to add `IndeterminateImport` to the top-level re-exports (recommendation: yes, for symmetry — call out in PR description). |
| `reference/packages/eden-wire/README.md` | **Touched** (verified round-2): line 3 claims `StoreClient` "satisfies the same `Store` Protocol from the opposite side" — this becomes false under F-4 Option C. Wave 4 amends to "previously satisfied the Store Protocol; after F-4 (issue #116), the public surface is composed per-resource sub-clients" or equivalent. |

### 5.3 Modified files (Store-Protocol-typed callsites — Section A)

These files accept `: Store`-typed parameters that, at runtime,
sometimes receive a `StoreClient`. After F-4, the StoreClient
no longer satisfies the Store Protocol (§1.6). Each entry below
needs ONE of: (a) retype the parameter from `: Store` to
`: StoreClient` (when the implementation is wire-bound by
construction), or (b) keep `: Store` and pass through a
**consumer-side adapter** that satisfies a narrower local
Protocol — the integrator (§7.1 lead) and dispatch (§7.1
follow-on) both follow this shape. **Both adapters live in the
consumer package, NOT on `StoreClient`** — they are not
delegation shims on the wire client surface.

Default disposition for service-side callers is **(a) retype to
`: StoreClient`**. The two library-package callers (integrator +
dispatch) use the **(b) consumer-side adapter** pattern per §7.1.

| File | Function(s) | Disposition |
|---|---|---|
| `reference/packages/eden-git/src/eden_git/integrator.py` | `Integrator.__init__(store: Store, …)` | Keep `: Store` typing using a local `IntegratorStore` Protocol (§7.1 lead). Reads 4 store methods: `read_variant`, `read_idea`, `integrate_variant`, `validate_evaluation`. A `_StoreClientBridge` in `eden_git/_integrator_store.py` adapts a StoreClient via its composed sub-clients; in-process Stores satisfy the narrower Protocol structurally because they retain the flat methods. |
| `reference/packages/eden-dispatch/src/eden_dispatch/driver.py` | functions taking `store: Store` | Keep `: Store` typing using a local `DispatchStore` Protocol (§7.1 follow-on). The dispatch package uses 21 unique store methods across its 4 current caller files; one `_StoreClientBridge` in `eden_dispatch/_dispatch_store.py` (new file added by F-4) adapts a StoreClient via composed sub-clients. |
| `reference/packages/eden-dispatch/src/eden_dispatch/sweep.py` | functions taking `store: Store` | Same — covered by the `eden_dispatch/_dispatch_store.py` bridge. |
| `reference/packages/eden-dispatch/src/eden_dispatch/workers.py` | functions taking `store: Store` | Same. |
| `reference/packages/eden-dispatch/src/eden_dispatch/state_view.py` | functions taking `store: Store` | Same. |
| `reference/packages/eden-storage/src/eden_storage/protocol.py` | `class Store(Protocol)` itself | No change. Store Protocol stays flat; this is the point of Option C (§1.7). |
| `reference/packages/eden-storage/src/eden_storage/_base.py` / `_checkpoint.py` | In-process Store implementations | No change. They satisfy the flat Store Protocol; their callers (in-process) keep working. |
| `reference/services/orchestrator/src/eden_orchestrator/{loop.py, multi_loop.py, cli.py, control_plane_bootstrap.py}` | Functions/classes that accept `store: Store` and may receive a StoreClient | Retype to `: StoreClient` (orchestrator is wire-bound; the in-process Store path is for unit tests only and tests can keep their own narrower typings). |
| `reference/services/executor/src/eden_executor_host/{cli.py, host.py, subprocess_mode.py}` | Same | Retype to `: StoreClient`. |
| `reference/services/evaluator/src/eden_evaluator_host/{cli.py, host.py, subprocess_mode.py}` | Same | Retype to `: StoreClient`. |
| `reference/services/ideator/src/eden_ideator_host/{cli.py, host.py, subprocess_mode.py}` | Same | Retype to `: StoreClient`. |
| `reference/services/web-ui/src/eden_web_ui/{cli.py, app.py, routes/}` | Multiple; some already `: StoreClient` (verified in cli.py:225), others may be `: Store` | Audit each; retype to `: StoreClient` for the ones that flow a wire client. |
| `reference/services/control-plane/src/eden_control_plane_server/{state_sync.py, app.py, auth.py}` | Same | Retype to `: StoreClient`. |
| `reference/services/_common/src/eden_service_common/auth.py` | `_resolve_bearer` constructs a transient StoreClient and calls `.whoami()` | Already StoreClient-typed; just migrate the call shape (`sc.whoami()` → `sc.workers.whoami()`). |
| `reference/services/task-store-server/src/eden_task_store_server/app.py` | `make_app(store: Store)` | This is the SERVER. `store` here is the in-process backend, not a StoreClient. **No retype needed.** Server-side never holds a StoreClient. |

### 5.4 Modified files (StoreClient direct callers — Section B)

These files explicitly construct or annotate `StoreClient`
instances and call methods on them. Every method call migrates
to the composed accessor. This is the bulk of the F-4 PR's
file-touch count. **Pyright catches every missed site** (§1.6
item 1).

#### 5.4.1 `eden-wire` package (the home; tests + the file itself)

| File | StoreClient call-site count (rough) |
|---|---:|
| `reference/packages/eden-wire/tests/test_auth.py` | ~50 (every test instantiates one or two and exercises a handful of methods) |
| `reference/packages/eden-wire/tests/test_wire_roundtrip.py` | ~80 (largest test file; touches every resource) |
| `reference/packages/eden-wire/tests/test_workers_wire.py` | ~25 |
| `reference/packages/eden-wire/tests/test_groups_wire.py` | ~20 |
| `reference/packages/eden-wire/tests/test_lifecycle_wire.py` | ~25 (terminate / policy-error / dispatch_mode read; flaky-transport indeterminate-recovery tests) |
| `reference/packages/eden-wire/tests/test_reassign_dispatch_wire.py` | ~30 |
| `reference/packages/eden-wire/tests/test_checkpoint_wire.py` | ~15 (export + import roundtrips; indeterminate-import recovery) |
| `reference/packages/eden-wire/tests/test_artifact_route.py` | 0 — REMOVED (round-1): uses `TestClient(make_app(...))`, not StoreClient. |
| `reference/packages/eden-wire/tests/test_wire_schema_parity.py` | 0 — REMOVED (round-1): Pydantic-↔-JSON-Schema parity check; no StoreClient instantiation. |

**Total wire-tests: ~245–255 call sites** (revised down from
round-0's ~250–260 after removing `test_artifact_route.py` and
`test_wire_schema_parity.py`).

#### 5.4.2 Services and their tests

| File | Rough call-site count | Notes |
|---|---:|---|
| `reference/services/orchestrator/src/eden_orchestrator/{loop.py, multi_loop.py, cli.py, control_plane_bootstrap.py}` | ~40 | Heaviest service: list_variants, create_task variants, accept/reject, emit_policy_error, terminate_experiment, read_experiment_state. |
| `reference/services/orchestrator/tests/{test_e2e.py, test_subprocess_e2e.py, test_loop_unit.py}` | ~50 | E2E tests seed via StoreClient + drive the orchestrator. |
| `reference/services/executor/src/eden_executor_host/{cli.py, host.py, subprocess_mode.py}` | ~12 | claim, submit, declare_variant_evaluation_error, read_task, create_variant. |
| `reference/services/executor/tests/{test_host.py, test_executor_subprocess.py}` | ~25 | |
| `reference/services/evaluator/src/eden_evaluator_host/{cli.py, host.py, subprocess_mode.py}` | ~10 | claim, submit, read_variant, validate_evaluation. |
| `reference/services/evaluator/tests/{test_evaluator_subprocess.py}` | ~15 | |
| `reference/services/ideator/src/eden_ideator_host/{cli.py, host.py, subprocess_mode.py}` | ~10 | claim, submit, create_idea, mark_idea_ready. |
| `reference/services/ideator/tests/{test_ideator_subprocess.py}` | ~10 | |
| `reference/services/web-ui/src/eden_web_ui/{cli.py, app.py, routes/}` | ~80 | Multiple admin routes call ~every wire method (workers, groups, dispatch_mode, lifecycle, integrator-equivalents, artifact-listing). |
| `reference/services/web-ui/tests/*` | ~150 | Largest test surface in the repo; admin + executor + evaluator + ideator e2e tests instantiate seed/operator/admin StoreClients per scenario. |
| `reference/services/control-plane/src/eden_control_plane_server/{state_sync.py, app.py, auth.py}` | ~20 | list_tasks / read_experiment_state / read_dispatch_mode for the federation sync. |
| `reference/services/control-plane/tests/test_server.py` (REMOVED) | 0 | Round-2 correction: this is the control-plane SERVER test (TestClient against the control-plane FastAPI app); no StoreClient instantiation. Verified by grepping the file for "StoreClient" — zero hits. Removed from migration list. |
| `reference/services/_common/src/eden_service_common/auth.py` | ~6 | StoreClient(...).whoami() probes (3 sites). |
| `reference/services/_common/tests/test_service_auth.py` | ~5 | |
| `reference/services/task-store-server/tests/test_artifacts_cli.py` (REMOVED) | 0 | Round-2 correction: this is the task-store-server CLI test (subprocess + curl/jq style assertions against the spawned wire server); no StoreClient instantiation. Removed from migration list. |

**Total services + their tests: ~450–500 call sites.**

#### 5.4.3 Other packages

| File | Rough call-site count | Notes |
|---|---:|---|
| `reference/packages/eden-git/src/eden_git/integrator.py` | ~6 | The integrator's Store-typed `store.read_variant` / `store.read_idea` / `store.integrate_variant` / `store.validate_evaluation` call sites (the 4-method needs-list verified in §7.1) — each migrates to a composed-form-via-bridge in the `IntegratorStore` Protocol shape. |
| `reference/packages/eden-git/tests/{test_integrator.py, test_remote_integrator.py}` | ~30 | |
| `reference/packages/eden-dispatch/src/eden_dispatch/*` (4 current caller files: `driver.py`, `sweep.py`, `state_view.py`, `workers.py`; F-4 adds a 5th file `_dispatch_store.py` for the bridge) | ~25 | All four current files accept `store: Store`. Bridge per §7.1. |
| `reference/packages/eden-dispatch/tests/*` (4 files) | ~30 | |
| `reference/packages/eden-storage/tests/*` (~12 files) | ~0 | These test the in-process Store directly; no StoreClient involvement. |
| `reference/packages/eden-control-plane/tests/test_store_protocol.py` | 0 | **Verified round-1**: this file tests the `ControlPlaneStore` (chapter-11 lease + dispatch state) backend conformance, NOT `StoreClient`. The file imports `from eden_control_plane import ControlPlaneStore, …`; there are no StoreClient references. Removed from the migration list. |
| `reference/packages/eden-control-plane/tests/test_client.py` | 0 | **Verified round-3 (REMOVED)**: tests `ControlPlaneClient` against `httpx.MockTransport`; no StoreClient instantiation. |
| `reference/packages/eden-checkpoint/tests/*` | 0 | **Verified round-1**: no StoreClient references in this package's tests (verified by `rg StoreClient reference/packages/eden-checkpoint/tests/`). Removed from the migration list. |

**Total other packages: ~95–105 call sites** (revised down from
round-0's ~120–130 after removing the false-positives in
`eden-control-plane/tests/` and `eden-checkpoint/tests/`).

#### 5.4.4 Conformance

| File | Rough call-site count | Notes |
|---|---:|---|
| `conformance/src/conformance/adapters/reference/adapter.py` | ~10 | Reference IUT adapter constructs `StoreClient`. Every method call migrates. |
| `conformance/src/conformance/scenarios/*` | 0 | Scenarios speak to the `IutAdapter` Protocol (NOT `StoreClient` directly). No call-site changes. |

**Total conformance: ~10 call sites.**

#### 5.4.5 Summary

**Estimated total: 800–900 individual call sites across ~80
files.** Migration is mechanical: every `<client>.<method>(...)` →
`<client>.<resource>.<method>(...)`. The pyright pass catches
missed migrations (every missed site is a "Cannot access
member" type error or, for retyped-from-Store sites, a "no such
attribute on StoreClient" error). Codex's impl-stage review on
the PR will catch any sites that pyright happens to type-erase
(e.g. `Any`-typed StoreClient instances).

### 5.5 Public-import sites for the `Indeterminate*` exception classes

Round-2 correction: the round-0 list was speculative. Actual
`Indeterminate*`-importing files, verified by single-line `rg`
(`rg "from eden_wire(\.client)? import.*Indeterminate" reference conformance`)
combined with manual inspection of the multiline `from eden_wire import (…)`
blocks in test_lifecycle_wire.py and test_reassign_dispatch_wire.py
— single-line `rg` does NOT match across newlines, so for impl,
use `rg --multiline -U "from eden_wire(\.client)? import \([^)]*Indeterminate"`
or simply `rg -l Indeterminate reference/packages/eden-wire/tests`:

| File | Import path | Classes imported |
|---|---|---|
| [`reference/packages/eden-wire/tests/test_wire_roundtrip.py:36`](../../reference/packages/eden-wire/tests/test_wire_roundtrip.py) | `from eden_wire.client import …` | `IndeterminateIntegration` |
| [`reference/packages/eden-wire/tests/test_checkpoint_wire.py:31`](../../reference/packages/eden-wire/tests/test_checkpoint_wire.py) | `from eden_wire.client import …` | `IndeterminateImport` |
| [`reference/packages/eden-wire/tests/test_lifecycle_wire.py:27`](../../reference/packages/eden-wire/tests/test_lifecycle_wire.py) | `from eden_wire import …` *(top-level)* | `IndeterminateTermination` |
| [`reference/packages/eden-wire/tests/test_reassign_dispatch_wire.py:31`](../../reference/packages/eden-wire/tests/test_reassign_dispatch_wire.py) | `from eden_wire import …` *(top-level)* | `IndeterminateDispatchModeUpdate`, `IndeterminateReassign` |

**No external service or non-test file** imports these classes
today (zero hits across `reference/services/`, `reference/packages/eden-git/`,
`reference/packages/eden-dispatch/`, and `conformance/`). The
exceptions ARE raised by code outside `eden-wire/`, but the
raise-site code catches via `except Exception` ladders rather
than explicit `except IndeterminateX:` clauses.

**Import-preservation discipline:**

- Decision 6's re-exports from `eden_wire/client` cover the two
  direct-import test files unchanged.
- The two top-level-import test files use names that
  `eden_wire/__init__.py` currently re-exports (4 of 5). Wave 4
  must keep those 4 re-exports AND (recommendation) add
  `IndeterminateImport` for symmetry — see Decision 6 + §5.2's
  `eden_wire/__init__.py` row.

If any future external caller adds an explicit `except IndeterminateX:`
clause, it can use whichever import path is convenient; the
plan's invariant is that the existing import lines stay valid.

### 5.6 Test files

| File | Change |
|---|---|
| `reference/packages/eden-wire/tests/*.py` | Migrate every call site to composed form (§5.4.1). Add the 4 regression tests from §6.2, including `test_storeclient_drops_flat_method_surface` (the runtime sibling of the pyright-level Protocol-break check). |
| `conformance/src/conformance/adapters/reference/adapter.py` | Migrate call sites (§5.4.4). The IUT adapter contract itself is unchanged — scenarios still see the same `IutAdapter` Protocol. |
| All other test files | Migrate call sites; no structural changes. |

### 5.7 Verification commands

| File | Change |
|---|---|
| `scripts/check-complexity.py` | No code change. Post-refactor, the script's `--list` output is one entry shorter (the file-level F-4 entry removed). Run `python3 scripts/check-complexity.py` at the end of every wave to confirm no new violators. |

### 5.8 Docs

| File | Change |
|---|---|
| `CHANGELOG.md` | Per-wave entries under `[Unreleased]`; consolidated on merge per AGENTS.md "Recording chunk completions". |
| `docs/roadmap.md` | Planless-shape one-liner pointing at the merged PR (this is an audit-deferred refactor, not a phase chunk). |
| `docs/audits/2026-05-20-phase-c-disposition.md` | Update §F-4 entry with the merged-PR ref and a "**resolved YYYY-MM-DD**" annotation. Also call out the surface-deviation from the disposition's original "public surface stays identical" risk-floor (§1.5 of this plan amends it). |

## 6. Test design

### 6.1 Behavioral preservation — the load-bearing gate

Every wave runs the full set of pre-existing tests (migrated to
composed call shape but otherwise unchanged). Acceptance = all
green.

| Command | Scope |
|---|---|
| `uv run pytest -q reference/packages/eden-wire/tests` | The 9 eden-wire test files at HEAD (~4800 LOC pre-migration). Covers tasks/ideas/variants/events/workers/groups/dispatch/lifecycle/checkpoints/auth/artifacts/schema-parity. Every call site migrates from flat to composed form; HTTP behavior invariant. |
| `uv run pytest -q reference/packages/eden-git/tests` | Integrator tests — these pass StoreClient (or fakes) into the integrator. Verify that the §7.1 isinstance-branch / local-Protocol strategy compiles cleanly through pyright. |
| `uv run pytest -q reference/packages/eden-dispatch/tests` | Dispatch driver / sweep / workers tests. Same caveat as eden-git. |
| `uv run pytest -q reference/services/{orchestrator,executor,evaluator,ideator}/tests` | Per-service unit + integration tests. The e2e tests spawn the wire server + drive via StoreClient — both the seed and the assertion path migrate. |
| `uv run pytest -q reference/services/web-ui/tests` | Largest test surface; ~150 call sites. Migration is mechanical. |
| `uv run pytest -q reference/services/control-plane/tests` | |
| `uv run pytest -q reference/services/_common/tests` | |
| `uv run pytest -q conformance/` | Black-box conformance against the reference IUT. **The strongest acceptance signal.** Every chapter-7 §1–§14 MUST is exercised through HTTP via the reference adapter (which migrates §5.4.4). |
| `uv run pytest -q` | Full reference test suite — the canonical pre-push gate per AGENTS.md "Commands". |

### 6.2 Targeted regression tests added during the refactor

| File | Test | Why |
|---|---|---|
| `reference/packages/eden-wire/tests/test_wire_roundtrip.py` | `test_storeclient_drops_flat_method_surface`: assert that the flat-surface method names are NOT attributes of a StoreClient instance — `assert not hasattr(sc, "claim")`, `assert not hasattr(sc, "create_variant")`, `assert not hasattr(sc, "integrate_variant")`, etc. (one assertion per representative method covering each sub-client). | The Protocol-break is the load-bearing consequence of the surface lock (§1.6). Pinning the absent-attributes as a runtime test prevents a future amendment from silently re-adding flat methods. **DOes NOT use `isinstance(sc, Store)`** — `Store` is a non-`runtime_checkable` Protocol (see [`protocol.py:34`](../../reference/packages/eden-storage/src/eden_storage/protocol.py)), so the isinstance call would raise `TypeError("Instance and class checks can only be used with @runtime_checkable protocols")`. The structural Protocol-break is enforced by pyright at the static level (per the Wave-4 gate in §8); the runtime test asserts the simpler invariant "these names are gone." |
| `reference/packages/eden-wire/tests/test_wire_roundtrip.py` | `test_sub_client_attributes_are_distinct_instances`: confirm `client.tasks is not client.ideas` (Decision 5 sanity-check). | Defense against an accidental "all attributes point to the same sub-client" bug if the `__init__` block is refactored later. |
| `reference/packages/eden-wire/tests/test_wire_roundtrip.py` | `test_transport_is_shared_across_sub_clients`: confirm `client.tasks._transport is client.variants._transport is client.events._transport`. | The audit's risk-prevention shape (one connection pool, one bearer, one header dict) depends on this. Test pins it so a future "let's give each sub-client its own httpx.Client" refactor surfaces. |
| `reference/packages/eden-wire/tests/test_wire_roundtrip.py` | `test_close_releases_transport_client`: confirm `client.close()` closes the underlying `httpx.Client` only when `owns_client` is True (i.e. when the caller didn't inject a client). | Mirrors the current implicit guarantee in `_owns_client`. Pin the contract. |

**Tests already covering invariants the plan needs.** Do NOT
duplicate:

- All transport-indeterminate read-back ladders are covered by
  the existing tests in `test_wire_roundtrip.py` /
  `test_lifecycle_wire.py` / `test_checkpoint_wire.py` /
  `test_reassign_dispatch_wire.py`. Migration of those tests
  to composed call shape leaves the coverage intact.
- `test_auth.py` covers the bearer-vs-worker-id preflight
  (`_assert_bearer_matches_worker_id` migrated to transport).
  Migration is mechanical; auth-surface coverage intact.

### 6.3 Static-analysis gates per wave

| Command | Gate |
|---|---|
| `uv run ruff check .` | Lint. Per AGENTS.md canonical list. |
| `uv run pyright` | **The load-bearing gate for F-4.** Every missed call-site migration surfaces as a type error. After all migrations, pyright is green. Recommendation: run `uv run pyright reference/packages/eden-wire/ reference/services/ conformance/` at the end of each wave (faster than full-repo pyright) and full-repo at the end of the chunk. |
| `python3 scripts/check-complexity.py` | Complexity gate. Must pass with NO new `# slop-allow:` annotations after the final wave. |
| `python3 scripts/check-rename-discipline.py` | Rename-discipline gate. F-4 introduces no new EDEN-domain identifiers — no rename risk. Defense in depth. |
| `npx --yes markdownlint-cli2@0.14.0 "**/*.md" "#node_modules" "#.venv" "#docs/archive/**" "#docs/plans/review/**"` | Markdown lint (this plan + docs PR). |

### 6.4 Compose smokes (final-wave only)

Compose smokes are network-heavy. Run at the chunk's end:

| Command | When |
|---|---|
| `bash reference/compose/healthcheck/smoke.sh` | Final wave (host-mode smoke). |
| `bash reference/compose/healthcheck/smoke-subprocess.sh` | Final wave. |
| `bash reference/compose/healthcheck/e2e.sh` | Final wave (Web UI ideator walkthrough; exercises ~every wire endpoint through the composed client). |

## 7. Tricky areas

### 7.1 Integrator's Store-typed parameter

[`reference/packages/eden-git/src/eden_git/integrator.py`](../../reference/packages/eden-git/src/eden_git/integrator.py)
takes `store: Store` and calls `store.read_variant`,
`store.read_idea`, `store.integrate_variant`, and
`store.validate_evaluation` (verified by grep; no `read_task`
calls in the integrator). After F-4:

- An in-process Store still has these flat methods → the integrator
  works against in-process backends unchanged.
- A `StoreClient` no longer has them → the integrator breaks for
  the wire-bound deployment (which is the default in
  `compose-smoke` and `compose-e2e`).

**Three resolutions** (operator picks at plan-merge or impl review):

1. **isinstance branch.** `if isinstance(store, StoreClient): store.variants.integrate(...)` else `store.integrate_variant(...)`. Simple but ugly; spreads the branching across every call.
2. **Local `IntegratorStore` Protocol.** Define a small protocol in `eden_git/_integrator_store.py` with just the 4 methods the integrator needs (`read_variant`, `read_idea`, `integrate_variant`, `validate_evaluation`, plus the `IndeterminateIntegration` raise discipline). The simplest implementation is a one-line wrapper class that the integrator's caller constructs around either a StoreClient or an in-process Store; that wrapper is the Integrator's contract. **Clean separation; F-4-local risk.** Recommended default.
3. **Bundle Store-protocol migration (Alternative B from §1.7).** Out of F-4 scope; tracked under #114.

**Plan recommendation: option 2 (local Protocol).** The
integrator already has a narrow needs-list of store methods; a
local Protocol there is the right boundary anyway. F-4 implements
this as part of the integrator's migration in the same PR.
Estimated additional surface: ~50 LOC for the Protocol +
~10 LOC adapter wrappers + retyping the integrator's
`__init__` parameter.

**eden-dispatch is different — much wider needs-list.** Grep
against `reference/packages/eden-dispatch/src/eden_dispatch/*.py`
(`grep -rEho "store\.[a-z_]+" eden_dispatch/*.py | sort -u`)
reveals **21 distinct `store.<method>` method calls** plus 1
property access (`store.experiment_id`), covering tasks, ideas,
variants, experiment lifecycle, and validation: `accept`,
`claim`, `create_evaluation_task`, `create_execution_task`,
`create_idea`, `create_ideation_task`, `create_variant`,
`emit_policy_error`, `list_ideas`, `list_tasks`, `list_variants`,
`mark_idea_ready`, `read_experiment`, `read_experiment_state`,
`read_idea`, `read_variant`, `reclaim`, `reject`, `submit`,
`terminate_experiment`, `validate_terminal`. No
`read_dispatch_mode` or `read_task` calls (round-2
verification: those names appear neither in `state_view.py` nor
in any other dispatch source file). The `DispatchStore` Protocol
declares exactly the 21 methods plus the `experiment_id`
property.

Two options for dispatch:

- **(A) Retype each function's `store: Store` parameter to a
  Union** `StoreClient | InMemoryStore | SqliteStore`. Each call
  site `isinstance`-branches once per method (or once per method
  group). This pushes concrete-backend knowledge through the
  dispatch library API — every public function in
  `eden_dispatch` would name three concrete backend types in
  its signature, and the narrowing has to recur at every
  `store.<method>` call site (21 such methods plus the `experiment_id` property, across 4 current caller files plus the new `_dispatch_store.py` bridge added by F-4).
- **(B) Keep `: Store` and provide a thicker
  `_StoreClientBridge` in `eden_dispatch/_dispatch_store.py`
  with all 21 methods (plus the `experiment_id` property).**
  This is the integrator-bridge pattern
  applied at a larger surface. It IS a delegation shim by every
  meaningful definition, but it lives in the consumer package
  (`eden_dispatch`), not on `StoreClient` itself. Same category
  as the integrator bridge (§7.1 lead) just scoped to dispatch's
  wider needs-list. The operator approved the integrator
  bridge by analogy in the surface-lock framing; dispatch is the
  same shape, just thicker.

**Plan recommendation (revised round-2): option B for
eden-dispatch.** The operator-locked anti-shim posture is
**about the public `StoreClient` surface**, not about any
consumer's local adapter. Option A spreads concrete-backend
unions across every public dispatch function signature — making
`eden_dispatch` callers aware of three storage backend types
they shouldn't need to know about, and forcing isinstance
narrowing at every call site (21+ method-call sites). Option B keeps
dispatch's library API clean (`store: DispatchStore`, a local
Protocol matching `Store`'s shape) and concentrates the
bridge-wiring in one file. Symmetry with the integrator is the
deciding factor: both are consumer-side adapters, both honor
the lock.

The cost of option B is one ~150-LOC `_StoreClientBridge` in
`eden_dispatch/_dispatch_store.py` with 21 thin methods (plus
the `experiment_id` property), each a 1-line passthrough to
`sc.<resource>.<method>(...)`. Maintenance cost: trivial; every
method maps to exactly one F-4 sub-client accessor. If a new
wire endpoint lands later, the bridge gets one more method.

If the operator disagrees and wants option A for dispatch
(e.g., "don't proliferate adapters even one-per-package"),
flag in the §10 open questions and adjust at plan-merge.

**Bridge shape (sketch).** Methods derived by grep against the
actual `Integrator.__init__(store=…)`-fed call sites
([`integrator.py:137-138`](../../reference/packages/eden-git/src/eden_git/integrator.py),
[`integrator.py:238`](../../reference/packages/eden-git/src/eden_git/integrator.py),
[`integrator.py:271`](../../reference/packages/eden-git/src/eden_git/integrator.py),
[`integrator.py:421`](../../reference/packages/eden-git/src/eden_git/integrator.py),
[`integrator.py:592`](../../reference/packages/eden-git/src/eden_git/integrator.py)):

```python
# eden_git/_integrator_store.py
class IntegratorStore(Protocol):
    def read_variant(self, variant_id: str) -> Variant: ...
    def read_idea(self, idea_id: str) -> Idea: ...
    def integrate_variant(self, variant_id: str, variant_commit_sha: str) -> None: ...
    def validate_evaluation(self, evaluation: dict[str, Any]) -> None: ...


class _StoreClientBridge:
    """Adapt a :class:`StoreClient` to :class:`IntegratorStore`."""
    def __init__(self, sc: StoreClient) -> None:
        self._sc = sc
    def read_variant(self, variant_id: str) -> Variant:
        return self._sc.variants.read(variant_id)
    def read_idea(self, idea_id: str) -> Idea:
        return self._sc.ideas.read(idea_id)
    def integrate_variant(self, variant_id: str, variant_commit_sha: str) -> None:
        return self._sc.variants.integrate(variant_id, variant_commit_sha)
    def validate_evaluation(self, evaluation: dict[str, Any]) -> None:
        return self._sc.reference.validate_evaluation(evaluation)
```

The four methods are exhaustive — every `self._store.<…>` call
in [`integrator.py`](../../reference/packages/eden-git/src/eden_git/integrator.py)
reduces to one of those four. The integrator does NOT call
`read_task` (that line of the round-0 sketch was wrong); verify by
`grep "self._store" reference/packages/eden-git/src/eden_git/integrator.py`
before impl.

The integrator's call site changes from `Integrator(store=sc)` to
`Integrator(store=_StoreClientBridge(sc))` for wire-bound
deployments. In-process backends are passed unchanged
(structurally satisfy IntegratorStore via their flat methods).

**This IS a delegation shim** — and the operator-lock at §1.5
disallows them on the *public StoreClient surface*. The bridge
is internal to consumers, not on the StoreClient itself; the
distinction is: the StoreClient is committed to composed-only,
and any consumer that wants a flat view declares its own narrower
contract and bridges explicitly. Operator review should confirm
this distinction is acceptable. If not, fallback to option 1
(isinstance branching, ~30+ branches across the package).

### 7.2 Validate-route split: TasksClient vs ReferenceClient

The three `/_reference/` routes (chapter-7 §15) split asymmetrically:

| Route | Server router (F-3) | Client sub-client (F-4 chosen) |
|---|---|---|
| `GET /_reference/.../tasks/{task_id}/validate-terminal` | `routers/reference.py` | `TasksClient.validate_terminal` |
| `POST /_reference/.../validate/evaluation` | `routers/reference.py` | `ReferenceClient.validate_evaluation` |
| `GET /_reference/.../artifacts/{path:path}` | `routers/reference.py` | *(no client method — artifacts are server-side observability only; no StoreClient method exists today)* |

The asymmetry is intentional: client-side ergonomics. A caller
doing `sc.tasks.validate_terminal(task_id)` reads better than
`sc.reference.validate_terminal_for_task(task_id)`; the
`task_id`-parameterized routes ergonomically belong with the
TasksClient. `validate_evaluation` takes a raw `dict`, not a
task or variant id — it's a shape-checker, not a state operation;
ReferenceClient is its home.

**Alternative (called out for operator review):** Put both
`validate_*` on ReferenceClient for strict symmetry with F-3's
`routers/reference.py`. The plan ships TasksClient by default per
ergonomics; if the operator prefers strict symmetry, swap. Either
way, the underlying HTTP path is unchanged.

### 7.3 `import_checkpoint` method name — Python keyword collision

The current `StoreClient.import_checkpoint(...)` becomes
`CheckpointsClient.import_(...)` because `import` is a Python
reserved word and `client.checkpoints.import(...)` is a syntax
error. The trailing underscore follows
[PEP-8 single-trailing-underscore convention](https://peps.python.org/pep-0008/#descriptive-naming-styles)
for avoiding keyword collisions.

**Alternatives considered:**

- `client.checkpoints.import_archive(stream, ...)` — explicit but
  asymmetric with `export(stream)`. Rejected — F-3 mapping table
  notes the server-side handler is `_import_checkpoint`; the
  bare verb is the route's natural name.
- `client.checkpoints.upload(stream, ...)` — different verb;
  loses the spec-level ↔ method-name alignment. Rejected — the
  spec calls this "import"; the wire endpoint is
  `POST /v0/checkpoints/import`; the method should match.
- Keep `client.checkpoints.import_checkpoint(stream, ...)` —
  works (no keyword collision because the leading method-name
  has no keyword). Verbose; loses the verb-only-on-sub-client
  pattern.

**Plan picks `import_`** — concise, Python-conventional, the
trailing underscore is well-understood. Call out in the wave's PR
description so reviewers don't trip on it.

### 7.4 `events()` / `replay()` aliases on EventsClient

The current `StoreClient.events()` and `.replay()` are aliases
for `.read_range(0)` — added to match the Store Protocol's two
historical names for the same operation (chapter 8 §2.1). Even
though the Protocol-conformance claim drops under F-4, the two
aliases preserve to avoid breaking the call sites that explicitly
use them. `EventsClient` exposes:

```python
class EventsClient:
    def list(self) -> list[Event]: return self.replay()
    def replay(self) -> list[Event]: return self.read_range()
    def read_range(self, cursor: int | None = None) -> list[Event]: ...
    def subscribe(self, cursor: int | None = None, *, timeout: float | None = None) -> list[Event]: ...
```

**Alternative**: drop the `events`/`replay` aliases and migrate
every caller to `read_range(0)`. F-4 is supposed to be a
"clean break" per §1.5, so dropping is defensible. The plan
**keeps** them because (a) the chapter-8 §2.1 spec uses both
"events" and "replay" names normatively and the client should
match the spec's vocabulary; (b) call-site count is non-trivial.
If the operator wants the deeper break, drop both during impl.

### 7.5 `update_experiment_state` NotImplementedError preserved

The current `StoreClient.update_experiment_state(new_state)`
raises `NotImplementedError` ("internal Store primitive; not
exposed as a wire endpoint per §8.3"). F-4 preserves this on
`ExperimentLifecycleClient` because (a) it's exercised by an
existing test (`test_lifecycle_wire.py` smoke or schema-parity)
and (b) callers may shadow-call it during transition / testing.
The method moves as-is, still raising NotImplementedError.

If the operator wants F-4 to drop this method outright (it's
unused on the wire and only exists for symmetry with the Store
Protocol that we're breaking from), F-4 can drop it. Tradeoff
called out; recommendation is to keep it (one line on a
sub-client, no maintenance cost) and let a future cleanup
decide.

### 7.6 `verify_worker_credential` deep-call shape

The current `StoreClient.verify_worker_credential(worker_id, token)`
does NOT use `self._request` — it builds its own request with a
swapped Authorization header (the §6.4 verify-credential probe).
This is intentional: the verify-call swaps the bearer per-call,
so it can't share the transport's headers dict.

Post-F-4, the method lives on `WorkersClient` and reaches into
the transport's `httpx.Client` directly:

```python
class WorkersClient:
    def verify_credential(self, worker_id: str, registration_token: str) -> bool:
        candidate_bearer = f"{worker_id}:{registration_token}"
        headers = {
            **self._transport.headers,
            "Authorization": f"Bearer {candidate_bearer}",
        }
        resp = self._transport.client.request(
            "GET", f"{self._transport.base}/whoami", headers=headers
        )
        # ... (current verify_worker_credential body verbatim)
```

The `self._transport.client.request(...)` direct access is the
single intentional case where a sub-client bypasses
`self._transport.request(...)` and goes straight to the underlying
httpx client. Same applies to `CheckpointsClient.export` (uses
`self._transport.client.stream(...)` for the streaming download
response).

**This means `ClientTransport.client` MUST be a public-from-
the-sub-client's-perspective attribute** (not name-mangled, not
under-underscored). The dataclass already has it as `client:
httpx.Client`. The plan formalizes that direct access is the
documented escape hatch for streaming + per-call header swaps.

### 7.7 Tests that inject custom `httpx.Client` instances

Round-1 correction: the round-0 plan claimed the tests monkeypatch
`StoreClient._request`. That was wrong. Verified by
`rg '_request|monkeypatch.setattr' reference/packages/eden-wire/tests/`:
no test in `eden-wire/tests/` mutates `_request` — every flaky-
transport / mock-transport test uses the well-supported pattern
of constructing an `httpx.Client(transport=httpx.MockTransport(...))`
and passing it to `StoreClient(..., client=flaky_http_client)`.
Example sites:

- [`test_auth.py:269`](../../reference/packages/eden-wire/tests/test_auth.py),
  `test_auth.py:282`, `test_auth.py:687`, `test_auth.py:714`,
  `test_auth.py:774` — every `_exploding_handler` / `_wrong_id_handler`
  posture.
- [`test_checkpoint_wire.py:612`](../../reference/packages/eden-wire/tests/test_checkpoint_wire.py),
  `test_checkpoint_wire.py:643` — the indeterminate-import probe
  fixtures.
- [`test_groups_wire.py:51`](../../reference/packages/eden-wire/tests/test_groups_wire.py)
  and the parallel proxy patterns in `test_workers_wire.py`,
  `test_lifecycle_wire.py`, `test_reassign_dispatch_wire.py`,
  `test_wire_roundtrip.py`.

**Implication for F-4: these tests need ZERO transport retargeting.**
The `client=...` constructor parameter still flows into
`ClientTransport.client` post-F-4 (see the `StoreClient.__init__`
sketch in §3.4); the test injects an httpx.Client that wraps a
MockTransport, the transport dataclass holds the reference, and
every sub-client uses it via `self._transport.client.request(...)`
internally. The only Wave-3 change in these tests is the
call-site migration (`sc.claim(...)` → `sc.tasks.claim(...)`); the
transport-injection pattern is invariant.

The two remaining test categories that DO need retargeting:

1. Any test that reaches into `sc._request` directly (verified
   round-1: **zero hits** across `eden-wire/tests/`, services,
   conformance). Spot-grep at impl time as defense in depth.
2. Any test that constructs a `_FlakyClient` subclassing
   `httpx.Client` and overriding `request` (e.g.
   [`test_reassign_dispatch_wire.py:663,688`](../../reference/packages/eden-wire/tests/test_reassign_dispatch_wire.py)).
   Same pattern — passes via `client=` parameter; no retargeting
   needed.

### 7.8 `_lineage.py`, `_submit_readback.py`, and other web-ui internals

[`reference/services/web-ui/src/eden_web_ui/routes/_lineage.py`](../../reference/services/web-ui/src/eden_web_ui/routes/_lineage.py)
and `_submit_readback.py` thread a `store` argument through a
sequence of helper functions; the type may be `: Store` or `:
StoreClient` depending on the route. Audit each and retype to
`: StoreClient` where the helper is wire-bound (most cases).
Where the helper is genuinely transport-agnostic (e.g. pure
read-and-format that works against an in-process test fixture),
keep `: Store` and ensure the test fixtures pass an in-process
Store directly.

## 8. Wave plan

Estimated chunk size — a single multi-wave PR per the operator's
"impl is mechanical once the seams are decided" framing in
issue #116. Internal waves:

### Wave 1 — Scaffolding (~0.5 day, single PR commit)

- Create `_client/__init__.py`, `_client/_transport.py`,
  `_client/_common.py`.
- Implement `ClientTransport` + the three methods (`request`,
  `assert_bearer_matches_worker_id`, `maybe_json`) byte-equivalent
  to the current implementations.
- Tests: zero new tests. The scaffolding is exercised by Wave 2's
  first sub-client + migrated wire-roundtrip tests.
- Gate: `uv run pyright reference/packages/eden-wire/` clean.

### Wave 2 — Per-resource sub-clients (~1 day)

- Implement all 11 sub-clients in 11 new `_client/*.py` modules.
  Each method body is the current `StoreClient.<method>` body
  with `self._request(...)` → `self._transport.request(...)`,
  `self._assert_bearer_matches_worker_id(...)` →
  `self._transport.assert_bearer_matches_worker_id(...)`, etc.
- Move the 5 `Indeterminate*` exception classes to their
  raising sub-client (per Decision 6).
- The new files coexist with the old monolithic `StoreClient` for
  the duration of this wave — i.e., `StoreClient` keeps every
  flat method AND gains the sub-client attributes. The slop-allow
  on client.py stays. Pyright should be clean across the whole
  repo at the end of this wave (nothing has migrated yet).
- Gate: full `uv run pytest -q` green; the new sub-clients are
  not yet exercised by tests (waves 3 + 4 do that).

### Wave 3 — Migrate all `StoreClient` direct callers to composed form (~1 day)

- Migrate every call site in `reference/services/`, `reference/packages/`,
  `conformance/`, and the existing `eden-wire/tests/` to the
  composed accessor path (`sc.foo(...)` → `sc.<resource>.<method>(...)`).
- For Store-Protocol-typed call sites (§5.3) that flow a
  StoreClient: retype to `: StoreClient` (default) or implement
  the §7.1 local-Protocol bridge (for integrator) / isinstance
  branching (for eden-dispatch).
- During this wave, the monolithic flat methods on StoreClient
  STILL EXIST (they were left in place by Wave 2). They just
  stop being called. **Pyright does NOT catch missed migrations
  during this wave** because the flat methods are still
  resolvable — `sc.claim(...)` continues to type-check green. The
  Wave-3 completeness gate is therefore a **grep audit**, not a
  type check:

  ```bash
  # Wave-3 audit gate — every match is a candidate for migration
  # to the composed accessor. Inspect each manually before
  # claiming Wave 3 complete. The regex matches the suffix
  # `.<method>(` against ANY object expression (no identifier
  # whitelist — round-1 review showed an allowlist of variable
  # names misses real direct-call sites like `seed.foo(...)` or
  # `verify.bar(...)` where the receiver is an ad-hoc fixture
  # variable). Expect MANY hits including in-process Store call
  # sites that are NOT StoreClient — those are noise to filter
  # manually, but the audit is then exhaustive across the
  # StoreClient direct-call surface.
  rg -n '\.(claim|submit|accept|reject|reclaim|create_task|create_idea|create_variant|read_task|read_idea|read_variant|read_submission|list_tasks|list_ideas|list_variants|events|replay|read_range|subscribe|reassign_task|validate_acceptance|validate_terminal|mark_idea_ready|declare_variant_evaluation_error|integrate_variant|validate_evaluation|register_worker|reissue_credential|verify_worker_credential|whoami|read_worker|list_workers|register_group|add_to_group|remove_from_group|delete_group|read_group|list_groups|resolve_worker_in_group|read_dispatch_mode|update_dispatch_mode|read_experiment|read_experiment_state|update_experiment_state|emit_policy_error|terminate_experiment|export_checkpoint|import_checkpoint|create_ideation_task|create_execution_task|create_evaluation_task)\(' reference conformance --glob '!**/_client/**'
  ```

  **Triage discipline.** Each hit must be classified as either
  (a) an in-process Store call (noise — leave alone), (b) a
  StoreClient call that's been migrated (correctly resolves to a
  `sc.<resource>.<method>(...)` form — recognizable by the
  preceding `.` chain), or (c) a StoreClient call that's still
  flat (migration target — fix). The triage is manual but the
  set is bounded (~hundreds, not thousands). Use type inference
  in your editor on each `(c)` hit to confirm — pyright resolves
  the receiver's type and can be queried at-point.

- Gate: full `uv run pytest -q` green + Wave-3 grep audit returns
  zero unmigrated hits in non-`_client/` source paths. `pyright`
  also green (a dead flat-method block is valid Python, but any
  brand-new typing error introduced by Section-A retypes
  surfaces here).

### Wave 4 — Remove flat methods + drop Protocol conformance + drop slop-allow (~0.25 day)

**This is the wave that turns pyright into the structural-conformance
backstop.** Removing the flat methods is what makes pyright start
reporting "Cannot access member `claim` for type `StoreClient`" on
any sites Wave 3's grep audit missed. The pyright pass at the end
of Wave 4 is the final completeness check.

- Replace `StoreClient`'s body with the slim assembler from §3.4
  (init + lifecycle only).
- Remove the `# slop-allow-file:` annotation at line 1 of
  client.py.
- Update the module docstring (drops the Store-Protocol claim).
- Add the regression tests from §6.2 (4 new tests in
  `test_wire_roundtrip.py`).
- Update `docs/audits/2026-05-20-phase-c-disposition.md` §F-4
  entry to "resolved YYYY-MM-DD".
- Update `CHANGELOG.md` + `docs/roadmap.md` per AGENTS.md
  "Recording chunk completions".
- Gate: full `uv run pytest -q` + `uv run pyright` +
  `python3 scripts/check-complexity.py` green;
  `bash reference/compose/healthcheck/smoke.sh` +
  `smoke-subprocess.sh` + `e2e.sh` green.

### Wave 5 — Plan PR + impl PR submission (this plan)

- Plan PR (this document) → codex-review → operator approve.
- Impl PR drafted post-approval; codex-review-to-convergence
  (2-4 rounds expected); merge.
- Post-merge: roadmap one-line flip + chunk-completion CHANGELOG
  consolidation (the planless-chunk shape per AGENTS.md, with the
  roadmap pointing at the merged PR ref since there's no phase
  number).

**Total chunk effort: ~2.5–3 days of focused work (Waves 1–4
implementation) + plan + codex-review cycles**. This is meaningfully
larger than the audit's 0.5-day estimate; the delta is the
~800-call-site migration in Wave 3, which the audit's
disposition did not anticipate (it assumed Alternative A — delegation
shim, zero caller migration). The operator's surface lock at §1.5
multiplies the cost.

## 9. Risks

1. **Pyright doesn't catch a call site (`Any`-typed StoreClient).**
   A few callers may have stored a StoreClient in an `Any`-typed
   variable (e.g. dict value in a config-driven service registry).
   Pyright won't surface those; runtime AttributeError at first call.
   **Mitigation**: at impl review, grep for `Any.*StoreClient` and
   inspect each. The codex-review impl pass also catches this kind
   of slip.
2. **Store-Protocol break catches a transport-agnostic call site
   the plan didn't enumerate.** Some helper deep inside the
   web-ui or dispatch packages takes `: Store` and may receive a
   StoreClient at runtime in a code path not exercised by
   pytest. **Mitigation**: §5.3 enumeration is from
   `: Store`-typed-parameter grep; codex-review will independently
   audit. The Compose smokes exercise the wire-bound paths
   end-to-end and surface any runtime AttributeError before
   merge.
3. **`_FlatStoreView`-style escape-hatch proliferation.** §7.1
   recommends a local `IntegratorStore` Protocol + bridge for
   the integrator. If the impl pass finds 5+ more places where
   such a bridge is needed, the "no delegation shim" lock is
   effectively eroded by death-of-a-thousand-cuts. **Mitigation**:
   cap the bridges at the two packages identified in §7.1
   (eden-git, eden-dispatch); any additional case triggers
   operator review.
4. **Tests that monkeypatch `_request` need careful retargeting.**
   §7.7 enumerates them; missing one would cause a confusing
   `AttributeError: 'StoreClient' object has no attribute '_request'`
   at test time (visible, but not at typecheck time because of
   monkeypatch's runtime-only nature). **Mitigation**: §6.2 +
   §7.7 audit list run during Wave 3; Wave 4's full pytest pass
   catches every remaining one.
5. **Sibling F-3 (#115) lands first OR last AND changes the
   resource boundaries.** F-3 is plan-stage in parallel; its
   final design may merge with adjustments to the router shape
   (e.g., `experiment_lifecycle` + `experiment_read` boundary
   tweaks). If F-3 lands first, F-4 follows its final shape; if
   F-4 lands first, F-3 follows F-4's. The §4 mapping is
   verified against F-3's plan at HEAD `f3f7932`; revisit if
   F-3 amends. **Mitigation**: the F-4 plan PR cites
   `origin/plan/issue-115-server-router-regroup` as its
   reference; the impl PR rebases on whichever side lands
   first.
6. **Conformance reference adapter migration is the longest
   single test-file change.** ~10 sites but each is in a
   sensitive test path. **Mitigation**: explicit codex review
   on the `conformance/src/conformance/adapters/reference/`
   diff; the conformance suite as a whole is the strongest
   acceptance signal so any regression surfaces.

## 10. Open questions for operator review

1. **§1.7 alternative confirmation.** The operator's lock at
   §1.5 is "composed public, no delegation shim, clean break."
   The plan codifies that as Option C (§1.7) which breaks the
   Store-Protocol-conformance claim of StoreClient. **Is the
   plan reading this correctly, OR did the operator intend
   Option A (composed internal, flat public) and the prompt's
   wording was hyperbolic?** If Option A, the plan shrinks
   dramatically (~0.5 day as the audit estimated) and the
   ~800-call-site migration vanishes. If Option C as drafted,
   the plan stands.
2. **§7.1 integrator bridge.** Operator approves the local
   `IntegratorStore` Protocol + `_StoreClientBridge` shape, OR
   prefers isinstance branching, OR prefers bundling with
   #114? The plan recommends the local Protocol; flag to
   operator review.
3. **§7.2 validate-route placement.** `TasksClient.validate_terminal`
   alongside `ReferenceClient.validate_evaluation` (ergonomic split)
   OR strict server-symmetry on ReferenceClient for both? Plan
   recommends ergonomic split.
4. **§7.4 `events`/`replay` aliases.** Keep for spec-vocabulary
   alignment, OR drop for clean-break? Plan recommends keep.
5. **§7.5 `update_experiment_state` stub.** Keep the
   NotImplementedError method as a documentation pin, OR drop
   it since the wire never exposed it? Plan recommends keep.

Operator should resolve §10.1 first; #2–#5 are smaller and can
be resolved either at plan-merge or during impl review without
re-planning.
