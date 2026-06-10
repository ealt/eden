# Issue #284 impl-stage codex-review — round 1 (2026-06-10)

`codex exec` (read-only sandbox, codex-cli 0.130.0) verifying the
round-0 fixes (commit `d6e1029`) on `impl/issue-284-helm-upgrade-smoke`.
Verdict: **fix-then-ship**, one residual P2 on the round-0 P2's fix.

## Round-0 status

- **P2 vacuous progress assertion: partially resolved** — see below.
- **P3 same-chart main push: resolved.** `HEAD~1` fallback fires only
  when the auto-resolved merge-base equals HEAD; an explicit
  `EDEN_UPGRADE_BASELINE_REF` is preserved.
- **P3 fetch hygiene: resolved.** Explicit
  `+refs/heads/main:refs/remotes/origin/main` refspec; failure
  tolerated only when `origin/main` already resolves.

Codex also re-verified bash-3.2 cleanliness (`bash -n` under bash
3.2.57 passes on all three scripts) and found no new sed-shape or
compatibility issues.

## [P2] Strict progress was measured from the wrong baseline

The round-0 fix compared `INTEGRATED_FINAL > INTEGRATED_PRE` — the
**pre-upgrade** snapshot. If the remaining variants integrate during
the upgrade/rollout window itself, the upgraded-and-ready stack can
make zero further progress and still pass. The round-0 validation
run's own data showed exactly this shape: 4/6 integrated pre-upgrade,
6/6 already at the post-rollout snapshot — `FINAL(6) > PRE(4)` passed
while proving nothing about the post-rollout stack.

**Resolution:** the strict check now baselines on `INTEGRATED_POST` —
the snapshot taken **after** `helm upgrade --wait` and every
`kubectl rollout status` settled — so the progress proven is
attributable to the upgraded-and-ready stack. The unprovable branch
(`INTEGRATED_POST >= UPGRADE_TOTAL`, i.e. everything integrated inside
the upgrade window) logs a NOTE instead of flaking; the no-regress,
6-variant end-state, and `helm test` assertions still hold there.

## Validation of the fix

Full local kind run with helm 3.16.2 (the CI pin) and the default
merge-base baseline on the final code: PASSED (see the PR description
for the per-run count traces across all validation runs).
