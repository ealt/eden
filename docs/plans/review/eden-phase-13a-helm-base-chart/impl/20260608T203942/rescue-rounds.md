# Phase 13a impl-stage codex review — rescue rounds (post-#128 port)

This file records the `codex:rescue` review iterations run while porting the
Phase 13a Helm chart off its pre-#128 identity model to the single-experiment
post-#128 model (Option A). The `plan.md` / `impl.patch` siblings capture the
earlier `/codex-review` attempt that was interrupted mid-run; these rounds are
the substantive impl-stage review that converged the port.

## Context

The chart as originally drafted predated [#128](https://github.com/ealt/eden/issues/128)
(opaque-id minting) and hardwired lease-driven multi-experiment mode:

- worker ids were `$(POD_NAME)` / operator-typed literals, not server-minted
  `wkr_*`;
- the bootstrap `POST`ed `{"worker_id": ...}` / `{"group_id": ...}` (pre-#128
  shape) instead of `{"name": ...}` (which mints + returns `worker_id` +
  `registration_token`);
- `experiment.id` was an operator mnemonic (`exp-1`), which the post-#128 event
  model rejects (`^exp_[0-9a-hjkmnp-tv-z]{26}$`);
- the orchestrator unconditionally received `EDEN_CONTROL_PLANE_URL`.

Option A: port to the single-experiment path that `compose-smoke` validates,
with lease-driven HA as an opt-in toggle deferred behind
[#281](https://github.com/ealt/eden/issues/281).

## Round 1

Reviewed the ported state. One concrete chart bug:

- **Identity gate vs Secret render condition.** `eden.identityEnabled` gated an
  identity-consuming workload on `workerId` alone, while the per-service
  identity Secret rendered only when BOTH `workerId` and `token` were set — so a
  partial/manual values state (workerId set, token empty) would render a pod
  referencing a Secret that never rendered.
  **Fix (commit "Address codex: identity gate requires both workerId and token"):**
  tighten the gate to require both, matching the Secret's render condition.

The round was cut off before final consolidation (session interruption); the one
grounded finding above was extracted from the transcript and fixed.

## Round 2 — verdict CONVERGED (no smoke-blockers)

1. **CORRECTNESS-BUG — web-ui credential path.** The chart installed every token
   at `/var/lib/eden/credentials/<worker_id>.token`, but web-ui resolves its
   per-experiment worker token through `BearerCache` at
   `<credential-dir>/<experiment_id>/<worker_id>.token`, and the chart passed no
   `--credential-dir` (so web-ui used the XDG default and never saw the
   provisioned token — surviving only via the admin-reissue fallback). Not a
   smoke-blocker (admin token covers it) but the provisioned identity was a
   no-op for web-ui.
   **Fix:** `eden.identityTokenPath` keys on the service — web-ui installs at the
   experiment-namespaced subdir, worker hosts stay flat; the web-ui pod now
   passes `--credential-dir /var/lib/eden/credentials`.
2. **STRUCTURAL — gate docs/schema drift.** values.yaml + schema still described
   the identity gate as `workerId`-only.
   **Fix:** updated the comment to "both workerId and token"; `values.schema.json`
   `serviceIdentity` now constrains each identity to both-empty-or-both-set
   (`oneOf`), so a half-set hand-written values file fails at lint/template time.
3. **STRUCTURAL — schema coverage.** Several live keys were absent from the
   schema.
   **Fix:** added `experiment.configRaw`, `postgres.*`, `forgejo.*`, and
   top-level `resources`/`nodeSelector`/`tolerations`/`affinity`.

Confirmed: lease mode genuinely opt-in (default off → no control-plane, no
`EDEN_CONTROL_PLANE_URL`, `replicas.orchestrator=1`); the orchestrator self-joins
the `orchestrators` group at startup under the admin token; `helm template`
renders valid k8s for both single-experiment and lease-mode.

## Round 3 — verdict CONVERGED (final verification)

Verified the round-2 fixes are correct and introduced no regression. The web-ui
identity path is aligned end-to-end (`resolve_credential_dir` →
`BearerCache.bearer_for` reads `<base>/<experiment_id>/<worker_id>.token`; the
helper installs there; the pod passes `--credential-dir`). The identity gate +
Secret + schema `oneOf` are mutually consistent. The single-experiment smoke
path is coherent: seed → task-store-server → mint groups/workers/tokens → app
tier with pre-provisioned credentials verified via `/whoami`.

No smoke-blocking identity-path bug or regression. **CONVERGED.**
