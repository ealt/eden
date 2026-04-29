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
# existing secrets (POSTGRES_PASSWORD, EDEN_SHARED_TOKEN,
# EDEN_SESSION_SECRET, GITEA_*) and re-runs the seed step (which
# itself short-circuits on a previously-seeded repo).
#
# Usage:
#   setup-experiment.sh <config.yaml> [--experiment-id <id>]
#                                     [--shared-token <T>]
#                                     [--postgres-password <P>]
#                                     [--env-file <path>]
#                                     [--experiment-dir <path>]
#                                     [--proposals-per-plan <N>]

usage() {
    cat <<'EOF' >&2
Usage:
  setup-experiment.sh <config.yaml>
                      [--experiment-id <id>]
                      [--shared-token <T>]
                      [--postgres-password <P>]
                      [--env-file <path>]
                      [--experiment-dir <path>]
                      [--proposals-per-plan <N>]
                      [--exec-mode {host,docker}]

Generates `reference/compose/.env` and copies <config.yaml> to
`reference/compose/experiment-config.yaml`, then seeds the bare
repo via a one-shot `compose run --rm --no-deps eden-repo-init`
call. Operator's next step is `docker compose up -d --wait`.

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
ARG_SHARED_TOKEN=""
ARG_POSTGRES_PASSWORD=""
ARG_EXPERIMENT_DIR=""
ARG_PROPOSALS_PER_PLAN=""
ARG_EXEC_MODE="host"

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
        --shared-token)         require_value "$1" "$#"; ARG_SHARED_TOKEN="$2";      shift 2 ;;
        --postgres-password)    require_value "$1" "$#"; ARG_POSTGRES_PASSWORD="$2"; shift 2 ;;
        --env-file)             require_value "$1" "$#"; ENV_FILE="$2";              shift 2 ;;
        --experiment-dir)       require_value "$1" "$#"; ARG_EXPERIMENT_DIR="$2";    shift 2 ;;
        --proposals-per-plan)   require_value "$1" "$#"; ARG_PROPOSALS_PER_PLAN="$2"; shift 2 ;;
        --exec-mode)            require_value "$1" "$#"; ARG_EXEC_MODE="$2";         shift 2 ;;
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

EXISTING_SHARED_TOKEN="$(read_env_key EDEN_SHARED_TOKEN "$ENV_FILE")"
EDEN_SHARED_TOKEN="${ARG_SHARED_TOKEN:-${EXISTING_SHARED_TOKEN:-$(gen_hex 32)}}"

EXISTING_SESSION_SECRET="$(read_env_key EDEN_SESSION_SECRET "$ENV_FILE")"
EDEN_SESSION_SECRET="${EXISTING_SESSION_SECRET:-$(gen_hex 32)}"

EXISTING_GITEA_SECRET_KEY="$(read_env_key GITEA_SECRET_KEY "$ENV_FILE")"
GITEA_SECRET_KEY="${EXISTING_GITEA_SECRET_KEY:-$(gen_hex 32)}"

EXISTING_GITEA_INTERNAL_TOKEN="$(read_env_key GITEA_INTERNAL_TOKEN "$ENV_FILE")"
GITEA_INTERNAL_TOKEN="${EXISTING_GITEA_INTERNAL_TOKEN:-$(gen_hex 32)}"

EXISTING_PG_HOST_PORT="$(read_env_key POSTGRES_HOST_PORT "$ENV_FILE")"
POSTGRES_HOST_PORT="${EXISTING_PG_HOST_PORT:-5433}"

EXISTING_GITEA_HOST_PORT="$(read_env_key GITEA_HOST_PORT "$ENV_FILE")"
GITEA_HOST_PORT="${EXISTING_GITEA_HOST_PORT:-3001}"

EXISTING_GITEA_SSH_HOST_PORT="$(read_env_key GITEA_SSH_HOST_PORT "$ENV_FILE")"
GITEA_SSH_HOST_PORT="${EXISTING_GITEA_SSH_HOST_PORT:-2222}"

EXISTING_WEB_UI_HOST_PORT="$(read_env_key WEB_UI_HOST_PORT "$ENV_FILE")"
WEB_UI_HOST_PORT="${EXISTING_WEB_UI_HOST_PORT:-8090}"

EXISTING_PLAN_TASKS="$(read_env_key EDEN_PLAN_TASKS "$ENV_FILE")"
EDEN_PLAN_TASKS="${EXISTING_PLAN_TASKS:-3}"

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
EXISTING_PROPOSALS_PER_PLAN="$(read_env_key EDEN_PROPOSALS_PER_PLAN "$ENV_FILE")"
EDEN_PROPOSALS_PER_PLAN="${ARG_PROPOSALS_PER_PLAN:-${EXISTING_PROPOSALS_PER_PLAN:-1}}"

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
EDEN_SHARED_TOKEN=${EDEN_SHARED_TOKEN}
EDEN_SESSION_SECRET=${EDEN_SESSION_SECRET}
EDEN_STORE_URL=${EDEN_STORE_URL}
EDEN_PLAN_TASKS=${EDEN_PLAN_TASKS}
WEB_UI_HOST_PORT=${WEB_UI_HOST_PORT}

# --- 10d subprocess overlay (only used by compose.subprocess.yaml) ---
EDEN_EXPERIMENT_DIR_HOST=${EDEN_EXPERIMENT_DIR_HOST}
EDEN_PROPOSALS_PER_PLAN=${EDEN_PROPOSALS_PER_PLAN}

# --- 10d follow-up A: container-isolated *_command execution ---
# EDEN_EXEC_MODE=host (default) keeps the chunk-10d behavior — user
# commands run on the worker host container. EDEN_EXEC_MODE=docker
# (set via --exec-mode docker) wraps each spawn in a sibling docker
# container via DooD (host /var/run/docker.sock).
EDEN_EXEC_MODE=${EDEN_EXEC_MODE}
EDEN_EXEC_IMAGE=${EDEN_EXEC_IMAGE}
EDEN_DOCKER_GID=${EDEN_DOCKER_GID}
EDEN_CIDFILES_DIR_HOST=${EDEN_CIDFILES_DIR_HOST}

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

# --- Seed the bare-repo volume ---
echo "--- seeding eden-bare-repo volume ---" >&2
SEED_OUTPUT="$(cd "$COMPOSE_DIR" && \
    docker compose --env-file "$ENV_FILE" run --rm --no-deps eden-repo-init)"
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
