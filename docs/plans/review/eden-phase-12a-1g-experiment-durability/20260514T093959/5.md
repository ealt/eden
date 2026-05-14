# Phase 12a chunk 1g — Experiment durability (spec invariant + Compose bind-mounts)

## 1. Context

A manual EDEN experiment running on the reference Compose stack was wiped overnight when Docker Desktop rebuilt its embedded Linux VM during an automatic update. The substrates were doing their job: Postgres fsync'd writes (WAL + `synchronous=FULL`), Gitea fsync'd its sqlite DB + git packs, the artifact store wrote atomic-rename files. The substrates' *disk* — the Docker Desktop VM's storage backing the named volumes — was less durable than the substrates assumed.

The failure mode is structural, not a bug:

- All EDEN substrate state lives in Docker named volumes (`eden-postgres-data`, `eden-gitea-data`, `eden-artifacts-data`, per-host `eden-*-repo` and `eden-*-credentials` volumes).
- Docker named volumes are stored inside the Docker engine's storage area. On Linux that's typically `/var/lib/docker/volumes/` on the host filesystem; on Docker Desktop (macOS / Windows) it's a virtual disk inside the embedded VM (`Docker.raw` on macOS).
- The embedded VM's disk is opaque to the operator. It is replaced wholesale by Docker Desktop updates, by `Reset to factory defaults`, by `Clean / Purge data`, and by the user's own `docker volume rm`. None of these are "disk failure" events the substrate could defend against — they are administrative deletions of the substrate's whole storage surface.
- The operator's mental model — "Postgres persists my data; Gitea persists my repo; therefore the experiment is durable" — is *correct* per the protocol, but breaks because the substrates' physical storage is not a persistent host filesystem.

The operator articulated the requirement this way:

> Experiments should be durable until the owner decides to shut it down. Experiment data should be persisted during and after the experiment. If the running processes terminate intentionally or not, it should be possible to restart and recover the state where things left off.

This chunk does two things:

1. **Spec.** Add a normative aggregate-durability invariant to [`spec/v0/01-concepts.md`](../../spec/v0/01-concepts.md) that makes this requirement first-class. Binding-agnostic: the spec states the WHAT, deployments choose the HOW. [`spec/v0/08-storage.md`](../../spec/v0/08-storage.md) §3 already covers per-store crash-recovery; the new section establishes the aggregate-over-substrates invariant that §3 composes to. [`spec/v0/09-conformance.md`](../../spec/v0/09-conformance.md) §5 gets a deferred "Experiment durability" group entry (no scenarios this chunk per operator decision; the spec invariant is the substantive deliverable).
2. **Compose binding.** Switch the reference Compose deployment from Docker named volumes to host bind-mounts under a configurable per-experiment data root (`$HOME/.eden/experiments/<id>/` by default). This is a deployment-binding decision local to the reference Compose stack; other deployments (Phase 13a's Helm chart already uses PVCs; Phase 13c/d/e move to managed substrates) satisfy the invariant their own way.

The chunk is intentionally narrow. It does not:

- Ship `backup-experiment.sh` / `restore-experiment.sh` tooling. Durability comes from the substrate; operators rely on normal filesystem-level backup hygiene of their `~/.eden/` directory for protection against disk failure.
- Add `checkpoint` / `restore` commands. Those are Phase 12b's portable-checkpoints concern (cross-implementation portability, not local durability).
- Author conformance scenarios for the durability invariant. Deferred per operator decision; chapter 9 §5 lists the group as a placeholder.
- Touch managed-substrate work (Phase 13c managed Postgres / 13d S3-GCS / 13e Gitea hardening). Those are the cloud-deployment path; out of scope here.
- Touch the Helm chart (Phase 13a). Helm uses PVCs already; the natural durability model under Kubernetes is unchanged.

## 2. Decisions captured before drafting

The operator's brief settled five load-bearing decisions before this plan. Listed here so codex-review and future maintainers can see what was deliberate vs. proposable:

1. **No operator-runnable backup tooling.** Durability comes from the substrate. Operators relying on filesystem-level backup of `~/.eden/` is acceptable; EDEN does not ship a `backup-experiment.sh`.

2. **Modularity.** The spec invariant is binding-agnostic. The Compose deployment chooses host bind-mounts as ITS particular binding. Other deployments (Phase 13a k8s PVCs, future managed cloud) satisfy the invariant differently. The chunk does NOT prescribe HOW to achieve durability; it states WHAT must hold.

3. **Default data root.** `EDEN_EXPERIMENT_DATA_ROOT=$HOME/.eden/experiments/$EDEN_EXPERIMENT_ID`. Operator-overridable via `--data-root <path>` on `setup-experiment.sh`.

4. **Conformance scenarios deferred.** The chapter-9 §5 index gains a placeholder group entry for "Experiment durability" but no scenarios are authored in this chunk. (The spec invariant is observable through any wire-API mutation that survives a substrate restart; a future chunk can drive a "stop-stack / kill-volume-mount / start-stack / replay" harness against any conforming IUT. The invariant is what enables that test, not the test itself.)

5. **Migration: documented one-shot recipe in the operator doc; no shipped tooling.** EDEN is pre-user-deployment-base; migration friction is acceptable. Existing Compose experiments on named volumes are migrated by stopping the stack, `docker run --rm -v <old>:/from -v <new>:/to alpine cp -a /from/. /to/` per substrate, and bringing the stack back up against the bind-mount paths.

These five decisions are NOT up for re-litigation in codex-review unless review surfaces a load-bearing contradiction with another spec MUST.

## 3. Design

### D.1 Spec invariant (chapter 01)

Add a new normative section to [`spec/v0/01-concepts.md`](../../spec/v0/01-concepts.md). Slot: between the current §12 (Workers and groups) and §13 (Relationships), pushing the current §13 to §14.

**Lifetime model — anchor to existing termination semantics, do NOT replace.** Chapter 01 §1 already enumerates termination conditions ("variant-count, wall-clock, convergence window, target condition") and §13 (Relationships) names "the experiment terminates per its configured conditions" as the closing event of an experiment's lifecycle. The durability invariant introduced here MUST NOT contradict that model. The new section anchors to it, says nothing new about WHEN an experiment terminates, and only adds a normative property about WHAT must survive across substrate restarts during the lifetime §1 defines. (The operator's articulated requirement, "until the owner decides to shut it down," is consistent with this: in the current v0 spec the only termination paths are the configured conditions in §1; future Phase 12 control-plane chapters may add an explicit operator-termination operation. The durability invariant constrains both.)

Proposed prose for the new §13:

> ## 13. Experiment durability
>
> An experiment is **durable** for its entire operational lifetime. The protocol's per-store durability rules ([`08-storage.md`](08-storage.md) §3) cover individual writes; this section states the aggregate invariant they compose to.
>
> **Lifetime.** An experiment's lifetime is bounded by the lifecycle defined in §1 and the relationships in §14: it begins when the experiment is registered (its evaluation schema and other configuration persisted, [`08-storage.md`](08-storage.md) §4.1) and continues until the experiment reaches a terminal state per its configured termination conditions (§1) or is otherwise discarded by an authorized operator. This section does NOT introduce a new termination model; it constrains what must survive across substrate restarts during the lifetime §1 already defines.
>
> **Aggregate invariant.** Throughout an experiment's lifetime, the union of its protocol-owned state — tasks, ideas, variants, events, the per-experiment worker / group registry, artifacts referenced from protocol-owned objects, and the git-side artifacts the integrator publishes — MUST collectively survive process restart, host restart, and individual substrate-component restart. A conforming deployment MUST NOT cause this state to be lost as a side-effect of routine substrate maintenance (process restart, host reboot, container engine restart, individual substrate replacement, …).
>
> **Implementation latitude.** The protocol is agnostic about HOW a deployment achieves the aggregate invariant. Per-store durability ([`08-storage.md`](08-storage.md) §3.1) constrains each substrate individually; the aggregate invariant constrains the *deployment*. A conforming deployment chooses substrate bindings (host filesystem mounts, Kubernetes PersistentVolumes, managed cloud storage with explicit SLA, …) consistent with its operational constraints. Different deployments MAY satisfy this invariant differently; all conforming deployments MUST satisfy it.
>
> **Non-protocol-owned losses.** Disk failure, accidental `rm -rf`, and explicit storage-substrate deletion are outside the protocol's control. A conforming deployment SHOULD document what operator actions destroy experiment state and SHOULD design its substrate bindings so that routine maintenance (updates, restarts, reconfiguration) does not.

Cross-references to add:

- Chapter 01 §10 (Storage components): one-line addition pointing at the new §13.
- Chapter 08 §3 (Durability): one-line cross-reference noting that §3's per-store rules compose to chapter 01 §13's aggregate invariant.
- Chapter 09 §5 (Scenario index): a new "Experiment durability" group row with status "(deferred — scenario authoring planned for a follow-up chunk)" and primary citation `[01-concepts.md] §13`. The check_citations harness gates on cited sections having `\bMUST\b`; §13 has multiple MUSTs (aggregate-invariant survival; no-loss-as-side-effect-of-maintenance).

### D.2 Why chapter 01 (not chapter 08)

Chapter 08 §3 is per-individual-store durability ("the task store survives crash of its host"; "the event log survives crash of its host"). The new invariant is the property the deployment-as-a-whole exposes to the operator — that the experiment, viewed as a single bundle of state, survives substrate restarts.

Chapter 01 is the concepts chapter; it already names "durable stores" in §10 without defining what experiment-level durability means. Slotting §13 there closes that gap. Chapter 08 stays the place where each store's individual contract is specified; chapter 01 §13 is what the chapter-08 rules compose to.

This split mirrors the existing chapter-01-vs-rest pattern (concepts in chapter 01, behavioral contracts in subsequent chapters).

### D.3 Compose binding: host bind-mounts under a per-experiment data root

The reference Compose deployment switches every **durable substrate-data** volume from a Docker named volume to a host bind-mount under `${EDEN_EXPERIMENT_DATA_ROOT}/`. Volumes that hold ephemeral / scratch / bootstrap-only state stay as named volumes — explicitly classified so the durability boundary is auditable.

**Durable (converted to bind-mount):**

| Volume name (today) | New bind-mount target | Substrate | Declared in |
|---|---|---|---|
| `eden-postgres-data` | `${EDEN_EXPERIMENT_DATA_ROOT}/postgres/` | task-store | `compose.yaml` |
| `eden-gitea-data` | `${EDEN_EXPERIMENT_DATA_ROOT}/gitea/` | git remote (workers' canonical git surface) | `compose.yaml` |
| `eden-artifacts-data` | `${EDEN_EXPERIMENT_DATA_ROOT}/artifacts/` | artifact store (web-ui's `--artifacts-dir`) | `compose.yaml` |
| `eden-orchestrator-repo` | `${EDEN_EXPERIMENT_DATA_ROOT}/orchestrator-repo/` | per-host bare clone (orchestrator) | `compose.yaml` |
| `eden-executor-repo` | `${EDEN_EXPERIMENT_DATA_ROOT}/executor-repo/` | per-host bare clone (executor) | `compose.yaml` |
| `eden-evaluator-repo` | `${EDEN_EXPERIMENT_DATA_ROOT}/evaluator-repo/` | per-host bare clone (evaluator) | `compose.yaml` |
| `eden-web-ui-repo` | `${EDEN_EXPERIMENT_DATA_ROOT}/web-ui-repo/` | per-host bare clone (web-ui) | `compose.yaml` |
| `eden-orchestrator-credentials` | `${EDEN_EXPERIMENT_DATA_ROOT}/credentials/orchestrator/` | persisted worker registration token | `compose.yaml` |
| `eden-ideator-credentials` | `${EDEN_EXPERIMENT_DATA_ROOT}/credentials/ideator/` | persisted worker registration token | `compose.yaml` |
| `eden-executor-credentials` | `${EDEN_EXPERIMENT_DATA_ROOT}/credentials/executor/` | persisted worker registration token | `compose.yaml` |
| `eden-evaluator-credentials` | `${EDEN_EXPERIMENT_DATA_ROOT}/credentials/evaluator/` | persisted worker registration token | `compose.yaml` |
| `eden-web-ui-credentials` | `${EDEN_EXPERIMENT_DATA_ROOT}/credentials/web-ui/` | persisted worker registration token | `compose.yaml` |

**Ephemeral / scratch (stays a named volume; explicitly classified):**

| Volume name (today) | Disposition | Why ephemeral |
|---|---|---|
| `eden-repo-init-staging` | unchanged — stays a named volume | bootstrap-only; setup-experiment intentionally `docker volume rm`s it on re-seed |
| `eden-worktrees` (overlay-only, `compose.subprocess.yaml`) | unchanged — stays a named volume | per-task scratch worktrees; shared between executor and evaluator hosts but holds no protocol-owned state. A crash mid-task drops the worktree and the task is reclaimed per the chapter-04 §5 sweep — the per-host startup `git worktree remove --force` already assumes worktrees don't survive restart |
| `eden-blob-data` | **deleted entirely if confirmed unused** (see §8.3) | legacy from 10a; only `blob-init` mounts it, and no reference service reads `/var/lib/eden/blobs` |

**Per-experiment bootstrap directories (NOT under `${EDEN_EXPERIMENT_DATA_ROOT}` — classified explicitly):**

setup-experiment.sh creates two per-experiment directories that sit in the source tree (`reference/compose/.gitea-creds-${EXPERIMENT_ID}/`, `reference/compose/.cidfiles-${EXPERIMENT_ID}/`) and bind-mounts them into the worker hosts. They hold:

- `gitea-creds-<id>/credential-helper.sh` — the per-experiment Gitea HTTP-Basic password embedded as a shell `case` block.
- `.cidfiles-<id>/` — per-spawn container-id files for the docker-exec wrap's SIGKILL-escalation path.

Neither is protocol-owned state. Both are regenerated deterministically by setup-experiment: the gitea password is preserved across re-runs via `read_env_key` from `.env`; the cidfiles dir is per-spawn ephemeral and recreated empty. Losing either is recoverable by re-running setup-experiment.

**Classification: these stay under `reference/compose/` and are NOT migrated under `${EDEN_EXPERIMENT_DATA_ROOT}`.** Two reasons: (1) the cidfile dir is load-bearing for the DooD wrap design, which requires the host-side AND container-side paths to be identical (so `docker run --cidfile <path>` resolves on the daemon side to a path that exists on the worker host's filesystem — see AGENTS.md's "Three load-bearing wiring traps" pitfall). Moving the cidfile path requires touching every place that path is computed and propagated. (2) The gitea-creds dir contains only regenerable bootstrap config; it doesn't satisfy "protocol-owned state that must survive substrate restart." A re-run of setup-experiment regenerates it.

Document both in the operator doc as bootstrap-only directories not part of the durable substrate. If the source-tree-pollution becomes operationally unpleasant, a future chunk can relocate them under `${EDEN_EXPERIMENT_DATA_ROOT}/bootstrap/` — out of scope here; not load-bearing for durability.

**Overlay propagation.** The base [`compose.yaml`](../../reference/compose/compose.yaml) conversion is necessary but not sufficient. The two overlay files reference the same volumes:

- [`compose.subprocess.yaml`](../../reference/compose/compose.subprocess.yaml) — adds `eden-worktrees` (ephemeral, stays named volume — no change needed) and re-declares the executor/evaluator container's `eden-executor-repo` / `eden-evaluator-repo` / `eden-artifacts-data` mounts as short-form `name:target` strings (L101-110, L155-157). These short-form lines need to migrate to the new bind-mount shape so `compose -f compose.yaml -f compose.subprocess.yaml up` resolves the executor/evaluator paths to the bind-mount sources from the base file. **NB:** Compose overlay merge for `volumes:` is *additive at the list level*, NOT a string-substitution — declaring a volume in the base AND the overlay results in *both* being mounted. The overlay's volume entries therefore need to either be removed (if the base entry already covers them) or rewritten as bind-mount sources matching the base. Audit each overlay-volume line during impl.
- [`compose.docker-exec.yaml`](../../reference/compose/compose.docker-exec.yaml) — the worker host CLI gains `--exec-volume <name>:<target>` and `--exec-bind <host-path>:<target>` flags forwarded to spawned children as `docker run --mount source=<...>` (see [`reference/services/_common/src/eden_service_common/container_exec.py`](../../reference/services/_common/src/eden_service_common/container_exec.py) and the docker-exec wrap design at chunk-10d follow-up A). Today's overlay forwards three durable volumes by name (L110-115, L168-171):
  - `--exec-volume eden-executor-repo:/var/lib/eden/repo` (executor-host)
  - `--exec-volume eden-artifacts-data:/var/lib/eden/artifacts:ro` (executor-host)
  - `--exec-volume eden-evaluator-repo:/var/lib/eden/repo` (evaluator-host)

  Once those volumes become bind-mounts, the `--mount source=<name>` resolution silently creates a *fresh empty named volume* on the host daemon (the exact trap documented in AGENTS.md's "name: discipline" pitfall for DooD wraps). The fix is to switch each of these from `--exec-volume <name>:<target>` to `--exec-bind ${EDEN_EXPERIMENT_DATA_ROOT}/<subdir>:<target>` so the spawned child's mount source is the same host filesystem path the worker host sees. The fourth forwarded mount, `--exec-volume eden-worktrees:/var/lib/eden/worktrees`, stays unchanged (eden-worktrees remains a named volume per the ephemeral table above).

The `${EDEN_EXPERIMENT_DATA_ROOT}` env var is populated by setup-experiment.sh (resolved to absolute path before being written to `.env`) so all three compose files do straight `${VAR}` interpolation without needing shell expansion of `$HOME` (which compose does not perform).

Two reservations:

- **`eden-blob-data` is dead.** [`reference/compose/compose.yaml`](../../reference/compose/compose.yaml) at L78-84 declares a `blob-init` busybox service that mounts it; no other service in the file references it. The blob-init service exists as a `compose up --wait` ergonomics fix (forces postgres/gitea to wait on a `service_completed_successfully` dependency, so `--wait` doesn't fail on infrastructure that exits early). The volume itself holds no protocol-owned state — confirm via a one-grep `grep -rn 'eden-blob-data\|/var/lib/eden/blobs' reference/` before deleting. If the grep returns only the blob-init service, the volume can be removed entirely. If a real consumer surfaces, convert it to a bind-mount and keep `blob-init` for the `--wait` ergonomics.

- **`eden-repo-init-staging` and `eden-worktrees` stay named volumes by design.** Both hold scratch state (bootstrap-staging and per-task worktrees, respectively) and existing code already assumes they don't survive — setup-experiment.sh L445 `docker volume rm`s the staging volume on re-seed; worker hosts run `git worktree remove --force` at startup. Declaring them as durable bind-mounts would conflict with those discard semantics. Document the classification in a comment block at the top of the compose.yaml `volumes:` section so the durable-vs-ephemeral boundary is auditable.

### D.4 Setup-experiment changes

Two additions to [`reference/scripts/setup-experiment/setup-experiment.sh`](../../reference/scripts/setup-experiment/setup-experiment.sh):

1. **New flag `--data-root <path>`.** Resolves to an absolute path (`cd "$path" && pwd` for an existing path, or `mkdir -p` first and then resolve). Defaults to `${HOME}/.eden/experiments/${EXPERIMENT_ID}` if unset. Writes `EDEN_EXPERIMENT_DATA_ROOT=<abs>` into the generated `.env`.

2. **Directory tree creation.** Before `compose build` / `compose up`, create the substrate subdirectory tree:

   ```text
   ${DATA_ROOT}/postgres/
   ${DATA_ROOT}/gitea/
   ${DATA_ROOT}/artifacts/
   ${DATA_ROOT}/orchestrator-repo/
   ${DATA_ROOT}/executor-repo/
   ${DATA_ROOT}/evaluator-repo/
   ${DATA_ROOT}/web-ui-repo/
   ${DATA_ROOT}/credentials/orchestrator/
   ${DATA_ROOT}/credentials/ideator/
   ${DATA_ROOT}/credentials/executor/
   ${DATA_ROOT}/credentials/evaluator/
   ${DATA_ROOT}/credentials/web-ui/
   ```

   Permissions: see §8.1 below — there are multiple container uids (postgres=70, gitea-rootless=1000, eden:1000). The current cidfile dir uses `chmod 0777` (acknowledged in setup-experiment.sh L240-243) as a known-pragmatic shortcut for the multi-uid problem. Same posture here — `chmod 0777` on each newly-created subdirectory, with an explanatory comment. Local-dev only; production deployments use Phase 13's managed substrates.

3. **`--data-root` idempotency.** Re-running setup-experiment on an already-configured experiment preserves the existing `EDEN_EXPERIMENT_DATA_ROOT` from `.env` (same `read_env_key` pattern the script uses for secrets). Operator can change it explicitly with `--data-root`; otherwise the prior root sticks. If a passed `--data-root` differs from the existing one in `.env` and any of the substrate subdirs are non-empty, the script aborts with an instruction to migrate manually — never silently relocates substrate data.

### D.5 Operator docs (`docs/operations/experiment-data-durability.md`)

PR #80 introduces `docs/operations/` with a README, `dispatch-mode.md`, `multi-orchestrator.md`, `reassign.md`, `initial-admin-credential.md`. This chunk adds `experiment-data-durability.md` alongside. The doc structure (after rebase against #80):

- **Where experiment data lives.** Tree under `~/.eden/experiments/<id>/`, one subdirectory per substrate.
- **Durability posture.** What survives (process restart / `docker compose stop` / `docker compose down` without `-v` / host reboot / Docker Desktop restart / Docker Desktop reset-to-factory). What does not survive (`docker compose down -v` / explicit `rm -rf` / disk failure / macOS reformat). Cross-reference to chapter 01 §13.
- **Custom data root.** How to use `--data-root <path>` to relocate the substrate tree (Time-Machine-backed location, external drive, encrypted home, …).
- **Migration from named volumes.** One-shot recipe for operators with existing Compose experiments on Docker named volumes:

  ```bash
  docker compose --env-file .env stop
  ROOT=~/.eden/experiments/$EXPERIMENT_ID
  mkdir -p "$ROOT"/{postgres,gitea,artifacts,orchestrator-repo,executor-repo,evaluator-repo,web-ui-repo,credentials/{orchestrator,ideator,executor,evaluator,web-ui}}
  chmod -R 0777 "$ROOT"
  # Repeat for each substrate. Example for postgres:
  docker run --rm -v eden-postgres-data:/from -v "$ROOT/postgres":/to alpine cp -a /from/. /to/
  # ... etc for gitea, artifacts, each *-repo, each */credentials/*
  # Update setup-experiment-generated .env to set EDEN_EXPERIMENT_DATA_ROOT=$ROOT
  # (Or re-run setup-experiment.sh --data-root "$ROOT" — preserves secrets)
  docker compose --env-file .env up -d --wait
  ```

- **Pointer to Phase 13.** For production-grade durability (managed Postgres / S3-GCS / hardened Gitea), reference the Phase 13c/d/e roadmap.

### D.6 Roadmap + AGENTS.md

- `docs/roadmap.md`: a one-line entry under the Phase 12 section noting that 12a-1g ships the experiment-durability invariant + Compose bind-mounts.
- `AGENTS.md` "Current phase" section: replace the existing 12a-1 / 12a-2 / 12a-1b paragraph with a 12a-1g paragraph (or append, depending on what's landed by rebase time). One-paragraph summary following the existing prose conventions.

## 4. Scope

In scope:

- One new normative section in [`spec/v0/01-concepts.md`](../../spec/v0/01-concepts.md) (§13 "Experiment durability"), with the current §13 renumbered to §14.
- Cross-references added in [`spec/v0/01-concepts.md`](../../spec/v0/01-concepts.md) §10 and [`spec/v0/08-storage.md`](../../spec/v0/08-storage.md) §3.
- One new conformance-group row in [`spec/v0/09-conformance.md`](../../spec/v0/09-conformance.md) §5 (deferred-scenarios row).
- [`reference/compose/compose.yaml`](../../reference/compose/compose.yaml) — substrate volumes converted from named volumes to bind-mounts; `eden-blob-data` removed if confirmed unused; `eden-repo-init-staging` retained as a named volume with explanatory comment.
- [`reference/compose/.env.example`](../../reference/compose/.env.example) — new `EDEN_EXPERIMENT_DATA_ROOT` entry with explanatory comment.
- [`reference/scripts/setup-experiment/setup-experiment.sh`](../../reference/scripts/setup-experiment/setup-experiment.sh) — new `--data-root <path>` flag; directory-tree creation; idempotent preservation across re-runs.
- New file `docs/operations/experiment-data-durability.md`.
- `docs/roadmap.md` + `AGENTS.md` cosmetic updates.

Out of scope (deferred / non-goals):

- Backup or restore tooling.
- Conformance scenario authoring for the durability invariant.
- The Helm chart (Phase 13a; uses PVCs).
- Managed-substrate work (Phase 13c/d/e).
- The portable-checkpoints concern (Phase 12b).
- Any change to per-store durability (chapter 08 §3 stays as-is).
- (The docker-exec / subprocess overlays DO change in this chunk — see §D.3 "Overlay propagation". The earlier draft incorrectly marked these as out-of-scope; the codex round-0 review caught the gap. Overlay changes are bounded: rewrite the durable `--exec-volume <name>:<target>` references to `--exec-bind ${EDEN_EXPERIMENT_DATA_ROOT}/<subdir>:<target>` plus audit the subprocess overlay's per-service `volumes:` lists for short-form duplicates of base-file durable mounts.)

## 5. Files to touch

**Spec (3 files):**

- [`spec/v0/01-concepts.md`](../../spec/v0/01-concepts.md) — add §13 "Experiment durability"; renumber the current §13 "Relationships" to §14; add one-line forward-reference in §10.
- [`spec/v0/08-storage.md`](../../spec/v0/08-storage.md) — add one-line cross-reference at the top of §3 ("Per-store rules below compose to the aggregate experiment-durability invariant in chapter 01 §13").
- [`spec/v0/09-conformance.md`](../../spec/v0/09-conformance.md) — add "Experiment durability" row to §5 v1 scenario index with deferred-scenarios annotation.

**Compose + setup (5 files):**

- [`reference/compose/compose.yaml`](../../reference/compose/compose.yaml) — convert 12 durable named volumes to bind-mounts under `${EDEN_EXPERIMENT_DATA_ROOT}`; retain `eden-repo-init-staging` as a named volume with explanatory comment; remove `eden-blob-data` if confirmed unused (with the dead-blob grep check per §8.3).
- [`reference/compose/compose.subprocess.yaml`](../../reference/compose/compose.subprocess.yaml) — audit per-service `volumes:` lists (L101-L114, L155-L161) and rewrite any short-form named-volume mount whose name resolves to a durable substrate (executor-repo, evaluator-repo, artifacts-data) to either be removed (if the base file already covers it after the bind-mount conversion) or rewritten as a bind-mount line consistent with the base file. `eden-worktrees` lines stay (overlay-declared ephemeral named volume).
- [`reference/compose/compose.docker-exec.yaml`](../../reference/compose/compose.docker-exec.yaml) — rewrite the three `--exec-volume <durable-name>:<target>` CLI flags forwarded to spawned children (executor-host: `eden-executor-repo`, `eden-artifacts-data`; evaluator-host: `eden-evaluator-repo`) to `--exec-bind ${EDEN_EXPERIMENT_DATA_ROOT}/<subdir>:<target>[:<ro|rw>]` so the spawned-child mount source resolves to the same host-side bind-mount path the worker host sees. `--exec-volume eden-worktrees:/var/lib/eden/worktrees` stays (ephemeral named volume).
- [`reference/compose/.env.example`](../../reference/compose/.env.example) — add `EDEN_EXPERIMENT_DATA_ROOT=` entry with explanatory comment under a new "Substrate data root" section.
- [`reference/scripts/setup-experiment/setup-experiment.sh`](../../reference/scripts/setup-experiment/setup-experiment.sh) — parse `--data-root`; resolve absolute path; create substrate subdirectory tree with `chmod 0777`; write `EDEN_EXPERIMENT_DATA_ROOT=<abs>` to `.env`; preserve across re-runs; abort-on-incompatible-relocation guard.

**Smoke scripts (4 files):**

Each script gets the same shape: a per-script `SMOKE_DATA_ROOT="$(mktemp -d -t eden-smoke-XXXXXX)"`, a `trap 'rm -rf "$SMOKE_DATA_ROOT"' EXIT` at the top so the bind-mount tree is cleaned up on every exit path (success, failure, signal), and an invocation `setup-experiment.sh --data-root "$SMOKE_DATA_ROOT"` so smoke runs don't pollute `~/.eden/experiments/`. The existing `docker volume rm` cleanup loops become `rm -rf` on the bind-mount paths (plus the named volumes `eden-repo-init-staging` and `eden-worktrees` that survive the conversion).

- [`reference/compose/healthcheck/smoke.sh`](../../reference/compose/healthcheck/smoke.sh) — scripted-mode smoke. Drop `eden-blob-data` from the volume-cleanup loops (L21, L42, L55, L69, L98 per the `grep -n blob-data` audit) if the volume is deleted from compose.yaml.
- [`reference/compose/healthcheck/smoke-subprocess.sh`](../../reference/compose/healthcheck/smoke-subprocess.sh) — subprocess-mode smoke; exercises the `compose.subprocess.yaml` overlay; same `mktemp -d` + `trap` + blob-data-drop posture.
- [`reference/compose/healthcheck/smoke-subprocess-docker.sh`](../../reference/compose/healthcheck/smoke-subprocess-docker.sh) — **first-class validation surface for this chunk's `compose.docker-exec.yaml` rewrites.** Exercises the DooD wrap's `--exec-volume` / `--exec-bind` forwarding paths end-to-end. Must be in the §7 validation gate set since this chunk edits the docker-exec overlay.
- [`reference/compose/healthcheck/e2e.sh`](../../reference/compose/healthcheck/e2e.sh) — Web UI ideator + admin-reclaim + termination drills; same posture.

(No durability assertions added to smokes in this chunk; that's deferred per operator decision §2.4.)

**Docs (3 files):**

- `docs/operations/experiment-data-durability.md` (new) — operator-facing durability documentation per §D.5.
- [`docs/operations/README.md`](../../docs/operations/README.md) — add link to the new doc (file is introduced by PR #80; this is a post-rebase edit).
- [`docs/roadmap.md`](../../docs/roadmap.md) — one-line entry under Phase 12.
- [`AGENTS.md`](../../AGENTS.md) — Current phase paragraph.

Approximate diff size: ~150 lines spec, ~80 lines compose.yaml (rewrite of volumes block), ~40 lines compose.subprocess.yaml + compose.docker-exec.yaml (volume-flag rewrites), ~70 lines setup-experiment.sh, ~200 lines new operator doc, ~30 lines smoke-script audit, ~20 lines roadmap/AGENTS/.env.example. Total ~590 lines added, ~40 lines removed.

## 6. Test design

The chunk has no new runtime code, so most validation is by static checks + the existing CI suite continuing to pass after the substrate-mount shape changes.

**Automated (must pass before push):**

- `markdownlint-cli2` — every touched markdown file is clean.
- `python3 scripts/spec-xref-check.py` — every new §-ref in the chapter-01 / chapter-08 / chapter-09 changes resolves; the renumber of chapter 01 §13 → §14 must update every existing cross-reference to chapter 01 §13 anywhere in the repo (grep + audit).
- `check-jsonschema` metaschema — unchanged; runs as usual.
- `check-rename-discipline.py` — no new identifier renames, but the script gates the chunk.
- `uv run ruff check . && uv run pyright && uv run pytest -q` — no new Python code, so failures here would indicate accidental damage to existing code via setup-experiment's regenerated `.env` shape. Should pass with no edits.
- `uv run pytest -q conformance/` + `uv run python conformance/src/conformance/tools/check_citations.py` — chapter 09 §5 gains a new group row, which the citation-check script may surface as expected-failure if any scenarios are mis-classified into it. Since no scenarios cite chapter 01 §13 in this chunk, the new group should remain empty without tripping the script.

**Compose smokes (must pass before push):**

- `bash reference/compose/healthcheck/smoke.sh` — scripted-mode end-to-end. Must pass with the new bind-mount shape and prove the new directory tree is created correctly. Smoke uses its own `mktemp -d` data root via the §5 pattern (so smoke runs are isolated from operator's real experiments).
- `bash reference/compose/healthcheck/smoke-subprocess.sh` — subprocess-mode (`compose.subprocess.yaml` overlay). Same data-root pattern; covers the subprocess overlay's `volumes:` short-form audit.
- `bash reference/compose/healthcheck/smoke-subprocess-docker.sh` — docker-exec mode (`compose.docker-exec.yaml` overlay). **Load-bearing for this chunk** because it's the only validation surface that exercises the `--exec-volume` → `--exec-bind` rewrites end-to-end (each `*_command` spawn in a sibling DooD container; if the bind-forwarding regresses to a fresh empty volume, the spawned child sees a missing repo / artifacts dir).
- `bash reference/compose/healthcheck/e2e.sh` — full UI + admin reclaim drill. Same data-root pattern.

The plan does NOT add a new CI job in this chunk. The existing `compose-smoke*` jobs cover the bind-mount path transparently — they `setup-experiment.sh` then `compose up`; whether the substrate is a named volume or a bind-mount is invisible to them.

**Manual smoke (durability-relevant, not automated):**

A "kill-and-restart" check the operator runs once locally to verify the durability claim. This recipe deliberately does NOT call `smoke.sh` — that script tears down the stack on `EXIT` and runs against a per-invocation `mktemp` env file, neither of which compose with an inter-restart durability inspection. Instead the recipe drives `setup-experiment.sh` + `compose up/down` directly with a stable data root and env file:

```bash
ROOT="$(mktemp -d -t eden-durability-XXXXXX)"
ENV_FILE="$ROOT/.env"
EID="eden-durability-check"
trap 'docker compose --env-file "$ENV_FILE" -f reference/compose/compose.yaml down -v >/dev/null 2>&1 || true; rm -rf "$ROOT"' EXIT

# 1. Bootstrap with stable data root + env file.
bash reference/scripts/setup-experiment/setup-experiment.sh \
    tests/fixtures/experiment/.eden/config.yaml \
    --experiment-id "$EID" \
    --data-root "$ROOT" \
    --env-file "$ENV_FILE"

# 2. Run to quiescence. Poll for the orchestrator's clean exit so the
#    pre-restart state is deterministic (all seeded ideation tasks
#    integrated to variants). Without this wait, step 6 would be
#    validating restart persistence of bootstrap-only state, not the
#    full post-quiescence experiment.
docker compose --env-file "$ENV_FILE" -f reference/compose/compose.yaml up -d --wait
until [[ "$(docker inspect -f '{{.State.Status}}' eden-orchestrator 2>/dev/null)" == "exited" ]]; do
    sleep 2
done

# 3. Stop without -v so bind-mounts survive.
docker compose --env-file "$ENV_FILE" -f reference/compose/compose.yaml down   # NO -v

# 4. Verify the substrate tree is still populated on the host filesystem.
ls "$ROOT/postgres" "$ROOT/gitea" "$ROOT/artifacts"

# 5. Bring the stack back up against the same data root + env file.
docker compose --env-file "$ENV_FILE" -f reference/compose/compose.yaml up -d --wait

# 6. Confirm worker registry survived. With the default compose stack
#    there are five worker hosts: orchestrator + ideator + executor +
#    evaluator + web-ui. Each self-registers at startup.
TOKEN="$(sed -n 's/^EDEN_ADMIN_TOKEN=//p' "$ENV_FILE")"
curl -sS -H "Authorization: Bearer admin:$TOKEN" \
    -H "X-Eden-Experiment-Id: $EID" \
    "http://localhost:${TASK_STORE_HOST_PORT:-8080}/v0/experiments/$EID/workers" \
  | jq '.items | length'   # expect ≥5
```

The trap cleans up at exit; for a longer-lived investigation, the operator skips the trap and `rm -rf "$ROOT"` manually when done. Document this in `docs/operations/experiment-data-durability.md` and run it once locally as part of impl validation. Codification as a CI job is the natural Phase-12a-1g-followup if it proves useful; not in scope here per operator decision §2.4.

## 7. Verification gates

Run before any push from impl branch:

```text
uv sync
uv run ruff check .
uv run pyright
uv run pytest -q
uv run pytest -q conformance/
uv run python conformance/src/conformance/tools/check_citations.py
npx --yes markdownlint-cli2@0.14.0 "**/*.md" "#node_modules" "#.venv" "#docs/archive/**" "#docs/plans/review/**"
pipx run 'check-jsonschema==0.29.4' --check-metaschema spec/v0/schemas/*.schema.json
python3 scripts/spec-xref-check.py
python3 scripts/check-rename-discipline.py
bash reference/compose/healthcheck/smoke.sh
bash reference/compose/healthcheck/smoke-subprocess.sh
bash reference/compose/healthcheck/smoke-subprocess-docker.sh
bash reference/compose/healthcheck/e2e.sh
```

The `smoke-subprocess-docker.sh` run is load-bearing for this chunk because it's the only validation surface that exercises the `compose.docker-exec.yaml` `--exec-volume` → `--exec-bind` rewrites end-to-end. Skipping it would leave the durable-mount-forwarding code path uncovered, exactly the trap AGENTS.md's "Three load-bearing wiring traps" pitfall warns about.

If any smoke fails, do NOT push. Diagnose locally first. Per the AGENTS.md "Commands" section is the literal validation gate — narrowed subsets are not.

## 8. Tricky areas

### 8.1 Container-uid mismatch under bind-mounts

The four substrate containers run as different uids:

- **`postgres:16.6-alpine`** — docker-entrypoint starts as root, then `chown`s `/var/lib/postgresql/data` to the postgres user (uid 70 in alpine) and switches via `gosu`. With a bind-mount, if the host directory is owned by the operator (uid 501 on macOS, 1000 on Linux) and has restrictive permissions, the entrypoint's `chown` will succeed (it's running as root inside the container) but the data the container writes will be 70:70-owned on the host. That's tolerable; the operator will see weird ownership but the container works.
- **`gitea/gitea:1.22.6-rootless`** — runs as the `git` user (uid 1000). Cannot chown its own data dir (rootless). The bind-mount source MUST already be writable by uid 1000 OR be world-writable.
- **`eden-reference:dev`** — runs as `eden:1000` per `reference/compose/Dockerfile` L25 + L67. Same situation as gitea.
- **`busybox` blob-init service** — runs as root by default; not a concern (and the service may be removed entirely if `eden-blob-data` is dead).

The pragmatic shape used in setup-experiment for the cidfile dir (`chmod 0777`, L240-243) is the proven cross-platform recipe for this multi-uid problem in the reference local-dev deployment. Same posture here: `chmod 0777` each substrate subdirectory, document the local-dev security tradeoff, point Phase 13 at the production-grade managed-substrate path.

An alternative — `chown` to known uids — is *more secure* but *less portable*: Docker Desktop's uid-mapping layer makes the host-side uid different from what the container sees, and the right answer differs between macOS / Linux / WSL2. The cidfile dir precedent already accepted `chmod 0777` after weighing this. Reuse the same posture.

### 8.2 Postgres' PGDATA subdir convention

Some operators set `PGDATA=/var/lib/postgresql/data/pgdata` (a subdir) precisely so the docker-entrypoint can `chown` the *subdir* without touching the bind-mount parent. We do NOT need this for correctness — `postgres:16.6-alpine` running as root can chown the mount target directly — but if the smoke-with-bind-mount turns out to fail on some Docker variant, this is the fallback. Document as a §8 known-workaround, not as the default.

### 8.3 The `eden-blob-data` removal

The plan removes `eden-blob-data` (and possibly the `blob-init` service) if confirmed unused. Verify before deleting:

```bash
grep -rn 'eden-blob-data\|/var/lib/eden/blobs' reference/ docs/ spec/ tests/
```

If the only hits are the blob-init service in compose.yaml + the volume declaration, it is safely removable. If anything else references it (a test fixture, a documented op, an overlay file), the volume converts to a bind-mount and `blob-init` either retains for the `--wait` ergonomics or moves to another `service_completed_successfully` dependency target (postgres → eden-repo-init's exit, for example).

The `blob-init` service's stated purpose (compose.yaml L72-77 comment) is "ensure eden-blob-data exists at compose-up time" because "compose does NOT create an unmounted top-level volume." With bind-mounts the parent directory is created by setup-experiment, so there's no analogous problem — the `blob-init` service can be removed alongside the volume. The `--wait` ergonomics it provides (postgres + gitea depending on blob-init's `service_completed_successfully`) can be preserved by switching those `depends_on` entries to depend on each other or removed if no longer needed; this is a follow-up audit during impl.

### 8.4 Path-with-spaces / shell-special-character risk in `EDEN_EXPERIMENT_DATA_ROOT`

The default `$HOME/.eden/experiments/$EDEN_EXPERIMENT_ID` is shell-safe on every supported platform. But operator-supplied paths can introduce trouble.

- **Spaces.** `--data-root "/Users/alice/Documents/My Experiments/foo"` works on the bash side (`cd "$path" && pwd`), works in `.env` (compose treats the whole line after `=` as the value, no shell parsing), and works as the volume source in `${VAR}/postgres:/var/lib/postgresql/data` — compose tolerates spaces in the source path. Run a quick local check during impl validation.
- **Colons.** `--data-root /opt/foo:bar/eden` breaks compose's volume-mount syntax — compose uses `:` as the source/target delimiter, and a colon inside the source half mis-splits the mount declaration. setup-experiment.sh MUST reject paths containing `:` with a clear error message at flag-parse time, rather than letting the bad value reach compose. The same rule applies to relative-path components like `./..` or symlinks that resolve outside their parent — setup-experiment's `cd "$path" && pwd` normalization handles those, but a `:`-rejecting check is the cheap explicit guard.
- **Long paths.** macOS / Linux both accept ≥256-char paths; the practical risk is the postgres data directory's per-file path length blowing past `PATH_MAX` when the data root is already deep. Not a problem for the default `~/.eden/experiments/<id>/postgres/` but document in the operator doc that the data root should be a short prefix (< 100 chars is plenty of headroom).

### 8.5 Rebase against PR #80 + PR #82

PR #80 (Phase 12a-2 orchestrator-as-role) touches compose.yaml + setup-experiment.sh + .env.example heavily — adds `EDEN_ORCHESTRATOR_WORKER_ID`, `EDEN_IDEATION_POLICY_*`, `EDEN_ADMINS_INITIAL_MEMBER`, new `compose.multi-orchestrator.yaml` overlay, new smokes. It also introduces `docs/operations/` with a README and several operator playbooks.

PR #82 (Phase 12a-1b admin UI) is in the web-ui module only; no overlap with this chunk's surface.

This chunk rebases against `origin/main` at impl-start time, so whichever of #80 / #82 has merged by then is part of the base. Reflect any post-merge env-var additions in the new bind-mount block (e.g., if #80 adds new credentials volumes for additional orchestrator instances, those join the bind-mount conversion list). Don't pre-merge against #80 in the plan PR — the plan's diff stays small and the impl's rebase absorbs the merged changes.

### 8.6 Spec renumber discipline

Renumbering chapter 01 §13 "Relationships" → §14 is a one-line section-header change but every cross-reference in the repo that cites `01-concepts.md §13` must be audited and updated. Grep:

```bash
grep -rn '01-concepts.md.*§13\|chapter 01 §13\|chapter 1 §13\|concepts §13' spec/ docs/ reference/ conformance/
```

Update each hit. The `spec-xref-check.py` script catches dangling §-refs but won't catch the inverse (a §13 cite that *should* still point to "Relationships" but is now silently pointing to "Experiment durability"). Manual audit during impl.

### 8.7 Renumber risk: this is the first new §-section added to chapter 01 since the 12a-1 wave

Wave 1 of 12a-1 added §12 "Workers and groups" with the same insert-and-renumber pattern. That precedent went smoothly; the same audit shape applies here. The renumber affects only the trailing "Relationships" section, which has no §-subsections, so the audit set is small.

### 8.8 Compose overlay-merge semantics on `volumes:`

Compose's overlay merge for service-level `volumes:` is list-concatenation, not key-replacement. A `volumes:` entry in `compose.subprocess.yaml` does NOT supersede the same-target entry in `compose.yaml`; both lines are passed to docker daemon, and the second mount silently shadows the first at the same container path. (This is documented behavior; see Compose's [spec on multiple-files merging](https://docs.docker.com/compose/multiple-compose-files/extends/) — service-level `volumes` are appended, not replaced.) That matters here because the subprocess overlay declares `eden-executor-repo:/var/lib/eden/repo` and `eden-evaluator-repo:/var/lib/eden/repo` even though the base file already declares the same mount. After the bind-mount conversion in the base, leaving the overlay's named-volume line in place means each `compose -f base -f subprocess up` mounts BOTH a bind-mount and a fresh empty named-volume at `/var/lib/eden/repo`, with the second-listed entry winning. The fix is to remove the redundant lines from the overlay (the base file already covers them after conversion).

A docker-compose-v2 `config` rendering (`docker compose --env-file .env -f compose.yaml -f compose.subprocess.yaml config | grep -A2 volumes:`) makes this auditable. Run during impl to confirm each service's resolved `volumes:` list matches expectation.

### 8.9 `eden-worktrees` is shared between executor-host and evaluator-host

The compose.subprocess.yaml mounts `eden-worktrees:/var/lib/eden/worktrees` on both executor-host and evaluator-host services so the evaluator can read the worktree the executor wrote. That sharing constraint is what justifies a named volume (or any other shared filesystem); the worktrees stay ephemeral (per-task scratch) but must be visible to both hosts. Switching to per-host bind-mounts would break the shared-read pattern. Keep `eden-worktrees` as a named volume with shared mount.

## 9. Risks / things to watch

- **`chmod 0777` security pushback.** Codex-review may flag the world-writable substrate directories. The cidfile-dir precedent already accepted this posture for local-dev (setup-experiment.sh L240-243). The plan documents the tradeoff explicitly and points Phase 13 at the production-grade alternative. If review pushes back hard, the fallback is `chown` to a documented per-platform uid list — more correct but less portable. Hold the line on `chmod 0777` unless review surfaces a deployment scenario where it breaks (e.g., a corporate-managed macOS that disallows world-writable directories).
- **Smoke regression from the bind-mount switch.** Postgres / gitea / eden services have different uid postures, and a bind-mount that "works on my Mac" may break on the CI Linux runner. The plan's smoke gate (run all four smoke scripts before push — `smoke.sh`, `smoke-subprocess.sh`, `smoke-subprocess-docker.sh`, `e2e.sh`) is the catch.
- **`$HOME` expansion in compose.yaml.** Compose does NOT shell-expand `$HOME`; the env var must be pre-resolved by setup-experiment.sh into an absolute path. If an operator hand-edits `.env` to put `$HOME` literally, compose interpolation will write a substrate to a directory literally named `$HOME`. Setup-experiment writes the resolved abs path, so the default path works; the operator-hand-edited case is operator error and gets a one-line note in the operator doc.
- **Migration friction for in-flight experiments.** EDEN is pre-user-deployment-base, so the operator's articulated requirement is to ship the new durability shape now and document migration as a one-shot recipe. If a real user-deployment had emerged in the meantime, this calculus would change. Verify with operator at codex-review time that no production experiment depends on the named-volume shape.
- **Spec invariant interpreted as a control-plane mandate.** The earlier draft of §13 spoke of "until an authorized operator explicitly terminates it"; that framing was retired in favor of anchoring lifetime to chapter 01 §1's existing termination conditions (codex round 0 / round 1). The current §13 reads "until the experiment reaches a terminal state per its configured termination conditions (§1) or is otherwise discarded by an authorized operator" — the operator-discard clause is informational (handles non-spec deletion paths like operator-decided shutdown) and does NOT name a control-plane operation, so it doesn't beg a future Phase 12 chapter to be co-shipped.
- **PR #80 absorbs setup-experiment in ways that conflict with `--data-root` parsing.** Reviewing PR #80's setup-experiment.sh diff (already merged for the 12a-2 surface) before impl-start is required. The plan's flag-parsing additions are at the existing arg-parser, easy to absorb mechanically.

## 10. Sequence within the chunk

Execution shape (single impl PR, no multi-wave split needed at this scope):

1. **Rebase impl branch against `origin/main`** at impl-start. Resolve any conflicts surfaced by PR #80 / #82 merges.
2. **Spec amendments first.** Author chapter 01 §13, audit existing §13 cross-references, update chapter 08 §3 cross-reference, add chapter 09 §5 row. Run `spec-xref-check.py`, `markdownlint-cli2`.
3. **Compose + setup-experiment second.** Update compose.yaml volumes block; remove `eden-blob-data` after grep verification; add `EDEN_EXPERIMENT_DATA_ROOT` to .env.example; add `--data-root` to setup-experiment.sh with directory creation + idempotency + relocation guard.
4. **Operator doc + roadmap + AGENTS.md third.** Author the operator doc; update roadmap and AGENTS.md.
5. **Validation gates.** Run the full validation suite per §7. Address any failures. Re-run smokes if any of compose.yaml / setup-experiment.sh changes after a fix.
6. **Manual durability smoke.** Run the §6 "kill-and-restart" check locally once and confirm worker registry survives.
7. **Codex-review to convergence.** Plan PR's codex-review may surface plan-level concerns (revisited section ordering, deferred-conformance scope); impl PR's codex-review surfaces impl-level concerns (volume-block correctness, chmod posture, migration recipe accuracy). Iterate until no substantive findings remain.
8. **Open impl PR.** Body: spec amendment summary, Compose-binding changes, migration story, test-plan checklist (verification gates), codex round count.

The plan PR and impl PR are separate surfaces per the chunk shape; operator merges each after codex convergence.

## 11. Out of scope (followups)

- **Conformance scenarios for the durability invariant.** A "stop-stack / kill-volume-mount / start-stack / replay" harness against any conforming IUT could codify the aggregate-durability MUST. Deferred per operator decision; chapter 9 §5 placeholder row is forward-compatible with this followup.
- **Backup-experiment.sh / restore-experiment.sh.** Out of scope per operator's "no backup tooling" decision.
- **Time-Machine / encrypted-home integration.** Documented in the operator doc as "use `--data-root` to relocate the substrate tree to your backup-protected location"; no codified tooling.
- **Phase 13c managed Postgres** / **Phase 13d S3-GCS blob backend** / **Phase 13e Gitea hardening** — the production-grade durability path. The spec invariant introduced here is what those phases are required to satisfy; they don't depend on this chunk shipping first, but this chunk's invariant retroactively constrains their design.
- **Helm chart bind-mount audit.** Phase 13a's chart uses PVCs (the natural k8s durability binding); no audit needed unless a PVC-vs-emptyDir mistake surfaces. If a future audit finds an `emptyDir` mount holding protocol-owned state, that's a 13a fix-it triggered by this chunk's invariant — out of scope here.
- **Removal of the `blob-init` service.** If §8.3's audit confirms `eden-blob-data` is dead, removing the service is in scope; if it isn't, the cleanup is its own followup. The plan handles either branch.
- **CI codification of the kill-and-restart smoke.** Out of scope per operator's deferred-conformance decision. Natural followup if the manual check proves useful operationally.

## 12. Estimated effort

| Activity | Estimate |
|---|---|
| Spec amendments (chapter 01 §13 + cross-refs + chapter 09 row) | ~0.5 day |
| compose.yaml volume conversion + blob-init audit + .env.example | ~0.5 day |
| setup-experiment.sh `--data-root` flag + directory creation + idempotency + tests | ~0.5 day |
| Operator doc (`experiment-data-durability.md`) | ~0.5 day |
| Validation gates (lint, type, conformance, smokes including the manual kill-and-restart) | ~0.5 day |
| Codex-review iterations (plan + impl, ~2 rounds each) | ~1 day |
| **Total** | **~3.5 days** |

Smaller than the typical chunk (12a-1's six waves landed in ~10 days) because the scope is narrow and most surfaces are touched lightly. Codex-review iteration is the dominant variable.
