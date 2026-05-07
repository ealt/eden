---
name: eden-manual-experiment
description: 'Spin up, tear down, or reset a manual-UI EDEN experiment. Trigger phrases: "start a new experiment", "spin up an experiment", "tear down the experiment", "reset the experiment", "fresh experiment", "wipe the experiment", "clean up the stack".'
---

# EDEN Manual — Experiment Lifecycle

## When to use

The user wants to start a fresh manual-UI experiment, tear an existing
one down, or wipe and start over. Trigger phrases above. Don't use this
skill for in-experiment work (claiming, drafting, submitting) — those
are `eden-manual-{ideator,executor,evaluator}`.

## What it does

The CLI lives at:

```text
EDEN_EXP=/Users/ericalt/Documents/eden-worktrees/test-main/reference/scripts/manual-ui/eden-experiment
```

Subcommands:

- `up <config> --experiment-id <id> [--seed-from <dir>] [--with-workers] [--port <n>]`
- `down [--purge]`
- `reset <config> --experiment-id <id> [--seed-from <dir>] [--with-workers] [--port <n>]`
- `status`
- `checkpoint <name> [--force]` — snapshot postgres + gitea + artifacts + .env
- `restore <name>` — load a checkpoint into a fresh stack (requires `down` first)
- `list-checkpoints`

`up` always wipes the orphan staging volume first (issue #8 workaround
baked in). `down` always removes compose volumes; with `--purge` also
removes per-experiment files (`.env`, gitea creds, manual-CLI scratch
state under `/tmp/eden-manual`). `reset` = `down --purge` + `up`.

## Workflow: spin up a new experiment

### Phase 1: Check current state (automatic)

```bash
$EDEN_EXP status
```

If a stack is already running, ask the user before tearing it down:
"There's an experiment `<id>` already running. Tear it down or keep it
and bail?"

### Phase 2: Elicit decisions (judgment)

Ask the user — concisely, one prompt — for:

1. **Experiment id** (required). Suggest a short kebab-case name. If they
   don't care, propose one based on context (e.g., `manual-<date>` or
   `<topic>-<n>`).

2. **Experiment config** (required). Default: the fixture at
   `tests/fixtures/experiment/.eden/config.yaml`. If the user wants a
   custom objective / evaluation_schema, they need a YAML at that shape;
   if they have one, ask for the path. If not, default to the fixture
   and note that.

3. **Seed contents** (optional). Three choices:
   - Empty stub (no `--seed-from`) — every variant starts from a blank repo.
     Good for "build me a thing" experiments.
   - `--seed-from <host-dir>` — snapshot of an existing host directory.
     If the source is a git repo, honors its `.gitignore` (skips
     `.venv/`, caches, etc.); otherwise copies the whole tree minus
     `.git/`.
   - The user already has a directory in mind: confirm the path exists.

4. **Worker hosts** (default off). Ask only if they're vague: "Manual
   roles only (the default), or run the auto-claiming worker hosts
   alongside?" For pure manual play: off.

### Phase 3: Spin up (automatic)

```bash
$EDEN_EXP up <config> --experiment-id <id> [--seed-from <dir>] [--with-workers]
```

Surface the resulting status output (experiment id, seed SHA, web-ui
URL). If `--seed-from` was used, *verify the seed*:

```bash
PASS=$(grep '^GITEA_REMOTE_PASSWORD=' /Users/ericalt/Documents/eden-worktrees/test-main/reference/compose/.env | cut -d= -f2)
curl -fsS -u "eden:$PASS" \
  http://localhost:3001/api/v1/repos/eden/<experiment-id>/contents \
  | python3 -m json.tool
```

Confirm to the user that the gitea repo has the expected files.

### Phase 4: Hand-off

Tell the user the experiment is ready, the URL of the web-ui (in case
they want to peek), and which role skills they can drive next:
`/eden-manual-ideator` is the natural starting point.

## Workflow: tear down

If the user says "tear down" or "wipe":

1. `$EDEN_EXP status` to confirm what's running.
2. Ask: full purge (`--purge`) or stop with state preserved?
   - Default for "I'm done" / "wipe" / "fresh slate": `--purge`.
   - Default for "stop for now, will resume": no flag.
3. `$EDEN_EXP down [--purge]`.

## Workflow: checkpoint / restore

**Why this exists.** The full authoritative state of an experiment lives
in three docker volumes (postgres, gitea, artifacts) plus
`reference/compose/.env` and the gitea credential helper. `checkpoint`
snapshots all five into a directory; `restore` materializes them into a
fresh stack. Useful for: branching off a known-good state, rolling back
a buggy iteration, copying state between machines.

### Take a checkpoint

```bash
$EDEN_EXP checkpoint <name>
```

Stack must be running (need `pg_dump`). Gitea is briefly stopped
(seconds) for a consistent on-disk snapshot. Output goes to
`reference/compose/checkpoints/<name>/`.

The checkpoint includes:

- `postgres.sql` — pg_dump of the eden database
- `gitea.tar.gz` — full gitea data dir (repos + auth + tokens)
- `artifacts.tar.gz` — artifacts volume
- `env` — copy of `.env` (carries secrets and experiment-id)
- `experiment-config.yaml`
- `gitea-creds/` — per-experiment credential helper
- `metadata.json`

### List checkpoints

```bash
$EDEN_EXP list-checkpoints
```

### Restore from a checkpoint

```bash
$EDEN_EXP down --purge      # required first; stack must be down
$EDEN_EXP restore <name>
```

This wipes the volumes (if any leftover), materializes them from the
snapshot, replays the postgres dump, and brings up the non-worker
services. Worker repo clones are rebuilt automatically from gitea on
service start.

### When to checkpoint

- Before experimenting with a destructive action you might want to roll
  back from.
- At known-good milestones (e.g., "after first integrated variant").
- Before tearing down for a context switch.

### What checkpoint does NOT capture

- The manual-CLI scratch state (`/tmp/eden-manual/`) — operator's local
  claim cache. Reproducible from postgres state if needed.
- Worker repo clones (`eden-{orchestrator,executor,evaluator,web-ui}-repo`).
  These are derivative; rebuilt from gitea on next service start.
- In-memory state on running services (web-ui session cookies, etc.).
  Lost on container restart by design.

## Workflow: reset

If the user wants a clean slate to run a NEW experiment under a different
name or seed: use `reset`. Treat the elicitation same as `up`, then run:

```bash
$EDEN_EXP reset <config> --experiment-id <id> [--seed-from <dir>]
```

## Best practices

- **Don't tear down without confirming first.** `down --purge` deletes
  experiment state irreversibly. Even with auto mode, this is the kind
  of action that warrants a "going to wipe `<id>` — confirm?" before
  running.
- **`--seed-from` defaults to the working tree, not history.** Tell the
  user this when they specify a git repo — they may have uncommitted
  changes that will get included.
- **Verify after `up --seed-from`.** A wrong seed is silent (you get
  files but the wrong files). The gitea contents check catches this.
- **Re-running `up` against a different `--seed-from` only works if
  `down` ran first.** The script enforces this with the
  "stack already running" guard, but the user may not realize that
  changing `--seed-from` requires a tear-down. If they want to swap the
  seed: `reset` is the one-step shortcut.
