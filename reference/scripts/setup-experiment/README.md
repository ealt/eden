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
    [--env-file <path>]
```

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

`-v` wipes the Postgres / Forgejo / artifacts volumes too. After
`down -v` you must re-run setup-experiment before `compose up`.
