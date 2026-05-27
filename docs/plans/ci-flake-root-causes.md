# CI flake root causes — inventory + fix plan

**Status.** Draft (plan-stage; no impl in this PR).

**Scope.** Inventory every CI failure the team hit over the four weeks
2026-04-29 → 2026-05-26, separate genuine flakes from real failures, and
name the **root-cause** fix for each flake category. Explicitly *not* in
scope: retries, `continue-on-error`, `pytest.mark.flaky`, or any
symptom-masking (see [§7 Anti-goals](#7-anti-goals)).

**Predecessors.** None. This plan reads the current CI surface
([`.github/workflows/ci.yml`](../../.github/workflows/ci.yml)) and the
seven smoke scripts under
[`reference/compose/healthcheck/`](../../reference/compose/healthcheck/).

---

## 1. Headline finding

**The CI "flakes" are not random noise — they are three time-clustered
events plus one genuine external-infra flake.** Over four weeks there were
**84 failed workflow runs**. After classifying every one by log signature
(§2), the failures that look like "flakes" collapse into a small number of
distinct, datable incidents:

| Incident | Hits | Window | Verdict |
|---|---|---|---|
| `EDEN_IDEATION_POLICY` retirement broke smoke quiescence | 12 of 14 quiescence-timeouts | 2026-05-25 20:13–21:27 (single 75-min window, blast across ≥9 branches incl. `main` ×2) | **Real regression masquerading as a flake** (#215 → fixed #224) |
| 12a-1g bind-mount substrate cleanup `EACCES` | 9 | 2026-05-13 → 2026-05-15 | **Real bug, fixed forward (#94) but incompletely propagated** |
| codeberg.org registry `50x` on `forgejo` image pull | 4 | 2026-05-24 23:05 → 2026-05-25 05:25 (~6 h outage) | **Genuine external-infra flake** |
| Genuinely slow/stuck quiescence (unrelated to #215) | 2 of 14 | 2026-05-04 | **Diagnosis gap** (slow-vs-stuck indistinguishable) |

The remaining **≈53 failures are legitimate CI-gate catches during dev
iteration** — `rename-discipline` (16), `ruff` (12), `pytest` assertions
(15), `markdownlint` (5), one Dockerfile `uv sync` break. These are the
gate doing its job; they are **not** flakes and this plan does not touch
them. They are listed in §2 only to keep the accounting honest.

**Consequence for chunking:** this is *not* "~7 independent flake fixes."
It is **one masked-regression class (highest value), one
incomplete-fix-propagation cleanup, and one external-infra hardening** —
plus a cross-cutting structural fix (§6) that prevents the next masked
regression from blasting across every branch the way #215 did.

---

## 2. Methodology + full inventory

### 2.1 How the data was gathered

- `gh run list --limit 400 --created '>=2026-04-29'` → 392 runs (303 success,
  84 failure, 5 in-flight).
- **Rerun-passed detection** (`attempt > 1` with `conclusion == success`):
  the cleanest flake signal — a run that failed, then passed on re-run with
  no code change. Only **2** in four weeks (both diagnosed below).
- For all 84 failed runs, `gh run view <id> --json jobs` → which *jobs*
  failed; then `gh run view <id> --log-failed` grepped for a fixed set of
  error signatures to classify each run.

### 2.2 Per-signature tally (all 84 failed runs)

| Signature | Count | Flake? | Root-cause section |
|---|---|---|---|
| `rename-discipline` lint | 16 | No — real gate catch | — |
| `pytest` assertion (dev iteration) | 15 | No — real, on feature branches | — |
| **quiescence-timeout** (orchestrator did not exit in 180 s) | 14 | **Mixed** — 12 regression, 2 genuine | [§3](#3-quiescence-timeout-highest-value), [§5](#5-masked-bugs-failures-weve-been-treating-as-transient) |
| `ruff` lint | 12 | No — real gate catch | — |
| **bind-mount cleanup `EACCES`** | 9 | **Was real bug; latent** | [§4](#4-bind-mount-cleanup-eacces) |
| `variant.integrated` count assertion | 6 | Mostly real (12b WIP); 1 suspect | [§5](#5-masked-bugs-failures-weve-been-treating-as-transient) |
| `markdownlint` | 5 | No — real gate catch | — |
| **codeberg `50x` image pull** | 4 | **Yes — external infra** | [§4b](#4b-codeberg-image-pull-50x) |
| Dockerfile `uv sync --frozen` break | 1 | No — real (conformance member; fixed) | — |
| other / unclassified | 2 | n/a | — |

### 2.3 The two rerun-passed (cleanest) flakes

1. **Run 26375298092** (branch `impl/issue-130-forgejo-etc-bind-mount`,
   2026-05-24): `compose-smoke` + `compose-smoke-multi-orchestrator` failed
   on attempt 1 with `forgejo Error received unexpected HTTP status: 502 Bad
   Gateway`, passed on attempt 2. → **codeberg pull flake** (§4b).
2. **Run 25231197462** (branch `codify-phase-11-lessons`, 2026-05-01):
   `python-test` failed on attempt 1 in the orchestrator subprocess-e2e
   test — "expected 3 successful variants, got 0" — and passed on attempt 2.
   (The log's test name predates a vocab rename; the behavior is the
   subprocess-e2e timing flake.) This is the `eden#39` PIPE-buffer/stall
   area (closed), whose fix is
   already documented in AGENTS.md (the `_spawn` / `_read_port_announcement`
   / `_dump_logs` shape). Single occurrence post-fix; tracked as residual in
   §5, not a standalone chunk.

---

## 3. Quiescence-timeout (highest value)

**Signature.** `orchestrator did not exit within 180s; current status:
running`, followed by a `docker compose logs --tail 30 orchestrator` dump
and `exit 1`. Source:
[`smoke.sh`](../../reference/compose/healthcheck/smoke.sh) lines 167–185
(and the equivalent block copy-pasted into the other six scripts).

**What actually happened (the 12 of 14).** On 2026-05-25, PR **#215**
(title: "Fix #133: surface ideation policy in experiment config") retired the
`EDEN_IDEATION_POLICY` env var and moved the policy into experiment config.
The fixture/compose wiring fell out of sync, so the orchestrator's ideation
policy kept topping up the work queue every iteration — the experiment
**never quiesced**. Every branch that ran CI in the next ~75 minutes
(including `main` twice) hit the 180 s wall and reported it as a
`compose-smoke*` failure. PR **#224** ("Fix smoke quiescence after #215
retired `EDEN_IDEATION_POLICY` env var") fixed it. The 12 failures were
**one regression with a wide blast radius**, not 12 flakes.

**The 2 of 14 (2026-05-04, `audit-and-tooling`).** Genuine slow-or-stuck:
the orchestrator was still `running` at 180 s with no config regression in
play. Whether it was *slow* (would have finished at 200 s) or *stuck*
(deadlock / lost task) is **unknowable from the log** — the `--tail 30`
dump shows only the last few poll iterations.

### Root-cause fixes

**3.1 — Orchestrator progress heartbeat (effort: M, ~1 day).** The 180 s
deadline with a tail-30 dump makes "slow" and "stuck" indistinguishable —
exactly the AGENTS.md "CI log is a hint; local stack is ground truth"
trap, but here we can't even re-run locally to reproduce a timing-sensitive
stall. Fix: have the orchestrator loop emit a structured
`orchestrator.progress` heartbeat each iteration carrying
`{iteration, open_tasks, in_flight, completed, last_transition_age_s}`.
The smoke quiescence-wait then asserts on **forward progress** (the counts
are changing / `last_transition_age_s` stays low) rather than only on a wall
clock. On timeout, the script dumps the heartbeat series, so the failure
report says "stuck at 4 open tasks for 120 s" vs "completing ~1 task / 8 s,
just slow" — turning every future timeout into a self-diagnosing failure.
This is the single highest-leverage change: it does not *prevent* the next
quiescence break, but it makes every one of them legible in the CI log
without a local repro.

**3.2 — Quiescence-config guard so the next #215 fails loud at startup
(effort: S–M, ~half day).** The reason #215 blasted 12 runs is that a
misconfigured ideation policy fails *silently* — the orchestrator runs
forever and the only symptom is a 180 s timeout three minutes later. Fix:
validate the resolved ideation/quiescence configuration at orchestrator
startup (or in `setup-experiment`) and fail fast with a clear message if the
policy can never reach a quiescent state under the smoke fixture (e.g. an
unbounded top-up policy with no variant cap). A startup `ValueError:
ideation policy will never quiesce (no variant cap, top-up=N)` converts a
75-minute cross-branch outage into a one-line, one-branch failure on the PR
that introduced it.

**3.3 — Make the 180 s a function of observed progress, not a magic
constant (effort: S, folds into 3.1).** Replace the fixed
`deadline=$((SECONDS + 180))` with "extend the deadline whenever the
heartbeat shows a state transition; only fail when progress has stalled for
T seconds." A genuinely slow-but-progressing experiment stops being a flake;
a truly stuck one fails *faster* than 180 s.

---

## 4. Bind-mount cleanup `EACCES`

**Signature.** During teardown, `rm: cannot remove
'/tmp/eden-smoke-*/…': Permission denied` on every file under the
bind-mount substrate, then `exit 1`. The smoke *assertions had already
passed* — the script failed in cleanup.

**Root cause.** Phase 12a-1g migrated worker repo storage from named docker
volumes to host bind-mounts under `${EDEN_EXPERIMENT_DATA_ROOT}`. Container
processes write those files as uids the host runner doesn't match
(`postgres=70`, `forgejo/eden=1000`) in mode-0755 subdirs, so the host's
`rm -rf` hits `EACCES`. All 9 occurrences fall in the 2026-05-13 → 05-15
window — the migration window.

**Status.** Fixed forward by **PR #94** ("container-side rm in smoke-script
teardown", shipped 2026-05-15): a sibling `alpine` container runs as root to
`find /cleanup -mindepth 1 -delete`, then the host `rmdir`s the now-empty
root, all behind `|| true` so cleanup can't mask the real exit code. This is
a correct root-cause fix.

**Why it's still on the list — incomplete propagation (effort: S, ~half
day).** The fix did **not** reach all seven smoke scripts. Audit of the
cleanup functions:

| Script | root-container delete? |
|---|---|
| `smoke.sh` | yes |
| `smoke-checkpoint.sh` | yes |
| `smoke-subprocess.sh` | yes |
| `smoke-subprocess-docker.sh` | yes |
| `e2e.sh` | yes |
| `smoke-manual-mode.sh` | **no** |
| `smoke-multi-orchestrator.sh` | **no** |

`smoke-manual-mode.sh` and `smoke-multi-orchestrator.sh` clean up only with
`docker compose down -v` + `rm -f $ENV_FILE` and never delete a
`SMOKE_DATA_ROOT` at all. On CI's fresh runners this is invisible (the
runner is discarded); on a contributor laptop re-running locally it
accumulates container-owned trees and, the moment either script starts
writing to a per-run data root, it will reproduce the exact #94 `EACCES`.
This is the AGENTS.md "substrate migration needs a same-PR audit of every
consumer of the old surface" pitfall (#178) recurring at the script layer.
**Fix: propagate the #94 teardown to both scripts** — and see §6 for why the
durable fix is to stop copy-pasting teardown across seven scripts entirely.

### 4b. codeberg image pull `50x`

**Signature.** `forgejo Pulling … Error received unexpected HTTP status:
502 Bad Gateway` (also seen as 504). The `forgejo` service image is
[`codeberg.org/forgejo/forgejo:11-rootless`](../../reference/compose/compose.yaml)
(line 35). All 4 occurrences cluster in a ~6 h window 2026-05-24 23:05 →
2026-05-25 05:25 — a codeberg.org registry outage that caught one `main` run
and three PR runs.

**Why it's a true flake, not a bug.** Nothing in our code changed; an
external single-homed registry was down. This is the one category where the
failure is genuinely outside our control — so the fix is *resilience*, not a
code correction.

### Root-cause fix

**4b.1 — Mirror the forgejo image to a registry we control + pin by digest
(effort: M, ~1 day).** codeberg.org is a single point of failure for every
`compose-*` job. Three layers, in order of impact:

1. **Mirror + pin.** Re-tag `codeberg.org/forgejo/forgejo:11-rootless` into
   GHCR under the repo (`ghcr.io/ealt/forgejo:11-rootless@sha256:…`) and
   reference it from `compose.yaml` **by digest**. GHCR shares GitHub's
   uptime/SLA with the runners and removes the codeberg dependency from the
   hot path. Digest-pinning also closes a supply-chain drift gap (the
   floating `:11-rootless` tag can move under us). **Verify in impl** that a
   suitable mirror tag exists / that we have push rights to GHCR for the
   repo; if GHCR is not viable, Docker Hub (`docker.io/forgejo/forgejo`) is
   the fallback — confirm the `11-rootless` tag is published there before
   committing to it.
2. **Pre-warm the cache in CI.** Add a `docker pull` (of the pinned digest)
   as an early workflow step *before* `compose up`, so an image-registry
   blip surfaces as a clear, isolated "pull failed" step rather than a
   confusing mid-`compose up` 502. Optionally `actions/cache` the saved
   image tarball keyed on the digest.
3. **(Optional) bounded pull retry on the pre-warm step only.** A *single*
   `docker pull` retry on the dedicated pre-warm step is **not** a
   test-retry and does not violate §7 — it is image-fetch resilience, scoped
   to the network operation, never wrapping the smoke assertions. Decide in
   review whether (1)+(2) suffice; if the mirror is on GHCR, retry is likely
   unnecessary.

---

## 5. Masked bugs (failures we've been treating as transient)

Per the task's requirement to surface "this is actually a bug, not a flake":

- **#215 quiescence regression (12 hits) — the poster child.** Already fixed
  (#224), but the *pattern* will recur: any future change to ideation
  policy, variant caps, or quiescence wiring can silently make the orchestrator
  never quiesce, and the only symptom is a 180 s timeout. The §3.2 startup
  guard is the durable fix; without it, the next config drift produces
  another cross-branch outage. **This is the strongest "masked bug" signal
  in the dataset: 12 reruns/failures all tracing to one silent
  never-quiesce.**
- **Multi-orchestrator over-integration — `expected exactly 3
  variant.integrated; got 4` (run 25830615663, 2026-05-13).** One occurrence,
  but it is a *correctness* smell, not a timing one: two orchestrators
  integrating the same lineage would produce a 4th integration. Worth a
  focused look at the multi-orchestrator lease/CAS path before it is dismissed
  as noise — if reproducible, it is a real concurrency bug, not a flake. File
  as an investigation issue (§8).
- **`variant.integrated … got 0` on `impl/phase-12b-*` (5 hits).** These are
  real WIP failures during the 12b checkpoint chunk (import not yet producing
  integrations), already resolved by the time 12b merged — listed for
  completeness, not action.
- **orchestrator subprocess-e2e timing flake (1 post-fix hit, §2.3).** The
  `eden#39` stall class. The documented fix landed; the single residual
  occurrence (2026-05-01) may predate full propagation. Action: confirm the
  `_spawn`/file-redirect drainer pattern is used in *every* multi-subprocess
  test, not just the orchestrator e2e ones (one-time audit, effort S).

---

## 6. Cross-cutting root cause: no shared smoke helper

The seven smoke scripts each **re-implement** setup, env-file generation,
the quiescence-wait loop, and teardown. There is **no shared library**
(`grep -l 'source' *.sh` finds none). This is the structural reason the #94
cleanup fix reached five scripts but not two, and the reason the §3
quiescence-wait block (the 180 s constant + tail-30 dump) is copy-pasted
seven times. Every per-script fix in §3/§4 will drift again the next time a
script is added unless the shared logic is extracted.

**Fix: extract `reference/compose/healthcheck/lib.sh` (effort: M, ~1–1.5
days).** A sourced helper providing `eden_smoke_setup`,
`eden_smoke_wait_quiescent` (the heartbeat-aware wait from §3),
`eden_smoke_teardown` (the #94 root-container delete), and the bash-3.2-safe
array helpers AGENTS.md already mandates. Then each script sources it.
Bonus: a CI lint that fails if a `healthcheck/*.sh` script defines its own
`cleanup()` instead of calling the shared one — the mechanical guardrail
that makes the next §4-style drift impossible (AGENTS.md "a hook beats a
preference you have to remember").

---

## 7. Anti-goals

These are explicitly **out of scope** and must not appear in any chunk:

- **No `continue-on-error`** on any job. (None exists today — keep it that
  way.)
- **No automatic retry-on-failure** of test jobs or smoke scripts. The only
  retry permitted anywhere is a *bounded `docker pull` retry on the
  image-pre-warm step* (§4b.1.3), which is network-fetch resilience, never a
  test re-run.
- **No `pytest.mark.flaky`** / rerun plugins.
- **No raising the 180 s wall to "just wait longer."** §3.3 replaces the
  magic constant with progress-based extension; bumping the constant would
  mask a stuck orchestrator.

Every item above addresses a *root cause*: a real regression (§3.2), an
incomplete fix (§4), a single-homed dependency (§4b), or the structural
duplication that lets fixes drift (§6).

---

## 8. Recommended chunking + sequencing

Ordered by cycle-time impact (highest first). Each chunk is independently
shippable.

1. **Chunk A — Quiescence legibility + guard (§3.1, §3.2, §3.3).**
   *Highest value.* 14 of the period's failures were quiescence-timeouts and
   the class is guaranteed to recur. The heartbeat + progress-based wait +
   startup guard turn the next occurrence from a silent 75-minute
   cross-branch outage into a one-line failure on the offending PR. Effort:
   M (~1.5–2 days). Depends on nothing.
2. **Chunk B — Shared smoke helper (§6), carrying the #94 teardown to all
   seven scripts (§4).** Do this *second* so Chunk A's heartbeat-aware wait
   lands in the shared `lib.sh` rather than being written once and
   copy-pasted. Closes the bind-mount `EACCES` latent recurrence and installs
   the anti-drift CI lint. Effort: M (~1.5 days). Soft-depends on A (wants A's
   wait function to extract).
3. **Chunk C — forgejo image mirror + pin + pre-warm (§4b).** Removes the one
   genuine external-infra flake. Independent of A/B; can run in parallel.
   Effort: M (~1 day), gated on verifying the mirror target (GHCR vs Docker
   Hub).
4. **Chunk D — Investigation issues, not code (§5).** File: (i)
   multi-orchestrator over-integration investigation, (ii) subprocess-e2e
   drainer-pattern audit. Effort: S. No merge dependency.

**Parallelism:** A and C are independent and can run concurrently; B should
follow A. Critical path is A → B (~3–3.5 days); C is off the critical path.

---

## 9. Validation gates

Plan-stage validation (this PR):

```text
npx --yes markdownlint-cli2@0.14.0 "docs/plans/ci-flake-root-causes.md"
python3 scripts/check-rename-discipline.py
```

Per-chunk impl validation (when each chunk lands) is the literal AGENTS.md
"Commands" quartet — `ruff` / `pyright` / full `pytest -q` / the affected
`smoke*.sh` scripts — **not** a narrowed subset (AGENTS.md "the Commands
section is the literal pre-push validation gate"). Chunk A and B must run
**all seven** smoke scripts locally, since the whole point is cross-script
consistency.

---

## 10. Deferrals tracked as issues

Per AGENTS.md, every deferral is filed as a GitHub issue at deferral time
and referenced here:

- Chunk A — quiescence heartbeat + guard: *issue TBD at plan-merge.*
- Chunk B — shared smoke `lib.sh` + anti-drift lint: *issue TBD.*
- Chunk C — forgejo image mirror/pin/pre-warm: *issue TBD.*
- Chunk D(i) — multi-orchestrator over-integration investigation:
  *issue TBD.*
- Chunk D(ii) — subprocess-e2e drainer-pattern audit: *issue TBD.*

(Issue numbers are filled in when this plan merges and the chunks are
scheduled; the PR description enumerates them per the deferral-tracking
rule.)
