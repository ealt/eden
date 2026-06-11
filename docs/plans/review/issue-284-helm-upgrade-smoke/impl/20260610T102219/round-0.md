# Issue #284 impl-stage codex-review — round 0 (2026-06-10)

`codex exec` (read-only sandbox, codex-cli 0.130.0) reviewing
`git diff origin/main...HEAD` on `impl/issue-284-helm-upgrade-smoke` —
the `helm-upgrade-smoke` CI job chunk (issue #284, Phase 13a §6.3
deferral; planless). Reviewed commit: `300d9bb`. Verdict:
**fix-then-ship**, 0×P1 + 1×P2 + 2×P3. All three addressed in the same
PR (fix commit follows the reviewed commit).

## [P2] Post-upgrade progress assertion could be vacuous

The script upgraded after seeing `>= 1 variant.integrated`, but the
3-variant fixture frequently completes during the setup script's own
rollout waits (the round-0 local validation run observed exactly this:
`variant.integrated=3 task.completed=10` already at the pre-upgrade
snapshot). The post-upgrade asserts then only re-checked the fixed
`>= 3 / >= 9 / >= 3` thresholds, so the smoke could pass without
proving the upgraded stack ever processed another task.

**Resolution:** the upgrade smoke now derives a **doubled-total
(6-variant) config** from the fixture (`sed` on `ideation_policy.total`
with a loud guard against fixture-shape drift), so work is still in
flight when the upgrade lands. Post-upgrade it waits for the 6-variant
end-state (`>= 6 integrated / >= 18 completed / >= 6 ideation`, via the
now-parameterized `eden_assert_end_state`) and adds a strict-progress
assertion keyed on **`variant.integrated`** — the last stage of the
task chain, so the slowest to saturate (the first fix attempt keyed on
`task.completed`, and the validation rerun immediately showed that
metric saturating at 18 while integrations were still in flight 3/6;
integrated-keyed gating is what actually proves post-upgrade work).
When integrations remained at the snapshot (`INTEGRATED_PRE < 6`, the
overwhelmingly common case with the doubled total), the final count
must strictly exceed it — pods that come back Ready but never integrate
another variant now fail. The all-integrated-pre-upgrade case logs a
NOTE instead of flaking (the no-regress, end-state, and `helm test`
assertions still hold there). Post-upgrade deadline gets its own knob
(`EDEN_HELM_UPGRADE_SMOKE_DEADLINE`, default 420s) since up to 5 of the
6 variants may still be pending at upgrade time.

## [P3] Push-to-main degenerated to a same-chart upgrade

`merge-base(HEAD, origin/main)` resolves to HEAD on a main push, so the
post-merge safety-net run exercised no chart diff.

**Resolution:** when the auto-resolved baseline equals HEAD (and
`HEAD~1` exists), the script falls back to `HEAD~1` — on a main push
that is the previous main commit, so the safety-net run upgrades
previous-main → new-main. The fallback lives in the auto-resolution
branch only; an explicit `EDEN_UPGRADE_BASELINE_REF` (including `HEAD`
for a deliberate same-chart run) is never overridden. A
`git cat-file -e <baseline>:reference/helm/eden/Chart.yaml` guard fails
loud when any baseline predates the chart.

## [P3] `git fetch origin main` silently tolerated + may only update FETCH_HEAD

**Resolution:** the fetch now uses an explicit refspec
(`+refs/heads/main:refs/remotes/origin/main`), and failure is tolerated
only when `refs/remotes/origin/main` already resolves locally;
otherwise the script exits 2 with a clear message.

## No findings

Bash-3.2 compatibility (no `mapfile`/`readarray`/`declare -A`; `bash
-n` passes), the `rollingUpdate: null` chart fix (correct for helm 3
three-way merge; renders as intended; no fresh-install side effect),
and the docs §7 `--reset-then-reuse-values` amendment all came back
clean. No structural-erosion findings on the lib extraction.

## Validation of the fixes

Full local rerun in kind with helm 3.16.2 (the CI pin) and the default
(merge-base) baseline — i.e. exactly the CI job's path, crossing the
`rollingUpdate: null` template diff with the doubled-total config — see
the round-1 record / PR description for the outcome.
