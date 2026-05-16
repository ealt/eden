#!/usr/bin/env bash
set -euo pipefail

# setup-experiment — bootstrap an EDEN reference Compose stack for a
# given experiment config. Reads a YAML config, generates (or
# preserves) the secrets the stack needs, runs the one-shot
# bare-repo init service, and writes everything into
# `reference/compose/.env` + `reference/compose/experiment-config.yaml`
# so `docker compose up -d --wait` brings the stack online.
#
# Idempotent: re-running on an already-configured stack preserves
# existing secrets (POSTGRES_PASSWORD, EDEN_ADMIN_TOKEN,
# EDEN_SESSION_SECRET, GITEA_*) and re-runs the seed step (which
# itself short-circuits on a previously-seeded repo).
#
# Usage:
#   setup-experiment.sh <config.yaml> [--experiment-id <id>]
#                                     [--admin-token <T>]
#                                     [--postgres-password <P>]
#                                     [--env-file <path>]
#                                     [--experiment-dir <path>]
#                                     [--ideas-per-ideation <N>]

usage() {
    cat <<'EOF' >&2
Usage:
  setup-experiment.sh <config.yaml>
                      [--experiment-id <id>]
                      [--admin-token <T>]
                      [--postgres-password <P>]
                      [--env-file <path>]
                      [--experiment-dir <path>]
                      [--data-root <path>]
                      [--ideas-per-ideation <N>]
                      [--exec-mode {host,docker}]
                      [--seed-from <host-dir>]

Generates `reference/compose/.env` and copies <config.yaml> to
`reference/compose/experiment-config.yaml`, then seeds the bare
repo via a one-shot `compose run --rm --no-deps eden-repo-init`
call. Operator's next step is `docker compose up -d --wait`.

--data-root specifies the host-side parent directory under which
every durable substrate (postgres data, gitea data, artifacts,
per-host bare clones, per-host worker credentials) is bind-mounted
(Phase 12a-1g, chapter 01 §13). Default:
`$HOME/.eden/experiments/<experiment-id>`. The substrate tree
under the data root survives `docker compose down`, `docker
compose down -v`, Docker Desktop VM rebuilds, host reboot, and
individual substrate container restart. The only operator action
that destroys it is explicit `rm -rf` of the data root; disk
failure is the only non-operator way to lose it. See
`docs/operations/experiment-data-durability.md`.

When --exec-mode docker is used (subprocess overlay only), the
script also: probes the in-VM gid of /var/run/docker.sock, builds
`eden-runtime:dev`, optionally builds an experiment-specific image
from `<EXPERIMENT_DIR>/Dockerfile`, and creates the host-side
cidfile dir mounted into worker hosts.
EOF
}

# --- Tooling preflight ---
for tool in docker python3; do
    command -v "$tool" >/dev/null || {
        echo "setup-experiment.sh requires '$tool' on PATH" >&2
        exit 2
    }
done
docker compose version >/dev/null || {
    echo "setup-experiment.sh requires the 'docker compose' v2 plugin" >&2
    exit 2
}

# --- Parse args ---
CONFIG_PATH=""
ENV_FILE=""
EXPERIMENT_ID=""
ARG_ADMIN_TOKEN=""
ARG_POSTGRES_PASSWORD=""
ARG_EXPERIMENT_DIR=""
ARG_IDEAS_PER_IDEATION=""
ARG_EXEC_MODE="host"
ARG_SEED_FROM=""
ARG_DATA_ROOT=""

require_value() {
    # Validate that a flag's value is present, since `set -u` would
    # turn a bare `$2` lookup into a confusing "unbound variable"
    # error rather than a clear "missing argument" message.
    local flag="$1" remaining="$2"
    if [[ "$remaining" -lt 2 ]]; then
        echo "$flag requires a value" >&2
        usage
        exit 2
    fi
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --experiment-id)        require_value "$1" "$#"; EXPERIMENT_ID="$2";         shift 2 ;;
        --admin-token)          require_value "$1" "$#"; ARG_ADMIN_TOKEN="$2";       shift 2 ;;
        --postgres-password)    require_value "$1" "$#"; ARG_POSTGRES_PASSWORD="$2"; shift 2 ;;
        --env-file)             require_value "$1" "$#"; ENV_FILE="$2";              shift 2 ;;
        --experiment-dir)       require_value "$1" "$#"; ARG_EXPERIMENT_DIR="$2";    shift 2 ;;
        --ideas-per-ideation)   require_value "$1" "$#"; ARG_IDEAS_PER_IDEATION="$2"; shift 2 ;;
        --exec-mode)            require_value "$1" "$#"; ARG_EXEC_MODE="$2";         shift 2 ;;
        --seed-from)            require_value "$1" "$#"; ARG_SEED_FROM="$2";         shift 2 ;;
        --data-root)            require_value "$1" "$#"; ARG_DATA_ROOT="$2";         shift 2 ;;
        -h|--help)              usage; exit 0 ;;
        --*)                    echo "unknown flag: $1" >&2; usage; exit 2 ;;
        *)
            if [[ -z "$CONFIG_PATH" ]]; then
                CONFIG_PATH="$1"
            else
                echo "unexpected positional arg: $1" >&2
                usage
                exit 2
            fi
            shift
            ;;
    esac
done

# Resolve absolute paths so this script works no matter where it
# was invoked from (compute SCRIPT_DIR before we may need it for the
# default config path below).
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_DIR="$(cd "${SCRIPT_DIR}/../../compose" && pwd)"
REPO_ROOT="$(cd "${COMPOSE_DIR}/../.." && pwd)"

if [[ -z "$CONFIG_PATH" ]]; then
    # Default to the repo's fixture experiment so re-running with no
    # args is a no-friction smoke path.
    CONFIG_PATH="${REPO_ROOT}/tests/fixtures/experiment/.eden/config.yaml"
fi
if [[ ! -f "$CONFIG_PATH" ]]; then
    echo "config not found: $CONFIG_PATH" >&2
    exit 2
fi

CONFIG_PATH="$(cd "$(dirname "$CONFIG_PATH")" && pwd)/$(basename "$CONFIG_PATH")"

if [[ -z "$ENV_FILE" ]]; then
    ENV_FILE="${COMPOSE_DIR}/.env"
fi

# --- Default experiment-id from the config's parent dir ---
if [[ -z "$EXPERIMENT_ID" ]]; then
    # `<some-path>/.eden/config.yaml` → use the directory containing `.eden`.
    parent="$(dirname "$CONFIG_PATH")"
    grandparent="$(dirname "$parent")"
    if [[ "$(basename "$parent")" == ".eden" ]]; then
        EXPERIMENT_ID="$(basename "$grandparent")"
    else
        EXPERIMENT_ID="$(basename "$parent")"
    fi
fi

# --- Secret helpers ---
gen_hex() {
    # 32 bytes of hex.
    python3 -c 'import secrets,sys; sys.stdout.write(secrets.token_hex(int(sys.argv[1])))' "${1:-32}"
}

# Read a key from an existing env file, returning empty if absent.
read_env_key() {
    local key="$1" file="$2"
    if [[ -f "$file" ]]; then
        # Strip surrounding quotes that an operator may have added.
        sed -n "s/^${key}=\(.*\)$/\1/p" "$file" | head -n1
    fi
}

# --- Resolve / preserve / generate secrets ---
EXISTING_POSTGRES_PASSWORD="$(read_env_key POSTGRES_PASSWORD "$ENV_FILE")"
POSTGRES_PASSWORD="${ARG_POSTGRES_PASSWORD:-${EXISTING_POSTGRES_PASSWORD:-$(gen_hex 32)}}"

EXISTING_ADMIN_TOKEN="$(read_env_key EDEN_ADMIN_TOKEN "$ENV_FILE")"
EDEN_ADMIN_TOKEN="${ARG_ADMIN_TOKEN:-${EXISTING_ADMIN_TOKEN:-$(gen_hex 32)}}"

EXISTING_SESSION_SECRET="$(read_env_key EDEN_SESSION_SECRET "$ENV_FILE")"
EDEN_SESSION_SECRET="${EXISTING_SESSION_SECRET:-$(gen_hex 32)}"

EXISTING_GITEA_SECRET_KEY="$(read_env_key GITEA_SECRET_KEY "$ENV_FILE")"
GITEA_SECRET_KEY="${EXISTING_GITEA_SECRET_KEY:-$(gen_hex 32)}"

EXISTING_GITEA_INTERNAL_TOKEN="$(read_env_key GITEA_INTERNAL_TOKEN "$ENV_FILE")"
GITEA_INTERNAL_TOKEN="${EXISTING_GITEA_INTERNAL_TOKEN:-$(gen_hex 32)}"

# Phase 10d follow-up B: per-experiment Gitea password for the eden
# user. Workers use this via HTTP Basic auth (the credential-helper
# script is generated from a template after Gitea is up). Preserved
# across re-runs.
EXISTING_GITEA_REMOTE_PASSWORD="$(read_env_key GITEA_REMOTE_PASSWORD "$ENV_FILE")"
GITEA_REMOTE_PASSWORD="${EXISTING_GITEA_REMOTE_PASSWORD:-$(gen_hex 32)}"

EXISTING_PG_HOST_PORT="$(read_env_key POSTGRES_HOST_PORT "$ENV_FILE")"
POSTGRES_HOST_PORT="${EXISTING_PG_HOST_PORT:-5433}"

EXISTING_GITEA_HOST_PORT="$(read_env_key GITEA_HOST_PORT "$ENV_FILE")"
GITEA_HOST_PORT="${EXISTING_GITEA_HOST_PORT:-3001}"

EXISTING_GITEA_SSH_HOST_PORT="$(read_env_key GITEA_SSH_HOST_PORT "$ENV_FILE")"
GITEA_SSH_HOST_PORT="${EXISTING_GITEA_SSH_HOST_PORT:-2222}"

EXISTING_WEB_UI_HOST_PORT="$(read_env_key WEB_UI_HOST_PORT "$ENV_FILE")"
WEB_UI_HOST_PORT="${EXISTING_WEB_UI_HOST_PORT:-8090}"

# 12a-2 wave 4: the pre-12a-2 EDEN_IDEATION_TASKS static-seed env is
# retired. The orchestrator now drives ideation creation through
# `eden_dispatch.policies:default_policy`, parameterized by
# EDEN_IDEATION_POLICY_TARGET_PENDING (default 3) +
# EDEN_IDEATION_POLICY_MAX_TOTAL (default unbounded). Preserved across
# re-runs.
EXISTING_TARGET_PENDING="$(read_env_key EDEN_IDEATION_POLICY_TARGET_PENDING "$ENV_FILE")"
EDEN_IDEATION_POLICY_TARGET_PENDING="${EXISTING_TARGET_PENDING:-3}"
EXISTING_MAX_TOTAL="$(read_env_key EDEN_IDEATION_POLICY_MAX_TOTAL "$ENV_FILE")"
EDEN_IDEATION_POLICY_MAX_TOTAL="${EXISTING_MAX_TOTAL:-}"
# 12a-2 wave 4 / §3.8: auto-orchestrator worker_id. Multi-replica
# deployments override per-replica.
EXISTING_ORCH_WID="$(read_env_key EDEN_ORCHESTRATOR_WORKER_ID "$ENV_FILE")"
EDEN_ORCHESTRATOR_WORKER_ID="${EXISTING_ORCH_WID:-orchestrator}"
# 12a-2 wave 7 / §5.7: initial admin worker_id seeded into the
# `admins` group below. Operators acting through the web UI
# authenticate as this worker; it's the only principal that can
# drive reassign_task / update_dispatch_mode / create_task(kind=execution)
# under the wave-3 §3.7 authority gates.
EXISTING_ADMIN_MEMBER="$(read_env_key EDEN_ADMINS_INITIAL_MEMBER "$ENV_FILE")"
EDEN_ADMINS_INITIAL_MEMBER="${EXISTING_ADMIN_MEMBER:-operator}"

# 12a-2 wave 7 follow-up: the web-ui's worker_id is the deployment-
# level admin actor (until the §D.5b per-user session-bearer retrofit
# lands in a later phase). Adding it to `admins` lets operators flip
# dispatch_mode + reassign tasks through the /admin/ routes; without
# this, the web-ui's StoreClient hits 403 on every admin-gated PATCH.
EXISTING_WEB_UI_WID="$(read_env_key EDEN_WEB_UI_WORKER_ID "$ENV_FILE")"
EDEN_WEB_UI_WORKER_ID="${EXISTING_WEB_UI_WID:-web-ui-1}"

# 10d subprocess overlay: experiment-dir bind-mount source. Default
# is the directory containing the experiment config's `.eden`
# parent (so passing `tests/fixtures/experiment/.eden/config.yaml`
# yields `tests/fixtures/experiment`). Operator can override.
if [[ -n "$ARG_EXPERIMENT_DIR" ]]; then
    EDEN_EXPERIMENT_DIR_HOST="$(cd "$ARG_EXPERIMENT_DIR" && pwd)"
else
    parent="$(dirname "$CONFIG_PATH")"
    if [[ "$(basename "$parent")" == ".eden" ]]; then
        EDEN_EXPERIMENT_DIR_HOST="$(cd "$(dirname "$parent")" && pwd)"
    else
        EDEN_EXPERIMENT_DIR_HOST="$(cd "$parent" && pwd)"
    fi
fi
EXISTING_IDEAS_PER_IDEATION="$(read_env_key EDEN_IDEAS_PER_IDEATION "$ENV_FILE")"
EDEN_IDEAS_PER_IDEATION="${ARG_IDEAS_PER_IDEATION:-${EXISTING_IDEAS_PER_IDEATION:-1}}"

# --- Phase 12a-1g: substrate data root resolution ---
# The data root is the parent directory under which every durable
# substrate (postgres, gitea, artifacts, per-host bare clones,
# per-host worker credentials) is bind-mounted. See chapter 01 §13
# + docs/operations/experiment-data-durability.md.
#
# Precedence:
#   1. --data-root flag (operator override).
#   2. EDEN_EXPERIMENT_DATA_ROOT in an existing .env (idempotent re-run).
#   3. Default: $HOME/.eden/experiments/<experiment-id>.
EXISTING_DATA_ROOT="$(read_env_key EDEN_EXPERIMENT_DATA_ROOT "$ENV_FILE")"
if [[ -n "$ARG_DATA_ROOT" ]]; then
    DATA_ROOT_INPUT="$ARG_DATA_ROOT"
elif [[ -n "$EXISTING_DATA_ROOT" ]]; then
    DATA_ROOT_INPUT="$EXISTING_DATA_ROOT"
else
    DATA_ROOT_INPUT="${HOME}/.eden/experiments/${EXPERIMENT_ID}"
fi

# Reject paths containing characters that break compose's volume-
# mount syntax. The most consequential one is `:` (compose's
# source/target delimiter); a colon in the source half would silently
# mis-split the mount declaration. See plan §8.4.
if [[ "$DATA_ROOT_INPUT" == *:* ]]; then
    echo "--data-root MUST NOT contain ':' (breaks compose volume-mount syntax): $DATA_ROOT_INPUT" >&2
    exit 2
fi

# Resolve to an absolute path. `cd "$path" && pwd` normalizes
# symlinks, relative components, and `~`-prefixed paths (after the
# shell has expanded them). The mkdir is idempotent.
mkdir -p "$DATA_ROOT_INPUT"
EDEN_EXPERIMENT_DATA_ROOT="$(cd "$DATA_ROOT_INPUT" && pwd)"

# Guard against silent relocation: if an existing .env points at a
# different data root AND that prior root has substantive substrate
# data (not just empty subdirs setup-experiment created), abort. The
# probe checks postgres' `PG_VERSION` sentinel (always present after
# postgres initdb) and gitea's `conf/` directory (created when the
# gitea container first boots). Either is sufficient evidence the
# operator has actually run the stack, not just bootstrapped the
# tree. Empty subdirs from an aborted setup don't trip this.
if [[ -n "$EXISTING_DATA_ROOT" && "$EXISTING_DATA_ROOT" != "$EDEN_EXPERIMENT_DATA_ROOT" ]]; then
    if [[ -f "$EXISTING_DATA_ROOT/postgres/PG_VERSION" \
        || -d "$EXISTING_DATA_ROOT/gitea/conf" ]]; then
        echo "refusing to silently relocate the data root:" >&2
        echo "  existing: $EXISTING_DATA_ROOT (has substrate data)" >&2
        echo "  new:      $EDEN_EXPERIMENT_DATA_ROOT" >&2
        echo "" >&2
        echo "Migrate manually (see docs/operations/experiment-data-durability.md)" >&2
        echo "or re-run setup-experiment without --data-root to keep the existing root." >&2
        exit 2
    fi
fi

# Create the substrate subdirectory tree. chmod 0777 matches the
# cidfile-dir precedent above: the four substrate containers run as
# different uids (postgres=70, gitea-rootless=1000, eden=1000) and
# Docker Desktop's uid mapping layer makes a single chown choice
# fragile. World-writable is acceptable for local-dev; production
# durability lives in Phase 13's managed substrates.
mkdir -p \
    "${EDEN_EXPERIMENT_DATA_ROOT}/postgres" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/gitea" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/artifacts" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/orchestrator-repo" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/executor-repo" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/evaluator-repo" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/web-ui-repo" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/credentials/orchestrator" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/credentials/ideator" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/credentials/executor" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/credentials/evaluator" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/credentials/web-ui"
if ! chmod 0777 \
    "${EDEN_EXPERIMENT_DATA_ROOT}" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/postgres" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/gitea" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/artifacts" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/orchestrator-repo" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/executor-repo" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/evaluator-repo" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/web-ui-repo" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/credentials" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/credentials/orchestrator" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/credentials/ideator" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/credentials/executor" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/credentials/evaluator" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/credentials/web-ui" 2>/dev/null
then
    # Best-effort: the script continues. But warn the operator so a
    # follow-up "compose up postgres-permission-denied" failure
    # surfaces a recognizable root cause instead of a generic error.
    echo "warning: chmod 0777 on substrate dirs under" >&2
    echo "         $EDEN_EXPERIMENT_DATA_ROOT" >&2
    echo "         partially failed. Containers running as non-root" >&2
    echo "         uids (postgres=70, gitea=1000, eden=1000) may fail" >&2
    echo "         to write. Adjust permissions manually or use a" >&2
    echo "         --data-root on a more permissive filesystem." >&2
fi

# --- Phase 10d follow-up A: --exec-mode docker resolution ---
# All four are written to .env unconditionally; the compose overlay's
# defaults make EDEN_EXEC_MODE=host a no-op (existing scripted /
# subprocess host-mode CI jobs stay green). When --exec-mode=docker
# we additionally:
#   * probe the in-VM gid of /var/run/docker.sock (so worker host
#     containers can use the bind-mounted socket as supplementary
#     group; see plan §D.6),
#   * build the default eden-runtime:dev image,
#   * if the experiment dir contains a Dockerfile, build an
#     experiment-specific image and use it as EDEN_EXEC_IMAGE,
#   * mkdir a host-side cidfile dir for cross-restart cidfile
#     persistence (see plan §D.3).
case "$ARG_EXEC_MODE" in
    host|docker) ;;
    *)
        echo "--exec-mode must be 'host' or 'docker'; got '$ARG_EXEC_MODE'" >&2
        exit 2
        ;;
esac
EDEN_EXEC_MODE="$ARG_EXEC_MODE"
EDEN_EXEC_IMAGE="eden-runtime:dev"
EDEN_DOCKER_GID="0"
EDEN_CIDFILES_DIR_HOST="${COMPOSE_DIR}/.cidfiles-${EXPERIMENT_ID}"
mkdir -p "$EDEN_CIDFILES_DIR_HOST"
# 0777 so worker-host containers (running as eden:1000) can write
# regardless of who created the dir on the host. The dir holds only
# unique-per-spawn cidfiles, no secrets.
chmod 0777 "$EDEN_CIDFILES_DIR_HOST" 2>/dev/null || true

if [[ "$EDEN_EXEC_MODE" = "docker" ]]; then
    if [[ ! -S /var/run/docker.sock ]]; then
        echo "--exec-mode docker requires /var/run/docker.sock on the host" >&2
        exit 2
    fi
    echo "--- probing in-container docker socket gid ---" >&2
    # Run `stat -c '%g'` from inside a throwaway container that
    # bind-mounts the socket. This returns the gid the worker host
    # container will see, which is what `group_add` needs. On Linux
    # native this matches the host's stat; on Docker Desktop /
    # Colima it does NOT (the VM has its own gid namespace).
    PROBED_GID="$(
        docker run --rm \
            -v /var/run/docker.sock:/var/run/docker.sock \
            alpine:3.20 \
            stat -c '%g' /var/run/docker.sock 2>/dev/null
    )"
    if [[ -z "$PROBED_GID" ]]; then
        echo "failed to probe docker socket gid" >&2
        exit 2
    fi
    EDEN_DOCKER_GID="$PROBED_GID"

    echo "--- building eden-runtime:dev ---" >&2
    docker build \
        -t eden-runtime:dev \
        -f "${COMPOSE_DIR}/Dockerfile.runtime" \
        "$REPO_ROOT" >&2

    if [[ -f "${EDEN_EXPERIMENT_DIR_HOST}/Dockerfile" ]]; then
        EXP_IMAGE="eden-experiment-${EXPERIMENT_ID}:dev"
        echo "--- building ${EXP_IMAGE} from experiment Dockerfile ---" >&2
        docker build -t "$EXP_IMAGE" "$EDEN_EXPERIMENT_DIR_HOST" >&2
        EDEN_EXEC_IMAGE="$EXP_IMAGE"
    fi
fi

# --- Copy experiment config into compose dir ---
cp "$CONFIG_PATH" "${COMPOSE_DIR}/experiment-config.yaml"

# --- Write the partial .env (no EDEN_BASE_COMMIT_SHA yet) ---
# Postgres DSN points at the in-network postgres service hostname.
# Percent-encode the password so user-supplied passwords containing
# reserved URI characters (`@`, `:`, `/`, `?`, `#`, …) don't break
# the DSN. The auto-generated 32-byte hex passwords are
# percent-encoding-safe by construction; this matters only when an
# operator passes `--postgres-password <raw-string>`.
POSTGRES_PASSWORD_ENC="$(python3 -c 'import sys, urllib.parse; sys.stdout.write(urllib.parse.quote(sys.argv[1], safe=""))' "$POSTGRES_PASSWORD")"
EDEN_STORE_URL="postgresql://eden:${POSTGRES_PASSWORD_ENC}@postgres:5432/eden"

ENV_TMP="$(mktemp)"
cat >"$ENV_TMP" <<EOF
# Generated by setup-experiment.sh — do not edit by hand. Re-run
# the script to regenerate. Manual edits to secrets will be
# preserved across re-runs (they are read back from this file).

# --- 10a infrastructure ---
POSTGRES_DB=eden
POSTGRES_USER=eden
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
POSTGRES_HOST_PORT=${POSTGRES_HOST_PORT}
GITEA_SECRET_KEY=${GITEA_SECRET_KEY}
GITEA_INTERNAL_TOKEN=${GITEA_INTERNAL_TOKEN}
GITEA_HOST_PORT=${GITEA_HOST_PORT}
GITEA_SSH_HOST_PORT=${GITEA_SSH_HOST_PORT}

# --- 10b/c reference services ---
EDEN_EXPERIMENT_ID=${EXPERIMENT_ID}
EDEN_ADMIN_TOKEN=${EDEN_ADMIN_TOKEN}
EDEN_SESSION_SECRET=${EDEN_SESSION_SECRET}
EDEN_STORE_URL=${EDEN_STORE_URL}
WEB_UI_HOST_PORT=${WEB_UI_HOST_PORT}

# --- 12a-1g substrate data root ---
# Host-side parent dir under which every durable substrate
# (postgres, gitea, artifacts, *-repo, credentials/*) is
# bind-mounted. See chapter 01 §13 + docs/operations/
# experiment-data-durability.md. Override via setup-experiment.sh
# --data-root <path>.
EDEN_EXPERIMENT_DATA_ROOT=${EDEN_EXPERIMENT_DATA_ROOT}

# --- 12a-2 orchestrator-role ---
EDEN_IDEATION_POLICY_TARGET_PENDING=${EDEN_IDEATION_POLICY_TARGET_PENDING}
EDEN_IDEATION_POLICY_MAX_TOTAL=${EDEN_IDEATION_POLICY_MAX_TOTAL}
EDEN_ORCHESTRATOR_WORKER_ID=${EDEN_ORCHESTRATOR_WORKER_ID}
EDEN_ADMINS_INITIAL_MEMBER=${EDEN_ADMINS_INITIAL_MEMBER}
EDEN_WEB_UI_WORKER_ID=${EDEN_WEB_UI_WORKER_ID}

# --- 10d subprocess overlay (only used by compose.subprocess.yaml) ---
EDEN_EXPERIMENT_DIR_HOST=${EDEN_EXPERIMENT_DIR_HOST}
EDEN_IDEAS_PER_IDEATION=${EDEN_IDEAS_PER_IDEATION}

# --- 10d follow-up A: container-isolated *_command execution ---
# EDEN_EXEC_MODE=host (default) keeps the chunk-10d behavior — user
# commands run on the worker host container. EDEN_EXEC_MODE=docker
# (set via --exec-mode docker) wraps each spawn in a sibling docker
# container via DooD (host /var/run/docker.sock).
EDEN_EXEC_MODE=${EDEN_EXEC_MODE}
EDEN_EXEC_IMAGE=${EDEN_EXEC_IMAGE}
EDEN_DOCKER_GID=${EDEN_DOCKER_GID}
EDEN_CIDFILES_DIR_HOST=${EDEN_CIDFILES_DIR_HOST}

# --- 10d follow-up B: Gitea-as-remote ---
# The workers' git remote is the in-network Gitea container. Workers
# clone bare on first run and fetch/push thereafter; the integrator
# publishes variant/* refs back to Gitea per chapter 6 §3.4.
GITEA_REMOTE_PASSWORD=${GITEA_REMOTE_PASSWORD}
GITEA_REMOTE_URL=http://gitea:3000/eden/${EXPERIMENT_ID}.git
EDEN_GITEA_CREDS_DIR_HOST=${COMPOSE_DIR}/.gitea-creds-${EXPERIMENT_ID}

# Placeholder; replaced at the end of setup-experiment with the
# real seed SHA. docker compose v2 validates ALL services'
# interpolation on every operation, so we need *some* value here for
# "docker compose build eden-repo-init" to succeed.
EDEN_BASE_COMMIT_SHA=0000000000000000000000000000000000000000
EOF

mv "$ENV_TMP" "$ENV_FILE"

# --- Build the shared image (so eden-repo-init can run) ---
echo "--- building eden-reference:dev ---" >&2
(cd "$COMPOSE_DIR" && docker compose --env-file "$ENV_FILE" build eden-repo-init >&2)

# --- Phase 10d follow-up B: bring Gitea up + provision ---
echo "--- bringing up gitea synchronously ---" >&2
(cd "$COMPOSE_DIR" && docker compose --env-file "$ENV_FILE" up -d --wait gitea >&2)

# Idempotent admin-user create. `gitea admin user create` exits
# non-zero if the user exists; the change-password fallback handles
# the re-run case.
echo "--- provisioning gitea eden user ---" >&2
if ! docker compose -f "${COMPOSE_DIR}/compose.yaml" --env-file "$ENV_FILE" \
        exec -T gitea gitea admin user create \
            --username eden \
            --password "$GITEA_REMOTE_PASSWORD" \
            --email eden@invalid \
            --admin \
            --must-change-password=false \
        >&2 2>&1
then
    docker compose -f "${COMPOSE_DIR}/compose.yaml" --env-file "$ENV_FILE" \
        exec -T gitea gitea admin user change-password \
            --username eden \
            --password "$GITEA_REMOTE_PASSWORD" \
            --must-change-password=false >&2
fi

echo "--- creating gitea repo eden/${EXPERIMENT_ID} ---" >&2
# 201 on first create, 409 on re-run; both are acceptable.
http_status=$(
    curl -fsS -o /dev/null -w '%{http_code}' \
        -u "eden:${GITEA_REMOTE_PASSWORD}" \
        -X POST "http://localhost:${GITEA_HOST_PORT}/api/v1/user/repos" \
        -H "Content-Type: application/json" \
        -d "{\"name\":\"${EXPERIMENT_ID}\",\"private\":true,\"auto_init\":false}" \
        || true
)
case "$http_status" in
    201|409) ;;
    *)
        echo "gitea repo create failed (http=$http_status)" >&2
        exit 1
        ;;
esac

# --- Generate the credential-helper script ---
echo "--- writing credential-helper script ---" >&2
mkdir -p "${COMPOSE_DIR}/.gitea-creds-${EXPERIMENT_ID}"
chmod 0755 "${COMPOSE_DIR}/.gitea-creds-${EXPERIMENT_ID}"
HELPER_PATH="${COMPOSE_DIR}/.gitea-creds-${EXPERIMENT_ID}/credential-helper.sh"
cat >"$HELPER_PATH" <<HELPER
#!/bin/sh
# Generated by setup-experiment.sh — do not edit. Regenerate by
# re-running setup-experiment.
case "\$1" in
  get)
    cat <<'EOF_INNER'
username=eden
password=__GITEA_REMOTE_PASSWORD__
EOF_INNER
    ;;
esac
HELPER
# Substitute the password into the heredoc (we use a sentinel so the
# heredoc doesn't expand variables in its body — keeps shell-special
# characters in the password from breaking the helper).
sed -i.bak "s|__GITEA_REMOTE_PASSWORD__|${GITEA_REMOTE_PASSWORD}|" "$HELPER_PATH"
rm -f "${HELPER_PATH}.bak"
chmod 0755 "$HELPER_PATH"

# --- Seed the bare-repo volume + push to Gitea ---
echo "--- seeding bare-repo volume + pushing seed to gitea ---" >&2
# eden-repo-init's --push-to flag uses the credential helper at
# /etc/eden/credential-helper.sh (bind-mounted via the run command
# below). The `--no-deps` ensures we don't accidentally start
# unrelated services for the seed step.
GITEA_REMOTE_URL_SHELL="http://gitea:3000/eden/${EXPERIMENT_ID}.git"
SEED_FROM_ABS=""
SEED_FROM_MOUNT_ARGS=()
SEED_FROM_PYTHON_ARGS=()
if [[ -n "$ARG_SEED_FROM" ]]; then
    if [[ ! -d "$ARG_SEED_FROM" ]]; then
        echo "--seed-from path is not a directory: $ARG_SEED_FROM" >&2
        exit 2
    fi
    SEED_FROM_ABS="$(cd "$ARG_SEED_FROM" && pwd)"
    SEED_FROM_MOUNT_ARGS=(-v "${SEED_FROM_ABS}:/seed:ro")
    SEED_FROM_PYTHON_ARGS=(--seed-from /seed)
    echo "--- seeding from host directory: ${SEED_FROM_ABS} ---" >&2
    # Drop any prior staging volume so repo-init doesn't short-circuit
    # via EDEN_REPO_ALREADY_SEEDED and silently ignore the new --seed-from
    # content. See MANUAL_UI_ISSUES.md §8. The volume has explicit
    # `name: eden-repo-init-staging` in compose.yaml so this rm works
    # regardless of the compose project name.
    docker volume rm eden-repo-init-staging >/dev/null 2>&1 || true
fi
# `${arr[@]+"${arr[@]}"}` is the bash-3.2-safe expansion for arrays
# that may be empty under `set -u`.
SEED_OUTPUT="$(cd "$COMPOSE_DIR" && \
    docker compose --env-file "$ENV_FILE" run --rm --no-deps \
        -v "${HELPER_PATH}:/etc/eden/credential-helper.sh:ro" \
        ${SEED_FROM_MOUNT_ARGS[@]+"${SEED_FROM_MOUNT_ARGS[@]}"} \
        eden-repo-init \
        python -m eden_service_common.repo_init \
            --repo-path /var/lib/eden/repo \
            --push-to "${GITEA_REMOTE_URL_SHELL}" \
            --credential-helper /etc/eden/credential-helper.sh \
            ${SEED_FROM_PYTHON_ARGS[@]+"${SEED_FROM_PYTHON_ARGS[@]}"})"
SEED_SHA="$(echo "$SEED_OUTPUT" | sed -nE 's/^EDEN_REPO_(SEEDED|ALREADY_SEEDED) sha=([0-9a-f]{40})$/\2/p' | head -n1)"
if [[ -z "$SEED_SHA" ]]; then
    echo "failed to parse seed SHA from eden-repo-init output:" >&2
    echo "$SEED_OUTPUT" >&2
    exit 1
fi

# Replace the placeholder zeros with the real SHA so the file has
# exactly one definition (later definitions in env files don't
# silently override earlier ones for every consumer — `grep ^X=`
# patterns are happiest with a unique line). Use a portable sed
# invocation that works on both BSD and GNU.
TMP_REPLACE="$(mktemp)"
sed -E "s/^EDEN_BASE_COMMIT_SHA=.*/EDEN_BASE_COMMIT_SHA=${SEED_SHA}/" \
    "$ENV_FILE" > "$TMP_REPLACE"
mv "$TMP_REPLACE" "$ENV_FILE"

# --- 12a-2 wave 7: reserved-group + initial-admin bootstrap ---
#
# Per plan §5.7: register the `admins` and `orchestrators` groups
# (both reserved per chapter 02 §7.5) and seed the initial admin
# worker into `admins`. The `orchestrators` group is created empty;
# auto-orchestrator instances populate it themselves at startup via
# `_ensure_orchestrators_membership` (12a-2 wave 4).
#
# All three calls are admin-token-authenticated and idempotent on
# existing record (per 12a-1 §D.1 / §D.2), so re-running setup on an
# already-configured experiment is safe. We swallow 409 on
# register_group (group exists) and 200 on register_worker
# (idempotent re-registration returns the existing record without a
# new token).
echo "--- bringing up task-store-server for group bootstrap ---" >&2
(cd "$COMPOSE_DIR" && docker compose --env-file "$ENV_FILE" \
    up -d --wait task-store-server >&2)

bootstrap_curl() {
    # Issue an admin-token-authenticated wire call from inside the
    # task-store-server container; that keeps the bootstrap working
    # whether or not the host has curl, and avoids guessing at the
    # exposed port (the container always listens on 8080).
    local method="$1" path="$2" body="${3:-}"
    local args=(
        -fsS -o /dev/null -w '%{http_code}'
        -X "$method"
        -H "Authorization: Bearer admin:${EDEN_ADMIN_TOKEN}"
        -H "X-Eden-Experiment-Id: ${EXPERIMENT_ID}"
        -H "Content-Type: application/json"
    )
    if [[ -n "$body" ]]; then
        args+=(-d "$body")
    fi
    args+=("http://localhost:8080${path}")
    docker compose -f compose.yaml --env-file "$ENV_FILE" \
        exec -T task-store-server curl "${args[@]}" || true
}

echo "--- registering reserved groups + initial admin worker ---" >&2
EXP_BASE="/v0/experiments/${EXPERIMENT_ID}"

# 1. register_group("orchestrators") — accept 200 (created) or 409
# (already exists; orchestrator may have raced us, or this is a
# re-run).
rc=$(bootstrap_curl POST "${EXP_BASE}/groups" \
    "{\"group_id\":\"orchestrators\"}")
case "$rc" in
    200|409) ;;
    *) echo "register_group(orchestrators) failed: http=$rc" >&2; exit 1 ;;
esac

# 2. register_group("admins") — same idempotency posture.
rc=$(bootstrap_curl POST "${EXP_BASE}/groups" \
    "{\"group_id\":\"admins\"}")
case "$rc" in
    200|409) ;;
    *) echo "register_group(admins) failed: http=$rc" >&2; exit 1 ;;
esac

# 3. register_worker(EDEN_ADMINS_INITIAL_MEMBER) — admin-gated. Per
# chapter 02 §6.3, re-registration of an existing worker_id returns
# the existing record (200) without a fresh registration_token. We
# intentionally do NOT capture the returned token here; the operator
# acquires a credential via the documented reissue path
# (`reference/scripts/setup-experiment/README.md` will be updated to
# point at the `eden_orchestrator` host's cred dir for inspection).
rc=$(bootstrap_curl POST "${EXP_BASE}/workers" \
    "{\"worker_id\":\"${EDEN_ADMINS_INITIAL_MEMBER}\"}")
case "$rc" in
    200) ;;
    *) echo "register_worker(${EDEN_ADMINS_INITIAL_MEMBER}) failed: http=$rc" >&2; exit 1 ;;
esac

# 4. add_to_group(initial-admin, "admins") — admin-gated; idempotent
# on existing membership.
rc=$(bootstrap_curl POST "${EXP_BASE}/groups/admins/members" \
    "{\"member_id\":\"${EDEN_ADMINS_INITIAL_MEMBER}\"}")
case "$rc" in
    200) ;;
    *) echo "add_to_group(admins, ${EDEN_ADMINS_INITIAL_MEMBER}) failed: http=$rc" >&2; exit 1 ;;
esac

# 5. register_worker(EDEN_WEB_UI_WORKER_ID) — admin-gated, idempotent
# (re-registration returns existing record without a fresh token).
# Pre-registering here means the web-ui container's startup
# bootstrap_worker_credential will see an existing row and reissue
# (per §8.2: no fall-through to fresh register on existing record).
rc=$(bootstrap_curl POST "${EXP_BASE}/workers" \
    "{\"worker_id\":\"${EDEN_WEB_UI_WORKER_ID}\"}")
case "$rc" in
    200) ;;
    *) echo "register_worker(${EDEN_WEB_UI_WORKER_ID}) failed: http=$rc" >&2; exit 1 ;;
esac

# 6. add_to_group(web-ui, "admins") — admin-gated; idempotent on
# existing membership. The web-ui's StoreClient bearer is its own
# worker_id, so PATCH /dispatch_mode + POST /tasks/{T}/reassign
# routes (both admins-gated per §3.7) need this membership to land.
rc=$(bootstrap_curl POST "${EXP_BASE}/groups/admins/members" \
    "{\"member_id\":\"${EDEN_WEB_UI_WORKER_ID}\"}")
case "$rc" in
    200) ;;
    *) echo "add_to_group(admins, ${EDEN_WEB_UI_WORKER_ID}) failed: http=$rc" >&2; exit 1 ;;
esac

# 7. Pre-register the headless worker host worker_ids (ideator-1,
# executor-1, evaluator-1) so they're known to the registry from
# bootstrap. Each worker host's own startup
# `bootstrap_worker_credential` will see the existing row and reissue
# per §8.2. Pre-registering matters for the wave-5 reassign route's
# unknown-target check (the route validates `new_target` against the
# live worker registry); without it, e2e drills that reassign a task
# to a worker that hasn't yet self-registered see `error=unknown-target`.
# The set of worker_ids is the reference deployment's known shape;
# operators with different worker_id schemes register theirs the same
# way (admin-gated POST /workers).
for wid in ideator-1 executor-1 evaluator-1; do
    rc=$(bootstrap_curl POST "${EXP_BASE}/workers" \
        "{\"worker_id\":\"${wid}\"}")
    case "$rc" in
        200) ;;
        *) echo "register_worker(${wid}) failed: http=$rc" >&2; exit 1 ;;
    esac
done

echo "--- bootstrap complete: admins + orchestrators groups; initial admin = ${EDEN_ADMINS_INITIAL_MEMBER}; web-ui admin = ${EDEN_WEB_UI_WORKER_ID}; worker hosts pre-registered ---" >&2

# If the operator passed a custom --env-file path, echo a
# next-step that uses an absolute path so they don't have to
# re-derive it. Compose's `--env-file` resolves relative paths
# from the current working directory, so a bare `.env` reference
# would silently disagree with whatever the script wrote.
cat <<EOF
setup-experiment complete.

  experiment id:    ${EXPERIMENT_ID}
  store URL:        ${EDEN_STORE_URL}
  base commit SHA:  ${SEED_SHA}
  env file:         ${ENV_FILE}
  data root:        ${EDEN_EXPERIMENT_DATA_ROOT}

Next steps:

  cd ${COMPOSE_DIR}
  docker compose --env-file ${ENV_FILE} up -d --wait
  open http://localhost:${WEB_UI_HOST_PORT}/

  # Or, to run the workers in subprocess mode (Phase 10d):
  docker compose --env-file ${ENV_FILE} \\
    -f compose.yaml -f compose.subprocess.yaml up -d --wait

Re-running setup-experiment is safe; existing secrets are preserved.
To pick up config changes, re-run setup-experiment + 'docker compose
--env-file ${ENV_FILE} up -d' (recreates services on config drift).
EOF
