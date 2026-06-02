# Issue #183 — Cross-mode CI smoke matrix (scripted × subprocess × docker-exec × …)

**Status.** Draft (plan).

**Issue.** [eden#183](https://github.com/ealt/eden/issues/183) — filed 2026-05-23 as a process-improvement item from the EDEN demo-session debrief: *"cross-mode interactions are a class of latent bug nobody has exercised."* Today's Compose smokes each cover one mode; no single job hits the cross-products where cross-cutting bugs hide.

**Predecessors.** This plan is **gated on [#147](https://github.com/ealt/eden/issues/147) landing first** (both its plan — already committed at [`docs/plans/issue-147-compose-smoke-multi-experiment.md`](issue-147-compose-smoke-multi-experiment.md) — and its impl). #147 delivers the entire multi-experiment substrate that every cell in this matrix builds on: the `control-plane` service as a first-class Compose container, `compose.multi-experiment.yaml`, `setup-experiment --register-additional-experiment`, and a lease-handoff chaos drill. #183 does **not** re-derive any of that; it composes #147's overlays with the existing exec-mode overlays to fill the missing matrix cells. See §11 for the coordination note on the shared smoke library.

**Naming.** Pre-draft check against [`docs/glossary.md`](../glossary.md) and AGENTS.md "Naming discipline": this plan introduces **no new role / verb / task-kind / submission / artifact identifiers**. "scripted", "subprocess", "docker-exec" (exec modes), "multi-experiment", "control-plane", "lease", "auth-disabled" are all established vocabulary. "matrix" is a documentation-level term, not an identifier. The naming map (§3) is therefore empty of renames; the only new names are smoke-script / CI-job names, which follow the established `smoke-<cell>.sh` / `compose-smoke-<cell>` convention.

## 1. Context

### 1.1 What the matrix looks like today

EDEN's deployment shape is the cross-product of several orthogonal axes. The Compose smokes that exist today (on `main`, plus #147 pending) cover these cells:

| Smoke / job | Exec mode | Experiments | Control-plane | Auth | Extra |
|---|---|---|---|---|---|
| `compose-smoke` | scripted | single | absent | on | — |
| `compose-smoke-subprocess` | subprocess | single | absent | on | — |
| `compose-smoke-subprocess-docker` | docker-exec | single | absent | on | DooD sibling containers |
| `compose-smoke-manual-mode` | scripted | single | absent | on | manual-UI drill |
| `compose-smoke-multi-orchestrator` | scripted | single | absent | on | 2 orchestrator replicas, §6.4 group safety |
| `compose-smoke-checkpoint` | scripted | single | absent | on | export/import round-trip |
| `compose-smoke-logging` | subprocess | single | absent | on | Loki/Alloy/Grafana overlay |
| `compose-e2e` | scripted | single | absent | on | UI walkthrough + admin reclaim |
| **`compose-smoke-multi-experiment` (#147, pending)** | **scripted** | **multi** | **present** | **on** | **lease-handoff chaos drill** |

Two observations that shape this plan:

1. **Every existing smoke runs auth-on.** The Compose stack hard-requires `--admin-token ${EDEN_ADMIN_TOKEN:?}` on every service ([`compose.yaml`](../../reference/compose/compose.yaml) lines 180, 254, 320, 356, 398, 437). There is no auth-disabled Compose deployment today. (§2 Decision 4 examines whether there should be.)
2. **Control-plane / multi-experiment is exercised by exactly one cell** — and only in scripted mode (#147). The "real deployment we'd actually run" — subprocess-mode worker hosts under multi-tenant control-plane orchestration — is **untested end-to-end**.

### 1.2 The axes, and which cross-products are load-bearing

The issue's axes:

- **Exec mode** ∈ {scripted, subprocess, docker-exec} — how the worker hosts run the user's `*_command` (in-process reference scripts vs. a long-running subprocess vs. a DooD sibling container).
- **Experiment topology** ∈ {single, multi-experiment + control-plane} — one registered experiment vs. ≥2 under a deployment-level lease registry.
- **Auth** ∈ {on, off}.

Full cross-product = `3 × 2 × 2 = 12` jobs; the issue (correctly) rejects that as CI-cost-prohibitive and asks for the **pragmatic pairwise cells that match real deployment shapes**. After #147 lands `scripted × multi`, the load-bearing gaps are:

| Gap cell | Why it's load-bearing | Disposition in this plan |
|---|---|---|
| **subprocess × multi-experiment** | The production-like multi-tenant shape: real subprocess worker hosts (the user's `ideation.py`/`execution.py`/`evaluation.py`) driven by a multi-experiment lease-holding orchestrator. The shape an operator would actually deploy. | **Cell 1 — primary** (§3.2) |
| **docker-exec × multi-experiment** | DooD's `--exec-volume`/`--exec-bind` plumbing + the per-experiment `_2`-suffixed volumes from #147 intersect here. AGENTS.md's DooD `name:` volume-resolution trap is *exactly* the kind of bug that only fires at this intersection. | **Cell 2 — secondary** (§3.3) |
| **explicit control-plane / lease primitive** (issue job #3) | The control-plane is "incidental" to most smokes today. | **Folded** — every multi cell uses the control-plane, and #147's lease-handoff chaos drill is the dedicated lease exercise. No separate `compose-smoke-control-plane` job (§2 Decision 3). |
| **auth-disabled** (issue job #4) | Originally: catch divergence between the conformance harness's auth-disabled mode and production auth-on mode. | **Re-scoped / open question** — the premise is now stale; see §2 Decision 4. |

### 1.3 Why the auth-disabled premise is now stale

The issue (2026-05-23) justifies `compose-smoke-auth-disabled` two ways, both of which have since changed:

1. *"The conformance harness uses auth-disabled mode but no other smoke does … catches divergence between the harness's mode and the production mode."* — **No longer true.** [#148](https://github.com/ealt/eden/issues/148) (CLOSED) retired the `X-Eden-Worker-Id` test-fixture header and migrated the conformance reference adapter to run the IUT with a per-test `--admin-token` (auth-**on**, per-worker bearers). The adapter docstring at [`conformance/src/conformance/adapters/reference/adapter.py`](../../conformance/src/conformance/adapters/reference/adapter.py) now reads: *"Runs the server with a per-test `--admin-token` so the §13 normative auth posture is active … (the pre-migration auth-disabled posture this replaced)."* The harness and production no longer diverge on auth.
2. *"Eligibility flows (#137, #143, #144) interact with auth."* — They interact with **per-worker identity**, which auth-disabled mode **cannot represent**. `worker_id_from_request` collapses *every* caller onto the sentinel `"anonymous"` when auth is disabled ([`reference/packages/eden-wire/src/eden_wire/_dependencies.py`](../../reference/packages/eden-wire/src/eden_wire/_dependencies.py) line 196: *"Auth disabled — collapse all callers onto the sentinel."*). A multi-worker eligibility experiment run auth-disabled would exercise a **degenerate single-identity shape**, not the production multi-worker shape the issue wants coverage for.

Auth-disabled is documented as *"convenient for unit tests but NOT [production]"* ([`reference/packages/eden-wire/src/eden_wire/server.py`](../../reference/packages/eden-wire/src/eden_wire/server.py) line 97). The open question (§2 Decision 4) is whether auth-disabled is a supported **deployment** posture at all — if it's purely a test/in-process convenience, an end-to-end Compose smoke for it tests a shape we'd never ship.

## 2. Decisions

These are the load-bearing calls; §3 unpacks the in-scope ones.

1. **#147 is a hard predecessor; #183 builds on its substrate, does not re-derive it.** The multi-experiment Compose service topology, the `compose.multi-experiment.yaml` overlay, the `setup-experiment --register-additional-experiment` ergonomics, and the lease chaos drill all ship with #147. #183 adds exec-mode overlays on top. If #147 has not merged when #183 starts, #183 blocks. (Alternative — subsume #147 into #183 as wave 0 — rejected: #147 is independently valuable, already has a committed plan, and the issue itself names #147 as "lands first.")

2. **The matrix grows via a shared sourced shell library, not copy-pasted per-cell scripts.** The new multi-experiment cells (scripted/subprocess/docker × multi) are ~90% identical: same two-experiment provisioning, same bring-up, same cross-experiment isolation assertions, same lease drill, same cleanup. Three hand-written ~15 KB scripts would be exactly the cross-file duplication the [code-quality audit](../audits/2026-05-20-code-quality-audit.md) flagged (5 near-identical duplicate blocks). Instead: a sourced, non-executable library [`reference/compose/healthcheck/lib/multi-experiment-common.sh`](../../reference/compose/healthcheck/lib/) holds the common flow; each cell is a thin wrapper that `source`s it and selects its overlay set.

   - **This respects the issue's out-of-scope boundary.** The issue scopes out *"refactoring CI to share substrate setup across [the existing] smokes."* The shared library covers only the **new multi-experiment family** (born together); the existing 8 single-experiment smokes are untouched. Sharing new code from birth is not refactoring existing code.
   - **Coordination with #147** (§11): #147's `smoke-multi-experiment.sh` is the first member of this family. Preferred shape is that #147 lands the common flow already factored into the library and `smoke-multi-experiment.sh` as the first thin wrapper. If #147 lands it as a monolith, #183's wave 1 extracts the library and refactors `smoke-multi-experiment.sh` to source it (still in-bounds — the multi-experiment family is new, not one of the protected existing 8).
   - **Alternative — one parametrized mega-driver** (`smoke-matrix.sh --overlays … --profile …`): rejected as harder to read at the call site and harder to debug a single cell; a sourced library keeps each cell's CI invocation a one-liner and each wrapper self-documenting.

3. **No standalone `compose-smoke-control-plane` job.** The issue's job #3 ("explicit control-plane up + lease primitive exercise") is delivered by #147's lease-handoff chaos drill plus the fact that *every* cell in this matrix runs the control-plane (multi-experiment ⇒ control-plane present). A control-plane-only job would duplicate #147's drill and the chapter-11 conformance scenarios. The lease primitive is exercised in each new multi cell's bring-up assertions (2 leases held by the orchestrator) and re-exercised by the chaos drill the library carries.

4. **Auth-disabled cell: defer pending a deployment-posture decision; recommend a minimal startup-only smoke or dropping it — NOT the full multi-worker smoke.** Per §1.3, the originally-proposed full auth-disabled smoke would (a) duplicate no real production shape and (b) run a degenerate single-identity topology. Two viable dispositions, surfaced for operator/codex selection (§8 risk 5):
   - **Drop entirely.** Auth-disabled stays covered where it already is — unit + wire + in-process tests. The matrix is auth-on-only. Document the rationale. *(Recommended if auth-disabled is purely a test convenience.)*
   - **Minimal startup-only smoke.** Add a thin `compose-smoke-auth-disabled` that asserts only: the Compose stack boots with `--admin-token` omitted across services, and a single anonymous worker drives one experiment to quiescence. Explicitly **not** a multi-worker / eligibility / multi-experiment exercise. Catches the "does Compose even boot without auth" regression class. Requires a small impl cost (make `--admin-token` conditional in `compose.yaml` + an `EDEN_AUTH_DISABLED` env knob; §3.4). *(Recommended only if auth-disabled is a supported deployment posture.)*

   This plan carries the minimal-startup-only design in §3.4 as the larger of the two so the scope is visible, and marks the cell **lowest priority / independently droppable** (wave 3). The full auth-disabled smoke is **not** in scope under any disposition.

5. **CI jobs follow the not-required-then-bump posture.** Same as `compose-smoke-checkpoint` / `compose-smoke-multi-orchestrator`: each new job is added unrequired, path-gated on the `compose` bucket ([`.github/workflows/ci.yml`](../../.github/workflows/ci.yml) `changes` filter), and bumped to required-status ~2 weeks after staying clean on `main`. Documented in the impl PR.

6. **Caps and assertion floors match the existing smokes' "smoke not load-test" posture.** `EDEN_IDEATION_POLICY_MAX_TOTAL=2` per experiment; reference scripted/subprocess workers (never the LLM ones); per-experiment floors of `≥2 variant.integrated`, `≥6 task.completed`. Cross-experiment isolation (disjoint task/variant/idea ids) is the load-bearing assertion, inherited verbatim from #147's library.

## 3. Design

### 3.1 The shared multi-experiment smoke library

[`reference/compose/healthcheck/lib/multi-experiment-common.sh`](../../reference/compose/healthcheck/lib/) — sourced, not executable. Holds the flow #147 establishes, parameterized by an `OVERLAYS` array the caller sets before sourcing:

```bash
# Caller contract (set before `source lib/multi-experiment-common.sh`):
#   OVERLAYS=(-f compose.yaml -f compose.multi-experiment.yaml ...)
#   SMOKE_NAME="subprocess-multi-experiment"   # for diagnostics
#   SETUP_EXTRA_ARGS=(--exec-mode subprocess)  # passed to setup-experiment
# Functions the library exposes (each bash-3.2 safe — no mapfile / declare -A):
#   me_preflight                 # docker compose v2 + jq + curl + python3
#   me_cleanup_volumes           # the eden- volume wipe (AGENTS.md smoke-cleanup discipline)
#   me_provision_two_experiments # setup-experiment exp-A + --register-additional-experiment exp-B
#   me_bring_up                  # docker compose "${OVERLAYS[@]}" up -d --wait
#   me_assert_two_leases         # control-plane /v0/control/leases shows 2 held by orchestrator
#   me_wait_quiescence           # orchestrator exits 0 (both experiments drained)
#   me_assert_cross_experiment_isolation  # disjoint task/variant/idea ids; per-experiment floors
#   me_lease_handoff_drill       # kill lease holder; second replica acquires both leases
#   me_assert_terminal_state     # both experiments last_known_state == terminated
```

Each cell wrapper is then ~20 lines: set `OVERLAYS` + `SETUP_EXTRA_ARGS`, source the library, call the functions in order, register the `cleanup` trap. The bash-3.2 discipline (AGENTS.md "Adding a new CI job") is enforced in the library once, not re-litigated per cell.

### 3.2 Cell 1 (primary) — `smoke-subprocess-multi-experiment.sh`

```bash
OVERLAYS=(-f compose.yaml -f compose.subprocess.yaml -f compose.multi-experiment.yaml)
SETUP_EXTRA_ARGS=(--exec-mode subprocess)
SMOKE_NAME="subprocess-multi-experiment"
source "$(dirname "$0")/lib/multi-experiment-common.sh"
me_preflight; me_cleanup_volumes
me_provision_two_experiments        # both experiments use the fixture ideation.py/execution.py/evaluation.py
me_bring_up
me_assert_two_leases
me_wait_quiescence
me_assert_cross_experiment_isolation
me_lease_handoff_drill
me_assert_terminal_state
echo "PASS: subprocess × multi-experiment"
```

What this exercises that no existing cell does: real long-running subprocess worker hosts (the fixture's `ideation.py` etc., per [`compose.subprocess.yaml`](../../reference/compose/compose.subprocess.yaml)) under a multi-experiment lease-holding orchestrator. The subprocess JSON-line protocol's `attach_stdin` posture, the per-experiment `_2`-suffixed substrate paths, and the lease loop all run together for the first time. **The "real deployment under multi-tenancy" shape.**

### 3.3 Cell 2 (secondary) — `smoke-docker-multi-experiment.sh`

```bash
OVERLAYS=(-f compose.yaml -f compose.subprocess.yaml -f compose.docker-exec.yaml -f compose.multi-experiment.yaml)
SETUP_EXTRA_ARGS=(--exec-mode docker)
SMOKE_NAME="docker-multi-experiment"
source "$(dirname "$0")/lib/multi-experiment-common.sh"
# ... same call sequence, plus:
me_assert_no_orphan_exec_containers   # reuse smoke-subprocess-docker.sh's orphan-reaping assertion
```

What this exercises: DooD sibling-container spawns (`--exec-mode docker`) for **two** experiments simultaneously. The load-bearing intersection (and the most likely real-bug-or-flake source) is the AGENTS.md DooD `name:` volume-resolution trap crossed with #147's per-experiment `_2`-suffixed volumes: every forwarded volume for exp-B's worker hosts must carry an explicit `name:` matching the wrap's literal, or the DooD spawn resolves to a fresh empty volume. The cidfile-dir bind-at-same-path discipline and the spawned-child uid pinning (AGENTS.md DooD traps) also apply per-experiment. This cell is where those break if exp-B's substrate naming was done wrong in #147 — **exactly the cross-mode latent-bug class the issue is about.**

### 3.4 Cell 3 (lowest priority, droppable) — `smoke-auth-disabled.sh` (minimal startup-only)

**Carried only if Decision 4 selects the minimal-startup posture.** Requires a small impl change to make auth optional in Compose:

- `compose.yaml`: thread `--admin-token` conditionally. Reuse #147's entrypoint-wrapper mechanism (§3.1.3 of [#147's plan](issue-147-compose-smoke-multi-experiment.md)) — the wrapper omits `--admin-token` when `EDEN_AUTH_DISABLED=1`. When unset (default), behavior is unchanged. This is the cleanest shape because #147 already introduces the wrapper; #183 extends it rather than adding a new mechanism.
- `setup-experiment.sh`: an `--auth-disabled` flag that sets `EDEN_AUTH_DISABLED=1` in `.env` and skips admin-token generation.

The smoke itself (scripted × single × auth-off):

```bash
OVERLAYS=(-f compose.yaml)   # auth-disabled is env-gated, not a separate overlay
# setup-experiment ... --auth-disabled
# bring up; assert:
#   - task-store-server boots with NO auth middleware (a /v0/ call with no Authorization header succeeds)
#   - the single anonymous worker drives one experiment to >=2 variant.integrated
#   - quiescence reached
echo "PASS: auth-disabled startup"
```

Explicitly **not** asserted: multi-worker identity, eligibility, multi-experiment. Those are structurally impossible under the `"anonymous"` sentinel (§1.3) and belong to auth-on cells.

### 3.5 CI jobs

Three jobs (or two if Decision 4 drops auth-disabled), each mirroring the `compose-smoke-checkpoint` block ([`.github/workflows/ci.yml`](../../.github/workflows/ci.yml)): `runs-on: ubuntu-latest`, `needs: changes`, `if:` path-gated on the `compose` bucket, `timeout-minutes: 25` (the multi cells run ~11–13 containers; bump from the 20-minute multi-experiment timeout to absorb the extra exec-mode containers), one step that runs the cell script. Not required by branch protection initially (Decision 5).

| Job | Script | Timeout |
|---|---|---|
| `compose-smoke-subprocess-multi-experiment` | `smoke-subprocess-multi-experiment.sh` | 25 min |
| `compose-smoke-docker-multi-experiment` | `smoke-docker-multi-experiment.sh` | 25 min |
| `compose-smoke-auth-disabled` *(conditional on Decision 4)* | `smoke-auth-disabled.sh` | 15 min |

### 3.6 Documentation

- **The matrix coverage table is the durable artifact.** [`docs/operations/ci-smoke-matrix.md`](../operations/) (new) — the canonical "which deployment shapes are CI-covered" doc the issue asks for. Holds the §1.1 table (kept current as cells are added), the axis definitions, and the policy: *new exec modes / topologies get a matrix row; the pairwise-not-full-cross-product rule and its rationale.* Cross-linked from [`docs/operations/README.md`](../operations/README.md) and [`docs/operations/multi-orchestrator.md`](../operations/multi-orchestrator.md).
- AGENTS.md "Commands" table: new rows for each new smoke script.
- [`reference/compose/README.md`](../../reference/compose/README.md): note the matrix cells under the existing smoke documentation.
- `CHANGELOG.md [Unreleased]` + `docs/roadmap.md` planless-chunk status flip on impl-merge (§4).

## 4. Scope

### 4.1 In scope

**Spec / contracts:** **None.** These are substrate-level smokes over already-shipped behavior (chapter 11 multi-experiment + chapters 3/4/5 role flows). No spec prose, JSON Schema, Pydantic model, or wire-binding changes. (Same posture as #147 and #152.)

**Code (reference impl):**

- *(Cell 3 only, conditional)* Extend #147's entrypoint wrapper to omit `--admin-token` under `EDEN_AUTH_DISABLED=1`; `setup-experiment --auth-disabled` flag. No other impl changes — Cells 1 & 2 are pure overlay composition.

**Smoke library + cells:**

- New `reference/compose/healthcheck/lib/multi-experiment-common.sh` (sourced library; §3.1). Coordinated with #147 (§11).
- New `smoke-subprocess-multi-experiment.sh` (§3.2).
- New `smoke-docker-multi-experiment.sh` (§3.3).
- *(Conditional)* New `smoke-auth-disabled.sh` (§3.4).

**CI:**

- 2–3 new jobs in [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml) (§3.5). Not branch-protected initially.

**Docs:**

- New [`docs/operations/ci-smoke-matrix.md`](../operations/) (the coverage table + policy; §3.6).
- AGENTS.md "Commands" rows; `reference/compose/README.md` note.
- `CHANGELOG.md [Unreleased]`; `docs/roadmap.md` planless-chunk flip on merge.

### 4.2 Out of scope (file as issues if pursued)

- **Full `3 × 2 × 2` cross-product.** Per the issue: pairwise pragmatic shape only.
- **Standalone `compose-smoke-control-plane` job** (issue job #3). Folded into the multi cells + #147's lease drill (Decision 3).
- **Full multi-worker auth-disabled smoke** (issue job #4 at original scope). Structurally degenerate (§1.3); at most the minimal startup-only variant (Decision 4).
- **Refactoring the existing 8 single-experiment smokes to share substrate setup.** The issue's explicit out-of-scope item; the shared library covers only the new multi-experiment family (Decision 2). A future "unify all smoke substrate setup" refactor is its own issue — **file it** when the library proves its shape across the new cells.
- **N-experiment generalization beyond N=2.** Inherited from #147's `_2`-suffix bound.
- **Stress / load testing.** Per the issue: correctness coverage, not load.
- **Checkpoint × any-mode cells** ([#152](https://github.com/ealt/eden/issues/152) complement). The checkpoint smoke is orthogonal; combining it with multi/subprocess/docker is a separate matrix expansion — file if operator demand surfaces.

### 4.3 Non-goals

- Changing any role's lease/decision/eligibility behavior. The cells **observe**; all behavior shipped with chapter 11 / earlier phases.
- Replacing or modifying the existing smokes. This adds cells; it doesn't touch the existing 8.

## 5. Files to touch

| File | Change |
|---|---|
| `reference/compose/healthcheck/lib/multi-experiment-common.sh` (new) | Sourced library holding the multi-experiment smoke flow (§3.1). Coordinated with #147 (§11). |
| `reference/compose/healthcheck/smoke-subprocess-multi-experiment.sh` (new) | Cell 1 wrapper (§3.2). |
| `reference/compose/healthcheck/smoke-docker-multi-experiment.sh` (new) | Cell 2 wrapper (§3.3). |
| `reference/compose/healthcheck/smoke-auth-disabled.sh` (new, conditional) | Cell 3 minimal startup smoke (§3.4). |
| `reference/compose/healthcheck/smoke-multi-experiment.sh` (#147's, refactor) | *(If #147 landed it as a monolith)* refactor to source the library (§3.1, §11). |
| [`reference/compose/compose.yaml`](../../reference/compose/compose.yaml) (conditional) | *(Cell 3 only)* entrypoint wrapper omits `--admin-token` under `EDEN_AUTH_DISABLED=1`. |
| [`reference/scripts/setup-experiment/setup-experiment.sh`](../../reference/scripts/setup-experiment/setup-experiment.sh) (conditional) | *(Cell 3 only)* `--auth-disabled` flag. |
| [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml) | 2–3 new `compose-smoke-*` jobs (§3.5). |
| `docs/operations/ci-smoke-matrix.md` (new) | The coverage-matrix doc + policy (§3.6). |
| [`docs/operations/README.md`](../operations/README.md) | Link the new matrix doc. |
| [`AGENTS.md`](../../AGENTS.md) | New "Commands" rows. |
| [`reference/compose/README.md`](../../reference/compose/README.md) | Note the matrix cells. |
| [`CHANGELOG.md`](../../CHANGELOG.md) | `[Unreleased]` entry on impl-merge (closes #183). |
| [`docs/roadmap.md`](../../docs/roadmap.md) | Planless-chunk status flip on impl-merge. |

## 6. Conformance impact

**None at the chapter-7 binding level.** Identical posture to #147: these are substrate smokes, not IUT-contract tests. Chapter 9 §6 binds the conformance harness to a single IUT over its HTTP surface; the cross-mode interactions these cells exercise (subprocess host lifecycle × lease loop; DooD sibling spawns × per-experiment volumes) are **off-wire** — the harness can't drive them, which is precisely why they need a Compose smoke. No conformance scenario files, no `check_citations.py` changes, no `CONFORMANCE_GROUP` declarations.

The cells' assertions mirror the same chapter-11 contracts #147's plan maps to the 9 documented multi-experiment conformance skips (lease decision gating, multi-experiment dispatch disjointness, state-sync hand-off). #183 adds the exec-mode dimension to that substrate-level coverage; the mapping is documentation-only.

## 7. Chunked execution plan

Gated on #147 impl having merged (Decision 1). Then one impl PR with three sequential waves (a reviewer can isolate failures per wave):

**Wave 1 — Shared library + Cell 1 (subprocess × multi).** *(Covers Decisions 1, 2, 6 + §3.1, §3.2.)*

- Extract / create `lib/multi-experiment-common.sh` (coordinated with #147's `smoke-multi-experiment.sh`; refactor it to source the library if it landed monolithic).
- New `smoke-subprocess-multi-experiment.sh`.
- New `compose-smoke-subprocess-multi-experiment` CI job (unrequired).
- **Validation gate:** the cell passes in CI; #147's `compose-smoke-multi-experiment` still passes (regression — the library extraction must not change its behavior); all existing 8 smokes unaffected. Local-repro: run the cell on macOS bash 3.2 cold-start + leftover-data-root (AGENTS.md local-repro discipline).

**Wave 2 — Cell 2 (docker-exec × multi).** *(Covers §3.3.)*

- New `smoke-docker-multi-experiment.sh` (sources the library + the orphan-container assertion from `smoke-subprocess-docker.sh`).
- New `compose-smoke-docker-multi-experiment` CI job (unrequired).
- **Validation gate:** the cell passes; specifically asserts no orphan exec containers post-quiescence per experiment, and the per-experiment `_2`-suffixed DooD volumes resolve correctly (the `name:` trap). This is the wave most likely to surface a real #147-substrate bug — if it does, that bug is filed and fixed (root-cause, not worked around).

**Wave 3 — Cell 3 (auth-disabled) + docs.** *(Covers Decisions 4, 5 + §3.4, §3.6.)*

- *(Conditional on Decision 4)* auth-optional Compose wiring + `setup-experiment --auth-disabled` + `smoke-auth-disabled.sh` + CI job.
- New `docs/operations/ci-smoke-matrix.md` (with the current cells filled in).
- AGENTS.md / README rows; `CHANGELOG.md [Unreleased]`; `docs/roadmap.md` flip.
- **Validation gate:** the full pre-push command quartet from AGENTS.md "Commands" (lint / typecheck / pytest / the relevant smokes) passes; markdownlint clean; the new cells + existing smokes all green.

Wave 3's auth-disabled portion is independently droppable (Decision 4) without affecting waves 1–2.

## 8. Risks

1. **GitHub Actions runner resource pressure.** Cell 1 (subprocess × multi) runs ~11–13 containers (2 experiments × 3 worker hosts + control-plane + orchestrator + postgres + forgejo + task-store-server + web-ui); Cell 2 adds DooD sibling containers on top. Memory/CPU pressure is the most likely flake source. **Mitigation:** `MAX_TOTAL=2` per experiment (Decision 6), reference (not LLM) workers, 25-minute timeout. If still flaky, split the lease chaos drill into a separate job (base cell goes required first; chaos drill is a follow-on job) — mirrors #147 risk 5's fallback.

2. **#147 coordination / shared-library extraction.** If #147 lands `smoke-multi-experiment.sh` as a monolith, wave 1 must refactor it into the library without changing its observable behavior — a regression surface. **Mitigation:** the validation gate re-runs `compose-smoke-multi-experiment` after extraction; if behavior drifts, the extraction is wrong. Best avoided by coordinating with #147 to land the library shape up front (§11).

3. **DooD × per-experiment volumes (Cell 2) — the `name:` trap.** AGENTS.md's three DooD wiring traps (explicit `name:` on forwarded volumes; cidfile-dir bind-at-same-path; spawned-child uid pinning) all apply *per experiment*. If #147's exp-B substrate naming omitted explicit `name:`, Cell 2's DooD spawns resolve to fresh empty volumes and the smoke fails confusingly (empty worktrees). **This is a feature, not a bug** — it's exactly the cross-mode latent bug the issue targets. **Mitigation:** the cell's failure dump (`docker compose logs --tail 200`) plus a targeted assertion that exp-B's seeded bare-repo SHA is visible inside the spawned sibling; if it fails, the fix is in #147's volume naming (filed + fixed at root).

4. **Subprocess stdout back-pressure across many containers.** AGENTS.md's `subprocess.PIPE` 64 KiB-buffer wedge: with ~13 containers each logging JSON per request, undrained pipes can wedge a service mid-handler and surface as a spurious `ReadTimeout`. **Mitigation:** the library's bring-up uses Compose-managed logging (no `subprocess.PIPE` in the smoke itself — Compose handles container stdout); the smoke reads state via wire calls, not piped stdout. This risk is mostly inherited-and-already-mitigated from the existing smokes' shape.

5. **Auth-disabled scope (Decision 4) — open question surfaced.** Whether to include any auth-disabled cell hinges on whether auth-disabled is a supported **deployment** posture or purely a test convenience. **This is the one decision worth operator/codex confirmation before wave 3.** Recommendation: drop entirely unless someone names a deployment that runs auth-off; if kept, minimal-startup-only (§3.4). The full multi-worker auth-disabled smoke is rejected under any reading (degenerate single-identity).

6. **Issue-boundary vs. duplication tension (Decision 2).** Codex-review may read the shared library as "refactoring CI substrate setup" (the issue's out-of-scope item) OR read per-cell scripts as duplication (the codebase's slop discipline). **Mitigation:** the plan resolves this explicitly — the library covers only the *new* multi-experiment family (born together, not a refactor of the protected existing 8). Surfaced here so the reviewer challenges the framing rather than missing it.

7. **Matrix-doc staleness.** `ci-smoke-matrix.md` is only useful if kept current. **Mitigation:** the doc's own policy section states the rule (new exec mode / topology ⇒ new row); a lightweight CI check that every `compose-smoke-*` job appears in the matrix table is a sensible **follow-up** (file as issue — AGENTS-only guidance is weaker than a guardrail, per the codification discipline). Not in this chunk's scope, but the doc's policy section names it.

## 9. Migration / cleanup

Per AGENTS.md "No backwards-compatibility shims in greenfield / pre-external-user projects":

- **No retirements.** This plan is purely additive (new library + cells + jobs + doc). The only "migration" is the conditional wave-1 refactor of #147's `smoke-multi-experiment.sh` to source the shared library — that's a same-PR clean refactor, no shim, no deprecation window.
- *(Cell 3 only)* the `EDEN_AUTH_DISABLED` env knob defaults off; no v0/v1 split, no compat shim — auth stays on unless explicitly disabled.
- **Substrate-audit discipline** (AGENTS.md "Substrate migrations need a same-PR audit") applies lightly: if wave 1 extracts the library from #147's monolith, grep for any reference to the old function/script shape and update in the same commit.

## 10. Naming map

No identifier renames. New names (all following established conventions):

| New name | Convention it follows |
|---|---|
| `smoke-subprocess-multi-experiment.sh` / `compose-smoke-subprocess-multi-experiment` | `smoke-<cell>.sh` / `compose-smoke-<cell>` (parallel to `smoke-subprocess-docker.sh` / `compose-smoke-subprocess-docker`) |
| `smoke-docker-multi-experiment.sh` / `compose-smoke-docker-multi-experiment` | same |
| `smoke-auth-disabled.sh` / `compose-smoke-auth-disabled` (conditional) | same |
| `lib/multi-experiment-common.sh` | sourced-library convention (new `lib/` subdir under `healthcheck/`) |
| `docs/operations/ci-smoke-matrix.md` | `docs/operations/<topic>.md` (parallel to `multi-orchestrator.md`, `dispatch-mode.md`) |
| `EDEN_AUTH_DISABLED` (conditional) | `EDEN_<SCREAMING_SNAKE>` env-var convention; boolean `=1` opt-in (parallel to `EDEN_ORCHESTRATOR_MULTI_EXPERIMENT` from #147) |

## 11. Coordination with #147

Issues #147 and #183 share the multi-experiment smoke surface. To avoid a #147→#183 refactor coupling, the **preferred** shape is:

- #147's impl lands the common multi-experiment smoke flow **already factored** into `reference/compose/healthcheck/lib/multi-experiment-common.sh`, with `smoke-multi-experiment.sh` as the first thin wrapper that sources it.
- #183 then adds cells as additional thin wrappers — zero refactor of #147's code.

If #147 has already landed (or lands first as a monolith), #183's wave 1 performs the extraction (Decision 2; risk 2). Either way the existing 8 single-experiment smokes are untouched.

**Action:** when #147 enters impl, add a note to its plan / PR pointing at this section so the library shape is landed up front. (Filed as a comment on #147 at plan-merge of #183.)

## 12. Estimated effort

- **Wave 1** (library + Cell 1): ~2 days. Bulk is the library extraction + the subprocess-mode-specific assertions; the lease drill is inherited from #147.
- **Wave 2** (Cell 2 / DooD × multi): ~1.5 days. Structurally a thin wrapper, but the DooD × per-experiment-volume debugging (risk 3) is the wildcard — could surface a real #147-substrate bug that needs a fix.
- **Wave 3** (auth-disabled + docs): ~1 day (0.5 if Cell 3 is dropped).
- **Codex review + iteration:** ~1 day.

**Realistic total: ~4–5 working days**, plus the hard dependency on #147 having merged. Comparable to #147's own estimate; smaller per-cell because the substrate is inherited.

## 13. Why this is one plan, gated on #147

The three cells are sequential but bounded, and they all build on the same #147 substrate. Splitting into three plan-stage PRs would re-litigate the matrix strategy and the shared-library decision three times. One plan, one impl PR (three waves), one codex pass, one matrix-doc update. The load-bearing coupling is to #147, not between the cells — so the gating (Decision 1) is the real sequencing constraint, and within #183 the waves are cheap to land together.

If wave 2's DooD debugging blows up beyond the §12 estimate, the right shape is to land waves 1 + 3 first (Cell 1 + docs + optional Cell 3) and sequence Cell 2 as its own follow-on PR. The plan stays the same; only the merge cadence changes.
