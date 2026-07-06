# setup-experiment

Bootstrap an EDEN reference Compose stack for a given experiment
config. Reads a YAML config, generates (or preserves) the secrets the
stack needs, provisions Forgejo, runs the one-shot bare-repo init
service, and writes everything into `reference/compose/.env` +
`reference/compose/experiment-config.yaml` so `docker compose up -d
--wait` brings the stack online.

## Usage

```bash
bash reference/scripts/setup-experiment/setup-experiment.sh <config.yaml> \
    [--experiment-id <id>] \
    [--admin-token <T>] \
    [--postgres-password <P>] \
    [--env-file <path>] \
    [--experiment-dir <dir>] \
    [--ideas-per-ideation <n>] \
    [--exec-mode host|docker] \
    [--seed-from <dir>] \
    [--data-root <dir>] \
    [--no-auto-host-workers]
```

`--exec-mode docker` enables the DooD container-isolation overlay (builds/uses `eden-runtime:dev` or an experiment-specific image, probes the docker-socket gid). `--seed-from` seeds the bare repo from an existing directory instead of an empty commit. `--data-root` overrides where the durable per-experiment substrate lives (default `$HOME/.eden/experiments/<experiment-id>/`). `--no-auto-host-workers` skips minting the per-service worker identities (for deployments that register workers themselves).

Then run:

```bash
cd reference/compose
docker compose --env-file .env up -d --wait
```

## What it does

1. Generates or preserves: `POSTGRES_PASSWORD`, `EDEN_ADMIN_TOKEN`,
   `EDEN_SESSION_SECRET`, `FORGEJO_SECRET_KEY`, `FORGEJO_INTERNAL_TOKEN`,
   default ports, and `EDEN_IDEATION_TASKS`. Re-runs preserve any
   existing values from `.env` (including legacy `FORGEJO_*` keys, which
   are migrated forward).
2. Copies `<config.yaml>` to `reference/compose/experiment-config.yaml`
   so Compose mounts it into the task-store-server, evaluator-host,
   and web-ui via `configs:`.
3. Builds the shared `eden-reference:dev` image.
4. Brings up Forgejo, provisions the `eden` admin user and
   `eden/<experiment-id>` repo, and writes a per-experiment credential
   helper under `.forgejo-creds-<experiment-id>/`.
5. Runs `docker compose run --rm --no-deps eden-repo-init` with
   `--push-to` the in-network Forgejo remote — seeds the repo and prints
   the commit SHA.
6. Writes the seed SHA into `.env` as `EDEN_BASE_COMMIT_SHA` so the
   ideator-host can thread it into `--base-commit-sha`.
7. Prints a "next steps" message.

## Idempotency

Re-running on an already-configured stack is safe:

- Secrets generated on a prior run are preserved (read back from the
  existing `.env`).
- Forgejo user/repo provisioning is idempotent.
- The experiment-config file is overwritten with the latest
  `<config.yaml>` contents.

## Tear-down

```bash
cd reference/compose
docker compose --env-file .env down -v
```

`-v` removes the ephemeral named volumes (worktrees, repo-init
staging). Durable substrate state — Postgres data, Forgejo data,
artifacts, per-service clones, worker credentials — lives as host
bind-mounts under `${EDEN_EXPERIMENT_DATA_ROOT}/` (Phase 12a-1g) and
is **not** touched by `down -v`; delete that directory explicitly to
wipe an experiment. See
[`docs/operations/experiment-data-durability.md`](../../../docs/operations/experiment-data-durability.md).
After a full wipe you must re-run setup-experiment before `compose up`.
