# Web UI: multi-experiment operation

How the reference Web UI serves multiple experiments from one deployment
once a control plane is configured (`--control-plane-url`). Shipped in
issue [#145](https://github.com/ealt/eden/issues/145) (per-route store
swapping for the 12c experiment switcher). Reference-impl behavior, not
protocol.

## What "select an experiment" does

The cross-experiment dashboard (`/admin/experiments/`) and the top-nav
switcher dropdown record the operator's choice in the session cookie
(`Session.selected_experiment_id`). Every per-experiment page then
resolves the **active experiment** per request — the selected one when
set and valid, else the deployment default (`--experiment-id`) — and
operates against that experiment's store, config, and (for the executor
module) integrator repo. No control plane → the switcher is hidden and
every page uses the deployment default with zero resolution overhead, so
single-experiment deployments are unchanged.

## Credentials: the four postures

Workers are per-experiment-scoped (each experiment has its own worker
registry). The web-ui's startup credential is registered only in its
`--experiment-id` experiment; talking to another experiment needs a
credential there. How that credential is obtained defines four postures
(plan §3.2):

| Posture | Admin token | Switcher works? |
|---|---|---|
| **A — no control plane** | optional | n/a (switcher hidden; single experiment) |
| **B — control plane, admin token at runtime** | present | yes — per-experiment worker credentials are minted just-in-time on first switch |
| **C — control plane, admin token bootstrap-only** | present at first boot, then rotated out | yes for experiments already credentialed on disk; new experiments redirect with `error=cannot-bootstrap-credential` |
| **D — control plane, no admin token ever** | absent | dashboard read fails; a startup warning is logged; switcher effectively unavailable |

Posture **B** is the default Compose path. Posture **C** is the
production hardening (rotate the admin token out of the runtime env after
first boot, matching the worker-host pattern): the web-ui persists a
deployment-scoped control-plane worker credential at first boot so the
switcher's control-plane reads keep working without the admin token, and
per-experiment worker credentials persist on disk for reuse.

If you switch to a new experiment in Posture C/D and see
`cannot-bootstrap-credential`, the web-ui has no persisted credential for
that experiment and no admin token to mint one. Either (a) provide the
admin token at runtime (`--admin-token` / `$EDEN_ADMIN_TOKEN`), or
(b) pre-provision the credential by running the web-ui once against that
experiment with the admin token available.

## Credential + config + repo layout

Resolved by `--credential-dir` / `$EDEN_CREDENTIAL_DIR`, falling back to
the common `--credentials-dir` / `$EDEN_WORKER_CREDENTIALS_DIR`, then an
XDG default:

```text
<credential-dir>/
  control-plane/<cp-worker-id>.token     # Posture B/C deployment-scoped
  <experiment_id>/<worker-id>.token      # per-experiment worker (JIT)
```

Per-experiment **config** is loaded from
`<--experiment-config-dir>/<experiment_id>.yaml` (the deployment default
still uses `--experiment-config`); `setup-experiment.sh` drops each
experiment's YAML there. Per-experiment **integrator repos** (executor
module) live at `<repo-path-parent>/<experiment_id>.git`, cloned from the
`--forgejo-url` org base with the experiment id substituted.

> **Config drift (known limitation).** The on-disk config dir is a
> separate source from the task-store-server's internal config text. If
> you hand-edit one but not the other, the web-ui's and the worker hosts'
> views of an experiment's objective / evaluation_schema can diverge.
> Issue [#259](https://github.com/ealt/eden/issues/259) (a
> `GET /v0/experiments/{E}/config` wire read) closes this by construction.

## Compose

The web-ui service passes `--experiment-config-dir
/var/lib/eden/web-ui-configs` (bind-mounted from
`${EDEN_EXPERIMENT_DATA_ROOT}/web-ui-configs`) and `--credentials-dir
/var/lib/eden/credentials` (the existing credentials bind-mount).
`setup-experiment.sh` creates `web-ui-configs/` and copies each
experiment's config in. Register additional experiments via the
dashboard's admin form, then run `setup-experiment.sh` per experiment to
seed its task-store data + config YAML.

A single-stack Compose deployment running multiple experiments through
one control plane is the path of intent; running separate Compose
projects per experiment (§12.2 of the user guide) remains valid for hard
isolation. The end-to-end multi-experiment Compose smoke is tracked in
[#147](https://github.com/ealt/eden/issues/147); the single-experiment
smoke remains the golden path.
