# Issue #131 — Automatic checkpointing (cadence + on-terminate)

**Status.** Draft.

**Issue.** [#131](https://github.com/ealt/eden/issues/131) — Automatic
checkpointing (cadence + on-terminate; optional safety net). Cluster
`durability`, milestone "Production hardening".

**Predecessors.** Phase 12b shipped the portable-checkpoint wire surface
([`docs/plans/eden-phase-12b-portable-checkpoints.md`](eden-phase-12b-portable-checkpoints.md)):
`POST /v0/experiments/<id>/checkpoint` (export) and
`POST /v0/checkpoints/import` (import), the `eden_checkpoint` format
package, and `Store.export_checkpoint` / `import_checkpoint`. This chunk
is a **consumer** of that surface — it adds *nothing* to the checkpoint
format and *nothing* normative to the protocol. It makes checkpoints
*happen automatically* so the operator's "I can accept interruptions,
teardown, redeployment, imperfect checkpoints" posture is viable
**without operator action**.

**Design framing (AWS-MVP).** Auto-checkpointing is the mechanism that
turns "checkpoints exist" into "checkpoints are happening regularly."
Two triggers, per the issue: **cadence** (every N seconds) and
**on-terminate** (the experiment reaching `state == "terminated"`).
Restoration stays operator-driven (no auto-restore — too magical, per
the issue).

---

## 1. Naming map

Pre-draft check against [`docs/glossary.md`](../glossary.md) and AGENTS.md
"Naming discipline". "Checkpoint" is the established noun (12b);
"export" is the verb. No new role/verb/kind vocabulary is introduced;
all new identifiers are deployment-mechanism names. `check-rename-discipline.py`
is run before submit as a backstop.

| Identifier | Kind | Where | Notes |
|---|---|---|---|
| `auto_checkpoint` | experiment-config block (object) | `experiment-config.schema.json` + `eden_contracts.config` | Opt-in; `enabled` defaults `false`. Implementation-defined deployment behavior (like `max_quiescent_iterations`), NOT normative protocol. |
| `auto_checkpoint.enabled` | bool, default `false` | config block | Master switch. |
| `auto_checkpoint.interval_seconds` | number > 0, default `3600` | config block | Cadence. **Seconds, not minutes** — see Decision D2. |
| `auto_checkpoint.retention_count` | int ≥ 1, default `6` | config block | Ring-buffer depth for periodic checkpoints. |
| `auto_checkpoint.on_terminate` | bool, default `true` | config block | Whether to take a terminal checkpoint when the experiment terminates. |
| `AutoCheckpointConfig` | Pydantic model | `eden_contracts/config.py` | Mirrors the schema block; `strict=True`, `NotNone`-wrapped optional field on `ExperimentConfig`. |
| `--auto-checkpoint-dir` / `EDEN_AUTO_CHECKPOINT_DIR` | orchestrator CLI flag / env | `eden_orchestrator/cli.py` | Destination directory (deployment path concern — NOT in the config block; see Decision D3). |
| `CheckpointScheduler` | class | `eden_orchestrator/checkpoint_scheduler.py` (new) | Owns interval timing, retention pruning, terminal-once flag, and the export call. |
| periodic checkpoint filename | `<experiment_id>-<YYYYMMDDTHHMMSSZ>.tar` | destination dir | `.tar` (not `.tar.gz`) — matches `application/x-eden-checkpoint+tar`; compression is out of scope per the issue. |
| terminal checkpoint filename | `<experiment_id>-terminated-<YYYYMMDDTHHMMSSZ>.tar` | destination dir | Kept outside the retention ring. |
| `smoke-auto-checkpoint.sh` | compose smoke | `reference/compose/healthcheck/` | Mirrors `smoke-checkpoint.sh` phases 1–3. |
| `compose-smoke-auto-checkpoint` | CI job | `.github/workflows/ci.yml` | Runs the new smoke. |

---

## 2. Decisions

These are the load-bearing design calls; §3 unpacks each.

### D1 — Runs inside the single-experiment orchestrator loop

The issue floats orchestrator vs. a dedicated `auto-checkpointer` sibling
container. **Chosen: the orchestrator's single-experiment loop**
([`run_orchestrator_loop`](../../reference/services/orchestrator/src/eden_orchestrator/loop.py)).
It is already a long-running per-experiment process with a polling loop,
already holds a `StoreClient`, and adding an "is it time to checkpoint?"
check after each iteration is cheap. A sibling container is more
infrastructure for no v0 benefit and would need its own credentials +
config plumbing. **Multi-experiment / control-plane mode
([`multi_loop.py`](../../reference/services/orchestrator/src/eden_orchestrator/multi_loop.py))
is explicitly out of scope** — it resolves per-experiment config
differently (deferred to [#214](https://github.com/ealt/eden/issues/214))
and would need a per-lease scheduler. Deferred to a follow-up (§4.2).

### D2 — Cadence is `interval_seconds`, not `interval_minutes`

The issue proposes `interval_minutes`. **Chosen: `interval_seconds`**
(default `3600` = 60 min). Rationale: every existing time-valued
experiment-config knob is in **seconds** —
`ideation_task_deadline` / `execution_task_deadline` /
`evaluation_task_deadline` (all `number`, seconds, see
[`experiment-config.schema.json`](../../spec/v0/schemas/experiment-config.schema.json)
§§188–202) and the orchestrator's `poll_interval`. A lone `_minutes`
key would be the only minutes-denominated field in the surface and a
latent foot-gun (operator reads `interval: 30` as 30s). Coherence with
the existing config vocabulary wins; the docs spell out the
seconds→minutes conversion for operators.

### D3 — Destination is a deployment path (CLI/env), not a config field

The issue puts `destination` in the `auto_checkpoint` config block.
**Chosen: destination is an orchestrator CLI flag /env
(`--auto-checkpoint-dir` / `EDEN_AUTO_CHECKPOINT_DIR`), NOT a config
field.** Rationale: the experiment-config is *portable* (it round-trips
through checkpoints and across deployments); a host filesystem path is
*deployment-local* and means nothing on a different host. The
established split is exactly this — portable intent in the config
(`max_quiescent_iterations`), deployment wiring on the CLI
(`--repo-path`, `--artifacts-dir`). Putting a host path in the portable
config would bake one deployment's layout into a portable artifact. The
config block carries only the portable *intent* (enabled / cadence /
retention / on-terminate); *where* the bytes land is deployment wiring.
Default resolves to `${EDEN_EXPERIMENT_DATA_ROOT}/checkpoints` at the
compose layer (the orchestrator just receives a resolved path).

### D4 — The orchestrator exports with the **admin bearer**, no spec change

The export endpoint is **deliberately admin-gated as "bootstrap-class"**
([`07-wire-protocol.md`](../../spec/v0/07-wire-protocol.md) §14): the
spec explicitly contrasts checkpoint export/import (admin, literal
`admin` principal) against the group-gated operational endpoints
(`terminate_experiment` etc., gated `(admins, orchestrators)` per #256).
So we do **NOT** broaden export to `orchestrators` — that would
contradict §14's stated rationale and amend the spec.

Instead, the auto-checkpointer authenticates to the export endpoint with
the **deployment admin bearer**, which the orchestrator *already
resolves at startup* (`resolve_admin_token`, used today for the
bootstrap group-join — [`cli.py`](../../reference/services/orchestrator/src/eden_orchestrator/cli.py)
§371). When `auto_checkpoint.enabled` is true the orchestrator MUST have
an admin token available; if it is absent, startup fails fast with a
clear message ("auto_checkpoint requires --admin-token / $EDEN_ADMIN_TOKEN
so the orchestrator can call the admin-gated export endpoint"). In the
compose deployment `EDEN_ADMIN_TOKEN` is already wired to the
orchestrator, so this is satisfied with no new operator action.

**Net: this chunk touches NO normative spec prose and NO wire authority
table.** Consistent with the issue ("auto-checkpoint is a deployment
behavior, not a protocol contract"). The only `spec/`-tree file touched
is `experiment-config.schema.json` (an implementation-defined config
block, like `max_quiescent_iterations`), updated in lockstep with the
Pydantic model per the schema-parity discipline.

### D5 — Best-effort: a checkpoint failure never crashes the loop

The whole point is a safety net; it must never *cause* an outage. Every
export/prune is wrapped: on any exception the scheduler logs a
structured warning and the loop continues. A failed periodic does not
advance the "last checkpoint" timer past its retry window (the next
iteration retries once the interval re-elapses) and does not count
against quiescence.

### D6 — On-terminate is observed in-loop; orphan case is documented, not solved here

The terminal checkpoint fires when the loop **observes** the experiment
transition to `terminated` (the loop already drains post-terminate, so
it has iterations to observe it — whether the transition came from its
own `termination_policy` or an admin). A final terminal-check also runs
once on the loop's exit paths. **Known gap:** if the orchestrator has
already exited (quiescence) before an admin later terminates, no
terminal checkpoint fires — the most recent periodic checkpoint is the
safety net. A server-side on-terminate hook would close this but needs
the task-store-server to own a destination + repo bundle + admin
context; deferred to a follow-up (§4.2).

### D7 — `auto_checkpoint` lives in the spec config schema (precedent), kept green by parity

`experiment-config.schema.json` is under `spec/v0/schemas/`, but it
already carries implementation-defined knobs (`max_quiescent_iterations`,
the `*_task_deadline` trio) whose descriptions say "Implementation-defined."
Adding `auto_checkpoint` there — with a description that pins it as
deployment behavior with no protocol role — follows that precedent and
gives operators schema validation. The alternative (flow it only through
`extra="allow"` and parse it ad hoc in the orchestrator) would break
schema-parity the moment we want to *reject* a bad value (e.g.
`interval_seconds: -1`): the model would reject, the schema would accept,
and the `schema-parity` job would fail. So it goes in **both** the schema
and the Pydantic model, in lockstep, with accept/reject corpus fixtures.

---

## 3. Design

### 3.1 Config surface (Wave 1)

Add to [`experiment-config.schema.json`](../../spec/v0/schemas/experiment-config.schema.json)
(inserted after `baseline`, before `max_quiescent_iterations`):

```jsonc
"auto_checkpoint": {
  "type": "object",
  "description": "Implementation-defined deployment behavior (NOT a protocol contract): when enabled, the reference orchestrator periodically exports a portable checkpoint (07-wire-protocol.md §14.1) and, optionally, one on experiment termination. Opt-in; absent ≡ {enabled: false}. The destination directory is deployment wiring (orchestrator --auto-checkpoint-dir), not part of this portable block.",
  "properties": {
    "enabled":         {"type": "boolean", "description": "Master switch. Default false."},
    "interval_seconds":{"type": "number", "exclusiveMinimum": 0, "description": "Cadence in seconds (matches the *_task_deadline seconds convention). Default 3600."},
    "retention_count": {"type": "integer", "minimum": 1, "description": "Ring-buffer depth for periodic checkpoints; oldest beyond this are pruned. Default 6. Does not bound terminal checkpoints."},
    "on_terminate":    {"type": "boolean", "description": "Take a terminal checkpoint when the experiment reaches state=terminated. Default true."}
  },
  "additionalProperties": false
}
```

`AutoCheckpointConfig` in
[`eden_contracts/config.py`](../../reference/packages/eden-contracts/src/eden_contracts/config.py)
mirrors it (`model_config = ConfigDict(strict=True, extra="forbid")` to
match `additionalProperties: false`; check sibling models for the house
style — `BaselineConfig` is the closest analog), with reference defaults
applied by the orchestrator (not baked into the schema as `default`, to
match how the existing knobs leave defaults to the impl). `ExperimentConfig`
gains `auto_checkpoint: Annotated[AutoCheckpointConfig | None, NotNone] = None`.

Corpus fixtures in
[`eden-contracts/tests/cases.py`](../../reference/packages/eden-contracts/tests/cases.py):
at least one accept (full block) and reject fixtures per constraint
(`interval_seconds: 0`, `retention_count: 0`, unknown key, wrong types).

### 3.2 `CheckpointScheduler` (Wave 2 — pure logic)

New module `eden_orchestrator/checkpoint_scheduler.py`. Constructed from
the resolved `AutoCheckpointConfig` + destination `Path` + an admin-authed
export callable + an injectable `now_fn` (monotonic, for deterministic
tests). Responsibilities — all pure enough to unit-test without the loop:

- `maybe_checkpoint_periodic(store, *, wall_now)` — if
  `monotonic_now - last_at >= interval_seconds`, export to
  `<dir>/<exp>-<wall_ts>.tar`, update `last_at`, then prune. Best-effort
  (D5): catch + log, leave `last_at` unchanged on failure so the next
  iteration retries.
- `maybe_checkpoint_terminal(store)` — if `on_terminate` and not already
  done, export to `<dir>/<exp>-terminated-<wall_ts>.tar`, set the
  done-flag. Idempotent.
- `_prune()` — list destination entries matching the **periodic** pattern
  for *this* `experiment_id` only (never the `-terminated-` files, never
  operator-added files), sort by embedded timestamp, unlink oldest beyond
  `retention_count`. Robust to unexpected files.

The export callable is `store.export_checkpoint(open(tmp, "wb"))` against
an **admin-authed** `StoreClient` (D4), written to a temp file then
`os.replace`-d into place so a partially-written `.tar` is never visible
to the operator (mirrors the credentials-file atomic-write discipline in
[`checkpoints.py`](../../reference/packages/eden-wire/src/eden_wire/routers/checkpoints.py)).

Disabled config (`enabled=false` or block absent) ⇒ a no-op scheduler
(both methods return immediately); the loop always holds one so there is
no `if scheduler is not None` branching.

### 3.3 Loop + CLI wiring (Wave 3)

[`run_orchestrator_loop`](../../reference/services/orchestrator/src/eden_orchestrator/loop.py)
gains a `scheduler: CheckpointScheduler` parameter. Per iteration, after
`run_orchestrator_iteration` returns:

1. `scheduler.maybe_checkpoint_periodic(store, wall_now=datetime.now(UTC))`.
2. Read experiment state (one cheap `store.read_experiment_state()`, same
   posture as the per-iteration `read_dispatch_mode`); if `terminated`,
   `scheduler.maybe_checkpoint_terminal(store)`.

Both `return` paths (quiescence + `stop`) funnel through a single exit
that runs a final `maybe_checkpoint_terminal` re-check (covers
terminate-then-immediate-quiescence, D6). Neither call perturbs the
`quiescent` counter.

[`cli.py`](../../reference/services/orchestrator/src/eden_orchestrator/cli.py):
new `--auto-checkpoint-dir` (env `EDEN_AUTO_CHECKPOINT_DIR`). At startup
(single-experiment path only): parse `config.auto_checkpoint`; if
`enabled`, assert an admin token is resolvable (else fail fast, D4),
build the admin-authed export client, construct the `CheckpointScheduler`,
and pass it to `run_orchestrator_loop`. When disabled, pass the no-op
scheduler. The multi-experiment entrypoint passes the no-op scheduler
unconditionally (§4.2 deferral).

### 3.4 Compose + setup wiring (Wave 4)

- [`setup-experiment.sh`](../../reference/scripts/setup-experiment/setup-experiment.sh):
  create `${DATA_ROOT}/checkpoints` alongside the other substrate
  subdirs (§§337–348), and write `EDEN_AUTO_CHECKPOINT_DIR` into `.env`.
- [`compose.yaml`](../../reference/compose/compose.yaml): bind-mount
  `${EDEN_EXPERIMENT_DATA_ROOT}/checkpoints` into the orchestrator
  container (e.g. at `/var/lib/eden/checkpoints`) and set the orchestrator's
  `EDEN_AUTO_CHECKPOINT_DIR` to that container path. (Per the AGENTS.md
  bind-mount-rename audit pitfall: grep for every consumer of the new
  path/env before merge.)
- `smoke-auto-checkpoint.sh`: mirrors `smoke-checkpoint.sh` phases 1–3
  but with a fixture config that sets `auto_checkpoint.enabled: true` and
  a short `interval_seconds`. Asserts: ≥1 periodic `.tar` appears in the
  host checkpoints dir while the experiment runs; after the orchestrator
  exits (quiescence/terminate), a `-terminated-` `.tar` exists; each `.tar`
  parses and carries `manifest.json`; the periodic count never exceeds
  `retention_count`. Bash-3.2 clean (no `mapfile`/`declare -A`) per AGENTS.md.
- `.github/workflows/ci.yml`: `compose-smoke-auto-checkpoint` job mirroring
  the existing `compose-smoke-checkpoint` job (no `setup-python` needed —
  pure docker/jq/curl). Includes the volume-cleanup-between-runs guard.

### 3.5 Docs (Wave 5)

- [`docs/user-guide.md`](../user-guide.md) §2 experiment-config authoring:
  document the `auto_checkpoint` block (fields, defaults, seconds→minutes
  note) and the `--auto-checkpoint-dir`/`EDEN_AUTO_CHECKPOINT_DIR`
  deployment knob.
- [`docs/observability.md`](../observability.md): a short "Checkpoint
  cadence" subsection — trigger model (cadence + on-terminate), retention
  ring, filename scheme, the documented orphan gap (D6), and a pointer to
  the operator-driven `eden-experiment restore` flow (restoration stays
  manual — no auto-restore).
- [`CHANGELOG.md`](../../CHANGELOG.md) `[Unreleased]` entry + roadmap
  one-liner (durability cluster, planless-shape pointer to the merged PR
  per AGENTS.md "Recording chunk completions"). Each deferral phrase in
  the entry carries its issue link.
- Glossary: confirm "checkpoint" coverage already suffices; add the
  `auto_checkpoint` block only if a reader would otherwise miss it.

---

## 4. Scope

### 4.1 In scope

- `auto_checkpoint` config block (schema + Pydantic + parity fixtures).
- `CheckpointScheduler`: cadence timer, retention ring, terminal trigger,
  best-effort failure isolation, atomic temp-file write.
- Single-experiment orchestrator loop + CLI wiring; admin-bearer export
  client; fail-fast when enabled-without-admin-token.
- Compose bind-mount + `setup-experiment.sh` dir creation + `.env` var.
- `smoke-auto-checkpoint.sh` + `compose-smoke-auto-checkpoint` CI job.
- Unit tests (scheduler + config) + loop wiring tests + docs.

### 4.2 Deferred (each tracked as a GitHub issue)

- **Multi-experiment / control-plane auto-checkpointing.** The
  `multi_loop` lease path resolves per-experiment config differently and
  needs a per-lease scheduler. Blocked on / related to per-experiment
  config resolution ([#214](https://github.com/ealt/eden/issues/214)).
  Filed as its own follow-up issue at implementation time.
- **Server-side on-terminate checkpoint hook.** Closes the D6 orphan gap
  (terminate after the orchestrator has exited). Needs the
  task-store-server to own a destination + repo bundle + admin context.
  Filed as a follow-up.
- **Checkpoint compression** (`.tar.gz`). Out of scope per the issue
  ("Compression strategy choices"). Filed if a real size motivator surfaces.

These deferral issues are filed at the moment the deferral lands in the
`CHANGELOG.md` entry (AGENTS.md "Deferrals MUST be tracked as GitHub
issues"), and the entry references each by number.

### 4.3 Out of scope (per the issue, no tracking needed)

- Cross-deployment auto-replication; continuous data protection
  (every-write capture); **auto-restore** on stack-up; encryption of
  archives (filesystem encryption handles confidentiality).

### 4.4 Non-goals

- Any change to the portable-checkpoint **format** or the wire
  endpoints' shape/semantics (12b is frozen; this is a pure consumer).
- Any normative spec prose or wire authority-table change (D4).

---

## 5. Files to touch

| Wave | File | Change |
|---|---|---|
| 1 | `spec/v0/schemas/experiment-config.schema.json` | Add `auto_checkpoint` block (§3.1). |
| 1 | `reference/packages/eden-contracts/src/eden_contracts/config.py` | `AutoCheckpointConfig` model + `ExperimentConfig.auto_checkpoint` field. |
| 1 | `reference/packages/eden-contracts/tests/cases.py` (+ existing parity/roundtrip tests) | Accept/reject corpus fixtures. |
| 2 | `reference/services/orchestrator/src/eden_orchestrator/checkpoint_scheduler.py` (new) | `CheckpointScheduler`. |
| 2 | `reference/services/orchestrator/tests/test_checkpoint_scheduler.py` (new) | Fake-clock + fake-store + tmp-dir unit tests. |
| 3 | `reference/services/orchestrator/src/eden_orchestrator/loop.py` | `scheduler` param; periodic + terminal hooks; single exit path. |
| 3 | `reference/services/orchestrator/src/eden_orchestrator/cli.py` | `--auto-checkpoint-dir`/env; admin-token assertion; scheduler construction; multi-loop passes no-op. |
| 3 | `reference/services/orchestrator/tests/` (loop tests) | Periodic-fires, terminal-fires, failure-isolation, disabled-no-op. |
| 4 | `reference/scripts/setup-experiment/setup-experiment.sh` | Create `checkpoints/` dir; write `EDEN_AUTO_CHECKPOINT_DIR`. |
| 4 | `reference/compose/compose.yaml` | Bind-mount + orchestrator env. |
| 4 | `reference/compose/healthcheck/smoke-auto-checkpoint.sh` (new) | Cadence + terminal smoke. |
| 4 | `.github/workflows/ci.yml` | `compose-smoke-auto-checkpoint` job. |
| 4 | `tests/fixtures/experiment/.eden/config.yaml` or a smoke-local config | Fixture with `auto_checkpoint.enabled: true`, short interval. |
| 5 | `docs/user-guide.md`, `docs/observability.md`, `CHANGELOG.md`, `docs/roadmap.md`, (glossary if needed) | Docs + completion record. |

---

## 6. Validation gates (per wave)

**Wave 1 (config):**

- `uv run ruff check .` / `uv run pyright` clean.
- `uv run pytest reference/packages/eden-contracts/tests/test_schema_parity.py`
  passes (model ↔ schema parity over the new fixtures).
- `uv run pytest -q reference/packages/eden-contracts`.
- `pipx run 'check-jsonschema==0.29.4' --check-metaschema spec/v0/schemas/*.schema.json`
  (the edited schema still validates against the metaschema).
- `pipx run 'check-jsonschema==0.29.4' --schemafile spec/v0/schemas/experiment-config.schema.json tests/fixtures/experiment/.eden/config.yaml`.

**Wave 2 (scheduler):**

- ruff / pyright clean.
- `uv run pytest -q reference/services/orchestrator/tests/test_checkpoint_scheduler.py`
  — interval fires at the boundary; retention prunes oldest only; terminal
  is once-only; failure is swallowed and `last_at` unchanged; disabled is
  no-op; periodic pruning ignores `-terminated-` and foreign files.

**Wave 3 (loop + CLI):**

- ruff / pyright clean.
- `uv run pytest -q reference/services/orchestrator` — loop fires the
  periodic and terminal checkpoints; enabled-without-admin-token fails
  fast; multi-loop unaffected.
- `uv run pytest -q` (full suite) — nothing else regressed.

**Wave 4 (compose + smoke):**

- `bash reference/compose/healthcheck/smoke-auto-checkpoint.sh` passes
  locally (volume-cleanup guard run first).
- `bash reference/compose/healthcheck/smoke-checkpoint.sh` still passes
  (no regression to the manual round-trip).
- `bash reference/compose/healthcheck/smoke.sh` passes (base pipeline).
- Bash-3.2 lint discipline (no `mapfile`/`declare -A`); ShellCheck clean.

**Wave 5 (docs) + pre-merge full gate (AGENTS.md "Commands" table):**

- `npx --yes markdownlint-cli2@0.14.0 "**/*.md" …` clean.
- `python3 scripts/check-rename-discipline.py` clean.
- `python3 scripts/spec-xref-check.py` clean (no broken `§N.M` refs in
  any edited spec-adjacent doc).
- `python3 scripts/check-complexity.py` clean (scheduler + loop changes
  stay under thresholds, or carry a justified `# slop-allow:`).
- Full quartet before push: lint + typecheck + `uv run pytest -q` +
  the smoke scripts (`smoke.sh`, `smoke-checkpoint.sh`,
  `smoke-auto-checkpoint.sh`) — NOT a narrowed subset (AGENTS.md).
- Manual check: bring up the compose stack with `auto_checkpoint.enabled:
  true` and a short interval; confirm periodic `.tar`s appear under the
  host checkpoints dir, terminate the experiment, confirm the
  `-terminated-` `.tar` lands and the periodic count respects retention.

---

## 7. Chunked waves (independently implementable + reviewable)

1. **Config surface.** Schema block + Pydantic model + parity fixtures.
   No behavior. Self-contained; green on its own gates.
2. **`CheckpointScheduler` (pure logic).** Module + unit tests with a
   fake clock / fake store / tmp dir. No loop wiring. Standalone.
3. **Loop + CLI wiring.** Inject the scheduler; periodic + terminal
   hooks; admin-bearer export client; fail-fast. Depends on 1 + 2.
4. **Compose + smoke + CI.** Bind-mount, `setup-experiment.sh` dir,
   `.env` var, `smoke-auto-checkpoint.sh`, CI job, fixture config.
   Depends on 3.
5. **Docs + completion record.** user-guide, observability, CHANGELOG
   `[Unreleased]`, roadmap one-liner, glossary check, deferral issues
   filed + referenced. Commits the impl-stage codex-review record under
   `docs/plans/review/issue-131-auto-checkpointing/impl/<timestamp>/`.

A reviewer going 1→5 should never see a red tree: behavior is dark until
Wave 3 wires it, and Wave 3 keeps `enabled=false` (the default) a no-op,
so existing smokes are unchanged until Wave 4 opts a fixture in.

---

## 8. Tricky areas / risks

1. **Admin-bearer plumbing (D4).** The orchestrator's primary
   `StoreClient` is worker-bearer-authed; the export call needs an
   admin-authed client. Build a second, narrowly-scoped client (or an
   admin-bearer header override) used *only* by the scheduler. Verify the
   admin bearer shape (`Bearer admin:<token>`) against
   [`auth.py`](../../reference/packages/eden-wire/src/eden_wire/auth.py)
   `parse_bearer` when implementing.
2. **Export blocks the loop thread.** The export is synchronous and holds
   a server-side read snapshot; for a large experiment it could block the
   loop for seconds. Acceptable at reference scale (read snapshots are
   cheap — 12b §7.8); threading the export off the loop is a future
   refinement, not v0.
3. **Retention pruning must be surgical.** Only manage files matching the
   periodic pattern for *this* `experiment_id`; never touch `-terminated-`
   files or operator-dropped files. A too-broad glob is a data-loss
   vector (cf. AGENTS.md "narrow exception handling" / "missing → destructive
   action" pitfalls — same posture for "this file → delete").
4. **Smoke timing.** A short `interval_seconds` must be long enough that
   the stack reaches running state before the first checkpoint yet short
   enough to fire within the smoke's quiescence budget. Follow the
   `smoke-checkpoint.sh` wait/poll discipline; never leak a dev-only
   `--max-quiescent-iterations` bump into the fixture (AGENTS.md
   cherry-pick-contamination pitfall).
5. **Disk growth.** `retention_count × checkpoint_size` per experiment;
   documented as an operator concern in observability docs.
6. **No spec drift.** Because this chunk is spec-free by design (D4/D7),
   the risk is *accidentally* introducing a spec claim. Keep
   `check-rename-discipline.py` + `spec-xref-check.py` in the gate and
   confirm the only `spec/` edit is the config schema.
