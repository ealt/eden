# setup-experiment

Bootstrap an EDEN reference Compose stack for a given experiment
config. Reads a YAML config, generates (or preserves) the secrets the
stack needs, runs the one-shot bare-repo init service, and writes
everything into `reference/compose/.env` +
`reference/compose/experiment-config.yaml` so `docker compose up -d
--wait` brings the stack online.

## Usage

```bash
bash reference/scripts/setup-experiment/setup-experiment.sh <config.yaml> \
    [--experiment-id <id>] \
    [--shared-token <T>] \
    [--postgres-password <P>] \
    [--env-file <path>]
```

Then run:

```bash
cd reference/compose
docker compose --env-file .env up -d --wait
```

## What it does

1. Generates or preserves: `POSTGRES_PASSWORD`, `EDEN_SHARED_TOKEN`,
   `EDEN_SESSION_SECRET`, `GITEA_SECRET_KEY`, `GITEA_INTERNAL_TOKEN`,
   default ports, and `EDEN_PLAN_TASKS`. Re-runs preserve any
   existing values from `.env`.
2. Copies `<config.yaml>` to `reference/compose/experiment-config.yaml`
   so Compose mounts it into the task-store-server, evaluator-host,
   and web-ui via `configs:`.
3. Builds the shared `eden-reference:dev` image.
4. Runs `docker compose run --rm --no-deps eden-repo-init` — a
   one-shot service that initializes (or re-uses) the bare repo on
   the `eden-bare-repo` volume and prints the seed commit SHA.
5. Writes the seed SHA into `.env` as `EDEN_BASE_COMMIT_SHA` so the
   planner-host can thread it into `--base-commit-sha`.
6. Prints a "next steps" message.

## Idempotency

Re-running on an already-configured stack is safe:

- Secrets generated on a prior run are preserved (read back from the
  existing `.env`).
- The bare repo is not re-seeded — `eden-repo-init` short-circuits
  on a previously-seeded volume and re-prints the existing SHA.
- The experiment-config file is overwritten with the latest
  `<config.yaml>` contents.

## Tear-down

```bash
cd reference/compose
docker compose --env-file .env down -v
```

`-v` wipes the Postgres / bare-repo / artifacts volumes too. After
`down -v` you must re-run setup-experiment before `compose up` to
re-seed the bare repo.

## Out of scope

Documented limits inherited from the chunk-10b/c roadmap-delta:

- Workers do not push/pull from Gitea; they share a Compose volume
  for the bare repo. Gitea is idle.
- No control-plane registration (Phase 12).
- No experiment-specific implementer image (10d).
