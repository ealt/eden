# Experiment data durability

This page describes where the reference Compose deployment keeps an experiment's data, what survives common operator actions, and how to relocate or migrate the substrate tree. It is the operator-facing complement to the normative invariant in [`spec/v0/01-concepts.md`](../../spec/v0/01-concepts.md) §13.

## Where experiment data lives

After running `setup-experiment.sh`, every durable substrate is a **host bind-mount** under a single per-experiment data-root directory. The default location is `$HOME/.eden/experiments/$EDEN_EXPERIMENT_ID/`.

```text
$EDEN_EXPERIMENT_DATA_ROOT/
├── postgres/              # task-store-server's PostgresStore data
├── forgejo/                 # Forgejo's data (sqlite DB, git packs, …)
├── artifacts/             # web-ui --artifacts-dir (idea markdown, …)
├── orchestrator-repo/     # orchestrator's per-host bare clone
├── executor-repo/         # executor-host's per-host bare clone
├── evaluator-repo/        # evaluator-host's bare clone (subprocess mode)
├── web-ui-repo/           # web-ui's per-host bare clone
└── credentials/
    ├── orchestrator/      # persisted per-worker registration token
    ├── ideator/
    ├── executor/
    ├── evaluator/
    └── web-ui/
```

This is the **entire** protocol-owned state that chapter 01 §13 of the spec requires to survive substrate restarts. The data root surfaces it directly on the host filesystem so the operator can see, back up, copy, and (if needed) destroy it without going through `docker volume` commands.

## Durability posture

| Operator action / event                                  | Survives? | Notes                                                                                                                                                                                |
| -------------------------------------------------------- | --------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `docker compose stop` / `docker compose down`            | yes       | Bind-mount files persist; the stack restarts against the same data root and the orchestrator picks up where it left off.                                                             |
| Host reboot                                              | yes       | Same as above — the substrate is on a normal host filesystem.                                                                                                                        |
| Docker engine restart                                    | yes       | Same as above.                                                                                                                                                                       |
| Docker Desktop quit / relaunch (macOS / Windows)         | yes       | Bind-mounts are NOT inside the Docker Desktop VM's disk image.                                                                                                                       |
| Docker Desktop "Restart" button                          | yes       | Same as above.                                                                                                                                                                       |
| Docker Desktop **update / VM rebuild**                   | **yes**   | This was the failure mode that motivated chapter 01 §13: Docker Desktop occasionally rebuilds its embedded VM (replacing `Docker.raw` on macOS), which destroys every named Docker volume. Bind-mounts on the host filesystem are unaffected. |
| Docker Desktop "Reset to factory defaults"               | yes       | Same as above. (Containers and images are wiped, but the data root on the host is not.)                                                                                              |
| `docker volume rm` of any EDEN volume                    | mostly    | The remaining named volumes are intentionally ephemeral (`eden-repo-init-staging`, `eden-worktrees`); their loss is recoverable by re-running `setup-experiment.sh`.                  |
| `docker compose down -v`                                 | mostly    | Same as above. Bind-mounts under the data root are untouched.                                                                                                                        |
| `rm -rf $EDEN_EXPERIMENT_DATA_ROOT`                      | **no**    | Operator-initiated destruction. The experiment is gone.                                                                                                                              |
| Host disk failure / disk format / OS reinstall           | **no**    | Outside the protocol's control. Operators concerned about disk failure should back up the data root with their normal filesystem-backup tooling (Time Machine, restic, rsync, …).    |

In short: routine maintenance — including the events that motivated this chunk — does NOT destroy experiment state. Only explicit deletion or hardware-level failure does.

## Custom data root

Set `--data-root <path>` when invoking `setup-experiment.sh` to put the substrate tree somewhere other than `~/.eden/experiments/<id>/`:

```bash
bash reference/scripts/setup-experiment/setup-experiment.sh \
    tests/fixtures/experiment/.eden/config.yaml \
    --experiment-id my-exp \
    --data-root /Volumes/MyBackedUpDrive/eden-data/my-exp
```

Use cases:

- **Time-Machine-backed location.** On macOS, `~/.eden/experiments/` is excluded from Time Machine by default if `~` is the user's home and `~/.eden` looks dot-prefixed; pointing the data root at `~/Documents/eden-data/<id>` gets it onto the Time Machine backup automatically.
- **External / encrypted drive.** Point at a mountpoint on an external SSD or an encrypted-volume mount.
- **Separation of concerns.** Multiple operators sharing a workstation can each have their own data-root tree.

Rules of thumb:

- The path is resolved to an absolute path by `setup-experiment.sh` and written into the generated `.env` as `EDEN_EXPERIMENT_DATA_ROOT`. Compose does NOT shell-expand `$HOME` or `~` in `.env` values, so the script must do the expansion; do not hand-edit `.env` to put `$HOME` or `~` literally.
- Paths containing `:` are rejected (Compose uses `:` as the volume-mount delimiter; a colon in the source half would silently mis-split the mount declaration).
- Spaces in the path are fine (`cd "$path" && pwd`-style quoting handles them).
- Keep the path short. Each substrate creates files inside it (`postgres/base/.../<oid>`, `forgejo/git/repositories/.../...`) and a very deep parent prefix can push individual files past `PATH_MAX`. The default `~/.eden/experiments/<id>` is well under any practical limit.

## Re-running setup-experiment

`setup-experiment.sh` is idempotent. If you re-run it without `--data-root`:

- The data root from the existing `.env` is preserved.
- The substrate subdirectory tree is created if missing, and chmod'd to 0777 (local-dev permissions; see "Permissions" below).
- Secrets (`POSTGRES_PASSWORD`, `EDEN_ADMIN_TOKEN`, `EDEN_SESSION_SECRET`, etc.) are preserved.

If you re-run with `--data-root <different-path>` and the existing data root has substrate data (postgres/ or forgejo/ non-empty), the script **aborts** rather than silently relocating. To intentionally change the data root, either:

- Empty the existing data root first (`rm -rf "$OLD_ROOT"`) — destroys the experiment.
- Or migrate manually (see "Migration recipes" below).

## Permissions

setup-experiment.sh creates the substrate subdirectories with `chmod 0777`. This is the proven cross-platform recipe for the multi-uid problem the reference Compose stack has: Postgres runs as uid 70, Forgejo-rootless as uid 1000, eden services as uid 1000, and Docker Desktop's uid-mapping layer makes a single `chown` choice fragile. World-writable is acceptable for the reference local-dev deployment; production-grade durability lives in Phase 13's managed substrates (managed Postgres, S3/GCS, hardened Forgejo), which avoid the local filesystem entirely.

## Migration recipes

### From the legacy named-volume layout (pre-12a-1g)

Operators with a Compose experiment already running on Docker named volumes (`eden-postgres-data`, `eden-forgejo-data`, etc., backed by the Docker storage area) migrate as follows. The recipe runs once; after it completes the operator is on the bind-mount layout and future restarts are durable.

**Volume-naming discipline (read this first).** Compose auto-prefixes top-level named volumes with the project name (default `eden-reference`, configurable via `name:` at the top of `compose.yaml` OR via `COMPOSE_PROJECT_NAME`). So the legacy stack had two shapes of volume name:

- **Project-prefixed:** `eden-reference_eden-postgres-data`, `eden-reference_eden-forgejo-data`, `eden-reference_eden-orchestrator-repo`, `eden-reference_eden-web-ui-repo`, and every `eden-reference_eden-*-credentials`.
- **Literal-`name:` pinned** (the chunk-10d-followup-A wrap needed exact names for DooD volume forwarding): `eden-artifacts-data`, `eden-executor-repo`, `eden-evaluator-repo`.

If your stack ran with a non-default `COMPOSE_PROJECT_NAME`, replace `eden-reference` below with your project name everywhere it appears. Confirm the actual names via `docker volume ls | grep eden` before starting the copy.

```bash
# Pick the new data root.
EID="my-existing-experiment"
ROOT="$HOME/.eden/experiments/$EID"

# Stop the stack WITHOUT -v so the named volumes survive the copy.
cd reference/compose
docker compose --env-file .env stop

# Create the bind-mount tree.
mkdir -p "$ROOT"/{postgres,forgejo,artifacts,orchestrator-repo,executor-repo,evaluator-repo,web-ui-repo,credentials/{orchestrator,ideator,executor,evaluator,web-ui}}
chmod -R 0777 "$ROOT"

# Copy each substrate from its named volume into the new bind-mount.
# The eden-reference_<vol> naming follows compose's default project-prefix
# behavior; volumes pinned with `name:` (eden-artifacts-data,
# eden-executor-repo, eden-evaluator-repo) drop the prefix.
docker run --rm -v eden-reference_eden-postgres-data:/from -v "$ROOT/postgres":/to       alpine cp -a /from/. /to/
docker run --rm -v eden-reference_eden-forgejo-data:/from    -v "$ROOT/forgejo":/to          alpine cp -a /from/. /to/
docker run --rm -v eden-artifacts-data:/from               -v "$ROOT/artifacts":/to      alpine cp -a /from/. /to/
docker run --rm -v eden-reference_eden-orchestrator-repo:/from -v "$ROOT/orchestrator-repo":/to alpine cp -a /from/. /to/
docker run --rm -v eden-executor-repo:/from                -v "$ROOT/executor-repo":/to  alpine cp -a /from/. /to/
docker run --rm -v eden-evaluator-repo:/from               -v "$ROOT/evaluator-repo":/to alpine cp -a /from/. /to/
docker run --rm -v eden-reference_eden-web-ui-repo:/from   -v "$ROOT/web-ui-repo":/to    alpine cp -a /from/. /to/
docker run --rm -v eden-reference_eden-orchestrator-credentials:/from -v "$ROOT/credentials/orchestrator":/to alpine cp -a /from/. /to/
docker run --rm -v eden-reference_eden-ideator-credentials:/from      -v "$ROOT/credentials/ideator":/to      alpine cp -a /from/. /to/
docker run --rm -v eden-reference_eden-executor-credentials:/from     -v "$ROOT/credentials/executor":/to     alpine cp -a /from/. /to/
docker run --rm -v eden-reference_eden-evaluator-credentials:/from    -v "$ROOT/credentials/evaluator":/to    alpine cp -a /from/. /to/
docker run --rm -v eden-reference_eden-web-ui-credentials:/from       -v "$ROOT/credentials/web-ui":/to       alpine cp -a /from/. /to/

# Re-run setup-experiment with the new --data-root. Existing secrets
# in .env are preserved.
cd ..
bash scripts/setup-experiment/setup-experiment.sh \
    <path-to-your-config.yaml> \
    --experiment-id "$EID" \
    --data-root "$ROOT"

# Bring the stack back up.
cd compose
docker compose --env-file .env up -d --wait

# Once verified, delete the legacy named volumes.
docker volume rm \
    eden-reference_eden-postgres-data \
    eden-reference_eden-forgejo-data \
    eden-artifacts-data \
    eden-reference_eden-orchestrator-repo \
    eden-executor-repo eden-evaluator-repo \
    eden-reference_eden-web-ui-repo \
    eden-reference_eden-orchestrator-credentials \
    eden-reference_eden-ideator-credentials \
    eden-reference_eden-executor-credentials \
    eden-reference_eden-evaluator-credentials \
    eden-reference_eden-web-ui-credentials
```

### Moving an experiment to a new data root

The relocation guard in setup-experiment.sh refuses to silently abandon a populated `/OLD/ROOT` (it checks for `postgres/PG_VERSION` and `forgejo/conf` sentinels). To migrate cleanly:

```bash
# Stop the stack first so nothing is writing to /OLD/ROOT while we copy.
docker compose --env-file .env stop

# Copy substrate state to the new location.
rsync -a --delete /OLD/ROOT/ /NEW/ROOT/

# Move the old root aside (or `rm -rf /OLD/ROOT` if you trust the copy).
# This is what unblocks setup-experiment's relocation guard — the
# guard only refuses when the OLD root still has substantive
# substrate data.
mv /OLD/ROOT /OLD/ROOT.migrated

# Now setup-experiment will accept --data-root /NEW/ROOT.
bash reference/scripts/setup-experiment/setup-experiment.sh \
    <config.yaml> --experiment-id <id> --data-root /NEW/ROOT

# Bring the stack back up; .env now points at the new location.
docker compose --env-file .env up -d --wait

# Once verified, delete the migrated old root.
rm -rf /OLD/ROOT.migrated
```

If you'd rather not move the old root (e.g. you want to keep it as a checkpoint), edit `.env` by hand to update `EDEN_EXPERIMENT_DATA_ROOT=/NEW/ROOT` BEFORE re-running setup-experiment. setup-experiment then reads /NEW/ROOT as the "existing" root, never inspects /OLD/ROOT, and the guard doesn't engage.

## Manual kill-and-restart check

To verify the durability claim hands-on, drive a complete experiment to quiescence, stop the stack (without `-v`), inspect the data root, and bring everything back up against the same root + env file. The recipe deliberately does NOT call `smoke.sh` — that script tears the stack down on EXIT and uses a per-invocation `mktemp` env file, neither of which compose with an inter-restart durability inspection.

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

# 2. Run to quiescence — wait for the orchestrator's clean exit so
#    the pre-restart state is deterministic.
docker compose --env-file "$ENV_FILE" -f reference/compose/compose.yaml up -d --wait
until [[ "$(docker inspect -f '{{.State.Status}}' eden-orchestrator 2>/dev/null)" == "exited" ]]; do
    sleep 2
done

# 3. Stop without -v so bind-mounts survive.
docker compose --env-file "$ENV_FILE" -f reference/compose/compose.yaml down

# 4. Verify substrate tree is still populated on the host filesystem.
ls "$ROOT/postgres" "$ROOT/forgejo" "$ROOT/artifacts"

# 5. Bring the stack back up against the same data root + env file.
docker compose --env-file "$ENV_FILE" -f reference/compose/compose.yaml up -d --wait

# 6. Confirm worker registry survived. The default compose stack has
#    five worker hosts (orchestrator + ideator + executor + evaluator
#    + web-ui), each self-registered at startup.
TOKEN="$(sed -n 's/^EDEN_ADMIN_TOKEN=//p' "$ENV_FILE")"
PORT="$(sed -n 's/^TASK_STORE_HOST_PORT=//p' "$ENV_FILE")"
curl -sS -H "Authorization: Bearer admin:$TOKEN" \
    -H "X-Eden-Experiment-Id: $EID" \
    "http://localhost:${PORT:-8080}/v0/experiments/$EID/workers" \
  | jq '.workers | length'   # expect ≥5
```

## Production-grade durability

The reference Compose deployment is a local-development tool. Production EDEN deployments use the Phase-13 substrate stack:

- **Phase 13a** — Helm chart with PersistentVolumeClaims for substrate storage (Kubernetes-native durability).
- **Phase 13c** — Managed Postgres backing the task store (cloud-provider SLA).
- **Phase 13d** — S3 / GCS backing the artifact store (cloud-provider durability).
- **Phase 13e** — Hardened Forgejo deployment.

Each Phase-13 chunk satisfies the chapter-01 §13 durability invariant via its own substrate binding. The reference Compose deployment's bind-mount choice is appropriate for local-dev; it is not the only valid binding.
