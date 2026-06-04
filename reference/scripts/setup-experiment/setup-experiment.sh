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
# EDEN_SESSION_SECRET, FORGEJO_*), reuses the opaque ids minted on the
# first run (EDEN_EXPERIMENT_ID, EDEN_*_WORKER_ID, EDEN_*_GROUP_ID —
# read back from .env so re-running never mints a duplicate identity),
# and re-runs the seed step (which itself short-circuits on a
# previously-seeded repo).
#
# Identity model (#128): the experiment id and all infra worker / group
# ids are opaque, system-minted (exp_* / wkr_* / grp_*) — NOT
# operator-typed. setup-experiment mints the experiment id (or accepts
# one an operator / control-plane already minted via --experiment-id)
# and registers the per-experiment infra workers (operator,
# orchestrator, web-ui-1, ideator-host-1, executor-host-1,
# evaluator-host-1) and reserved groups (admins, orchestrators) under
# the admin bearer, capturing each server-minted id into .env.
#
# Usage:
#   setup-experiment.sh <config.yaml> [--experiment-id <exp_*>]
#                                     [--admin-token <T>]
#                                     [--postgres-password <P>]
#                                     [--env-file <path>]
#                                     [--experiment-dir <path>]
#                                     [--ideas-per-ideation <N>]

usage() {
    cat <<'EOF' >&2
Usage:
  setup-experiment.sh <config.yaml>
                      [--experiment-id <exp_*>]
                      [--admin-token <T>]
                      [--postgres-password <P>]
                      [--env-file <path>]
                      [--experiment-dir <path>]
                      [--data-root <path>]
                      [--ideas-per-ideation <N>]
                      [--exec-mode {host,docker}]
                      [--seed-from <host-dir>]
                      [--no-auto-host-workers]

Generates `reference/compose/.env` and copies <config.yaml> to
`reference/compose/experiment-config.yaml`, then seeds the bare
repo via a one-shot `compose run --rm --no-deps eden-repo-init`
call. Operator's next step is `docker compose up -d --wait`.

--data-root specifies the host-side parent directory under which
every durable substrate (postgres data, forgejo data, artifacts,
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
ARG_NO_AUTO_HOST_WORKERS=""

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
        --no-auto-host-workers) ARG_NO_AUTO_HOST_WORKERS="1";                        shift ;;
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

# --- Secret helpers ---
gen_hex() {
    # 32 bytes of hex.
    python3 -c 'import secrets,sys; sys.stdout.write(secrets.token_hex(int(sys.argv[1])))' "${1:-32}"
}

# --- #128 opaque-id minting (Crockford base32 ULID) ---
# Mint an opaque id of shape "<prefix>_<26-char-ULID>" matching the
# spec/v0/02-data-model.md §1.6 grammar (^<prefix>_[0-9a-hjkmnp-tv-z]{26}$).
# Prefix is one of exp / wkr / grp. The 26-char suffix is a 48-bit ms
# timestamp + 80-bit random, encoded with the lowercase Crockford
# alphabet (no i/l/o/u). This mirrors eden_contracts.mint_opaque_id so a
# setup-minted id is indistinguishable from a server-minted one.
mint_opaque_id() {
    python3 - "$1" <<'PY'
import secrets, sys, time
prefix = sys.argv[1]
alphabet = "0123456789abcdefghjkmnpqrstvwxyz"
value = ((int(time.time() * 1000) & ((1 << 48) - 1)) << 80) | secrets.randbits(80)
suffix = "".join(alphabet[(value >> (5 * i)) & 31] for i in range(26))[::-1]
sys.stdout.write(f"{prefix}_{suffix}")
PY
}

# Read a key from an existing env file, returning empty if absent.
read_env_key() {
    local key="$1" file="$2"
    if [[ -f "$file" ]]; then
        # Strip surrounding quotes that an operator may have added.
        sed -n "s/^${key}=\(.*\)$/\1/p" "$file" | head -n1
    fi
}

# --- Resolve the opaque experiment id (#128) ---
# The experiment id is now an opaque, system-minted `exp_*` id (no
# longer the operator-typed mnemonic from the config's parent dir).
# Precedence:
#   1. --experiment-id flag (an operator / control-plane that already
#      minted an `exp_*`; we do NOT validate the grammar here — the
#      task-store-server rejects an ill-formed id at first use).
#   2. EDEN_EXPERIMENT_ID in an existing .env (idempotent re-run: reuse
#      the previously-minted id; re-minting would orphan the prior
#      data root + registry).
#   3. Mint a fresh `exp_*`.
if [[ -z "$EXPERIMENT_ID" ]]; then
    EXISTING_EXPERIMENT_ID="$(read_env_key EDEN_EXPERIMENT_ID "$ENV_FILE")"
    if [[ -n "$EXISTING_EXPERIMENT_ID" ]]; then
        EXPERIMENT_ID="$EXISTING_EXPERIMENT_ID"
    else
        EXPERIMENT_ID="$(mint_opaque_id exp)"
    fi
fi

# --- Resolve / preserve / generate secrets ---
EXISTING_POSTGRES_PASSWORD="$(read_env_key POSTGRES_PASSWORD "$ENV_FILE")"
POSTGRES_PASSWORD="${ARG_POSTGRES_PASSWORD:-${EXISTING_POSTGRES_PASSWORD:-$(gen_hex 32)}}"

EXISTING_ADMIN_TOKEN="$(read_env_key EDEN_ADMIN_TOKEN "$ENV_FILE")"
EDEN_ADMIN_TOKEN="${ARG_ADMIN_TOKEN:-${EXISTING_ADMIN_TOKEN:-$(gen_hex 32)}}"

EXISTING_SESSION_SECRET="$(read_env_key EDEN_SESSION_SECRET "$ENV_FILE")"
EDEN_SESSION_SECRET="${EXISTING_SESSION_SECRET:-$(gen_hex 32)}"

EXISTING_FORGEJO_SECRET_KEY="$(read_env_key FORGEJO_SECRET_KEY "$ENV_FILE")"
FORGEJO_SECRET_KEY="${EXISTING_FORGEJO_SECRET_KEY:-$(gen_hex 32)}"

EXISTING_FORGEJO_INTERNAL_TOKEN="$(read_env_key FORGEJO_INTERNAL_TOKEN "$ENV_FILE")"
FORGEJO_INTERNAL_TOKEN="${EXISTING_FORGEJO_INTERNAL_TOKEN:-$(gen_hex 32)}"

# Phase 10d follow-up B: per-experiment Forgejo password for the eden
# user. Workers use this via HTTP Basic auth (the credential-helper
# script is generated from a template after Forgejo is up). Preserved
# across re-runs.
EXISTING_FORGEJO_REMOTE_PASSWORD="$(read_env_key FORGEJO_REMOTE_PASSWORD "$ENV_FILE")"
FORGEJO_REMOTE_PASSWORD="${EXISTING_FORGEJO_REMOTE_PASSWORD:-$(gen_hex 32)}"

# Phase 12a-1f: per-experiment password for the eden_readonly
# Postgres role. The task-store-server provisions the role at
# startup via ensure_readonly_role (REVOKE-then-GRANT, idempotent;
# rotates the password if this value changes). Preserved across
# re-runs so existing worker subprocesses can keep using the DSN.
EXISTING_READONLY_PASSWORD="$(read_env_key EDEN_READONLY_PASSWORD "$ENV_FILE")"
EDEN_READONLY_PASSWORD="${EXISTING_READONLY_PASSWORD:-$(gen_hex 32)}"

EXISTING_PG_HOST_PORT="$(read_env_key POSTGRES_HOST_PORT "$ENV_FILE")"
POSTGRES_HOST_PORT="${EXISTING_PG_HOST_PORT:-5433}"

EXISTING_FORGEJO_HOST_PORT="$(read_env_key FORGEJO_HOST_PORT "$ENV_FILE")"
FORGEJO_HOST_PORT="${EXISTING_FORGEJO_HOST_PORT:-3001}"

EXISTING_FORGEJO_SSH_HOST_PORT="$(read_env_key FORGEJO_SSH_HOST_PORT "$ENV_FILE")"
FORGEJO_SSH_HOST_PORT="${EXISTING_FORGEJO_SSH_HOST_PORT:-2222}"

EXISTING_WEB_UI_HOST_PORT="$(read_env_key WEB_UI_HOST_PORT "$ENV_FILE")"
WEB_UI_HOST_PORT="${EXISTING_WEB_UI_HOST_PORT:-8090}"

# Issue #110: log-search overlay (compose.logging.yaml) secrets/knobs.
# Generated/preserved unconditionally (decision §2.4) — negligible when
# the overlay isn't used (one unused secret line, one empty dir each
# for loki/ + alloy/), and keeps bring-up frictionless: no --with-logging
# flag, just add `-f compose.logging.yaml`. Grafana admin password
# follows the same generate-or-preserve path as every other secret.
EXISTING_GRAFANA_ADMIN_PASSWORD="$(read_env_key EDEN_GRAFANA_ADMIN_PASSWORD "$ENV_FILE")"
EDEN_GRAFANA_ADMIN_PASSWORD="${EXISTING_GRAFANA_ADMIN_PASSWORD:-$(gen_hex 32)}"

EXISTING_GRAFANA_HOST_PORT="$(read_env_key GRAFANA_HOST_PORT "$ENV_FILE")"
GRAFANA_HOST_PORT="${EXISTING_GRAFANA_HOST_PORT:-3000}"

# Issue #133: ideation policy moved from env vars to the experiment
# config's `ideation_policy` block. The orchestrator reads
# /etc/eden/experiment-config.yaml at startup; `setup-experiment.sh`
# copies the operator's YAML to that path (see below). Smoke scripts
# that previously sed-edited EDEN_IDEATION_POLICY_* now edit the
# experiment config YAML directly.

# --- #128 infra worker / group ids: read-from-.env, mint-later ---
# Under the opaque-id model setup-experiment is the SOLE minter of the
# per-experiment infra workers (operator, orchestrator, web-ui-1,
# ideator-host-1, executor-host-1, evaluator-host-1) and the two
# reserved groups (admins, orchestrators). The server mints the id at
# register-time and returns it; the operator no longer picks it.
#
# Idempotency now lives in `.env`: on a re-run we REUSE any id already
# present (registering again would mint a NEW id and orphan the prior
# registry row + credential). An empty value here means "not yet
# minted" — the reserved-group + initial-admin bootstrap block below
# mints it and writes it back to `.env`. The worker NAMES are the
# stable role labels (also the reserved literals for the groups).
EDEN_ORCHESTRATOR_WORKER_ID="$(read_env_key EDEN_ORCHESTRATOR_WORKER_ID "$ENV_FILE")"
EDEN_ADMINS_INITIAL_MEMBER="$(read_env_key EDEN_ADMINS_INITIAL_MEMBER "$ENV_FILE")"
EDEN_WEB_UI_WORKER_ID="$(read_env_key EDEN_WEB_UI_WORKER_ID "$ENV_FILE")"
EDEN_IDEATOR_HOST_WORKER_ID="$(read_env_key EDEN_IDEATOR_HOST_WORKER_ID "$ENV_FILE")"
EDEN_EXECUTOR_HOST_WORKER_ID="$(read_env_key EDEN_EXECUTOR_HOST_WORKER_ID "$ENV_FILE")"
EDEN_EVALUATOR_HOST_WORKER_ID="$(read_env_key EDEN_EVALUATOR_HOST_WORKER_ID "$ENV_FILE")"
EDEN_ADMINS_GROUP_ID="$(read_env_key EDEN_ADMINS_GROUP_ID "$ENV_FILE")"
EDEN_ORCHESTRATORS_GROUP_ID="$(read_env_key EDEN_ORCHESTRATORS_GROUP_ID "$ENV_FILE")"

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
# substrate (postgres, forgejo, artifacts, per-host bare clones,
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
# postgres initdb) and forgejo's `conf/` directory (created when the
# forgejo container first boots). Either is sufficient evidence the
# operator has actually run the stack, not just bootstrapped the
# tree. Empty subdirs from an aborted setup don't trip this.
if [[ -n "$EXISTING_DATA_ROOT" && "$EXISTING_DATA_ROOT" != "$EDEN_EXPERIMENT_DATA_ROOT" ]]; then
    if [[ -f "$EXISTING_DATA_ROOT/postgres/PG_VERSION" \
        || -d "$EXISTING_DATA_ROOT/forgejo/conf" ]]; then
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
# different uids (postgres=70, forgejo-rootless=1000, eden=1000) and
# Docker Desktop's uid mapping layer makes a single chown choice
# fragile. World-writable is acceptable for local-dev; production
# durability lives in Phase 13's managed substrates.
mkdir -p \
    "${EDEN_EXPERIMENT_DATA_ROOT}/postgres" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/forgejo" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/forgejo-etc" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/artifacts" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/orchestrator-repo" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/executor-repo" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/evaluator-repo" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/web-ui-repo" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/ideator-repo" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/checkpoints" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/credentials/orchestrator" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/credentials/ideator" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/credentials/executor" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/credentials/evaluator" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/credentials/web-ui" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/credentials/operator" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/web-ui-configs" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/web-ui-repos" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/logs/task-store-server" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/logs/orchestrator" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/logs/ideator-host" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/logs/executor-host" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/logs/evaluator-host" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/logs/web-ui" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/logs/control-plane" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/loki" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/alloy"
# Issue #110: loki/ + alloy/ are DERIVED / observability storage for the
# opt-in compose.logging.yaml overlay — NOT protocol-owned durable state
# (chapter 01 §13). loki/ holds Loki's index+chunks (a queryable
# projection of logs/); alloy/ holds Alloy's file-tail positions. They
# are created unconditionally (created here even for experiments that
# never run the overlay; they stay empty until it does) and wiped by the
# same `rm -rf ${EDEN_EXPERIMENT_DATA_ROOT}` reset. chmod 0777 because
# Loki runs as uid 10001 (12a-1g multi-uid precedent).
if ! chmod 0777 \
    "${EDEN_EXPERIMENT_DATA_ROOT}" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/postgres" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/forgejo" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/forgejo-etc" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/artifacts" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/orchestrator-repo" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/executor-repo" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/evaluator-repo" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/web-ui-repo" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/ideator-repo" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/checkpoints" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/credentials" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/credentials/orchestrator" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/credentials/ideator" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/credentials/executor" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/credentials/evaluator" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/credentials/web-ui" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/credentials/operator" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/web-ui-configs" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/web-ui-repos" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/logs" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/logs/task-store-server" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/logs/orchestrator" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/logs/ideator-host" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/logs/executor-host" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/logs/evaluator-host" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/logs/web-ui" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/logs/control-plane" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/loki" \
    "${EDEN_EXPERIMENT_DATA_ROOT}/alloy" 2>/dev/null
then
    # Best-effort: the script continues. But warn the operator so a
    # follow-up "compose up postgres-permission-denied" failure
    # surfaces a recognizable root cause instead of a generic error.
    echo "warning: chmod 0777 on substrate dirs under" >&2
    echo "         $EDEN_EXPERIMENT_DATA_ROOT" >&2
    echo "         partially failed. Containers running as non-root" >&2
    echo "         uids (postgres=70, forgejo=1000, eden=1000) may fail" >&2
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

# Issue #145: also drop the config into the web-ui's per-experiment
# config dir as <experiment_id>.yaml. The web-ui's experiment switcher
# loads non-default experiments' objective / evaluation_schema from this
# dir (the deployment default still reads --experiment-config). Idempotent
# overwrite; running setup-experiment per experiment populates the dir.
cp "$CONFIG_PATH" "${EDEN_EXPERIMENT_DATA_ROOT}/web-ui-configs/${EXPERIMENT_ID}.yaml"

# --- Write the partial .env (no EDEN_BASE_COMMIT_SHA yet) ---
# Postgres DSN points at the in-network postgres service hostname.
# Percent-encode the password so user-supplied passwords containing
# reserved URI characters (`@`, `:`, `/`, `?`, `#`, …) don't break
# the DSN. The auto-generated 32-byte hex passwords are
# percent-encoding-safe by construction; this matters only when an
# operator passes `--postgres-password <raw-string>`.
POSTGRES_PASSWORD_ENC="$(python3 -c 'import sys, urllib.parse; sys.stdout.write(urllib.parse.quote(sys.argv[1], safe=""))' "$POSTGRES_PASSWORD")"
EDEN_STORE_URL="postgresql://eden:${POSTGRES_PASSWORD_ENC}@postgres:5432/eden"

# Phase 12a-1f: readonly DSN for the eden_readonly Postgres role.
# Same percent-encoding posture as the eden superuser password.
EDEN_READONLY_PASSWORD_ENC="$(python3 -c 'import sys, urllib.parse; sys.stdout.write(urllib.parse.quote(sys.argv[1], safe=""))' "$EDEN_READONLY_PASSWORD")"
EDEN_READONLY_STORE_URL="postgresql://eden_readonly:${EDEN_READONLY_PASSWORD_ENC}@postgres:5432/eden"

# #147: control-plane store DSN. Same Postgres instance as the task
# store, a SEPARATE logical database (chapter 11 §3.4 Option A) created
# by the postgres init hook (reference/compose/init-control-plane-db.sh).
# Same percent-encoded eden-superuser password as EDEN_STORE_URL.
POSTGRES_DB_CONTROL_PLANE="eden_control_plane"
EDEN_CONTROL_PLANE_STORE_URL="postgresql://eden:${POSTGRES_PASSWORD_ENC}@postgres:5432/${POSTGRES_DB_CONTROL_PLANE}"

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
FORGEJO_SECRET_KEY=${FORGEJO_SECRET_KEY}
FORGEJO_INTERNAL_TOKEN=${FORGEJO_INTERNAL_TOKEN}
FORGEJO_HOST_PORT=${FORGEJO_HOST_PORT}
FORGEJO_SSH_HOST_PORT=${FORGEJO_SSH_HOST_PORT}

# --- 10b/c reference services ---
EDEN_EXPERIMENT_ID=${EXPERIMENT_ID}
EDEN_ADMIN_TOKEN=${EDEN_ADMIN_TOKEN}
EDEN_SESSION_SECRET=${EDEN_SESSION_SECRET}
EDEN_STORE_URL=${EDEN_STORE_URL}
WEB_UI_HOST_PORT=${WEB_UI_HOST_PORT}

# --- Log search UI (Loki/Alloy/Grafana overlay, issue #110) ---
# Used only by compose.logging.yaml (opt-in). Generated/preserved
# unconditionally so the overlay works against any bootstrapped stack
# with no extra setup flag. EDEN_LOGGING_DOCKER_GID is intentionally
# absent here — it is required ONLY for the optional infra-stdout
# overlay (compose.logging-infra.yaml) and the operator supplies it at
# bring-up time (see that overlay's header + .env.example).
EDEN_GRAFANA_ADMIN_PASSWORD=${EDEN_GRAFANA_ADMIN_PASSWORD}
GRAFANA_HOST_PORT=${GRAFANA_HOST_PORT}

# --- 12a-1g substrate data root ---
# Host-side parent dir under which every durable substrate
# (postgres, forgejo, artifacts, *-repo, credentials/*) is
# bind-mounted. See chapter 01 §13 + docs/operations/
# experiment-data-durability.md. Override via setup-experiment.sh
# --data-root <path>.
EDEN_EXPERIMENT_DATA_ROOT=${EDEN_EXPERIMENT_DATA_ROOT}

# --- #128 opaque infra worker + group ids ---
# All system-minted (wkr_... / grp_...) by the reserved-group +
# initial-admin bootstrap block at the end of this script. On the
# FIRST run these are written empty here and then filled in-place once
# the task-store-server is up and has minted them; on a re-run they
# carry the previously-minted ids (idempotent reuse). Do NOT hand-edit
# — the value is the registry's primary key and the per-host credential
# filename (<id>.token) under the credentials/* data-root dirs.
EDEN_ORCHESTRATOR_WORKER_ID=${EDEN_ORCHESTRATOR_WORKER_ID}
EDEN_ADMINS_INITIAL_MEMBER=${EDEN_ADMINS_INITIAL_MEMBER}
EDEN_WEB_UI_WORKER_ID=${EDEN_WEB_UI_WORKER_ID}
EDEN_IDEATOR_HOST_WORKER_ID=${EDEN_IDEATOR_HOST_WORKER_ID}
EDEN_EXECUTOR_HOST_WORKER_ID=${EDEN_EXECUTOR_HOST_WORKER_ID}
EDEN_EVALUATOR_HOST_WORKER_ID=${EDEN_EVALUATOR_HOST_WORKER_ID}
EDEN_ADMINS_GROUP_ID=${EDEN_ADMINS_GROUP_ID}
EDEN_ORCHESTRATORS_GROUP_ID=${EDEN_ORCHESTRATORS_GROUP_ID}

# --- 12c / #147 control plane ---
# The control-plane service is always-on in compose.yaml. Its Postgres
# store is a separate logical database created by the postgres init
# hook. EDEN_CONTROL_PLANE_URL is intentionally LEFT EMPTY here so the
# default stack (and the existing six smokes) run single-experiment and
# ignore the control plane; the lease-handoff smoke appends a non-empty
# value to flip the orchestrator + web-ui into lease-driven mode.
POSTGRES_DB_CONTROL_PLANE=${POSTGRES_DB_CONTROL_PLANE}
EDEN_CONTROL_PLANE_STORE_URL=${EDEN_CONTROL_PLANE_STORE_URL}
EDEN_CONTROL_PLANE_URL=

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

# --- 10d follow-up B: Forgejo-as-remote ---
# The workers' git remote is the in-network Forgejo container. Workers
# clone bare on first run and fetch/push thereafter; the integrator
# publishes variant/* refs back to Forgejo per chapter 6 §3.4.
FORGEJO_REMOTE_PASSWORD=${FORGEJO_REMOTE_PASSWORD}
FORGEJO_REMOTE_URL=http://forgejo:3000/eden/${EXPERIMENT_ID}.git
EDEN_FORGEJO_CREDS_DIR_HOST=${COMPOSE_DIR}/.forgejo-creds-${EXPERIMENT_ID}

# --- 12a-1f substrate access (ideator + evaluator subprocesses) ---
# The task-store-server provisions the eden_readonly Postgres role
# on startup using EDEN_READONLY_PASSWORD; agentic subprocesses
# connect via EDEN_READONLY_STORE_URL. The artifact-server route is
# rooted at EDEN_ARTIFACT_URL with files under
# EDEN_ARTIFACT_PATH_ROOT on disk (both sides bind-mount the
# eden-artifacts-data volume at the same target path, so the
# subprocess's file://URI → URL translation is mechanical). See
# spec/v0/reference-bindings/worker-host-subprocess.md §9.
EDEN_READONLY_PASSWORD=${EDEN_READONLY_PASSWORD}
EDEN_READONLY_STORE_URL=${EDEN_READONLY_STORE_URL}
EDEN_ARTIFACT_URL=http://task-store-server:8080/_reference/experiments/${EXPERIMENT_ID}/artifacts/
EDEN_ARTIFACT_PATH_ROOT=/var/lib/eden/artifacts

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

# --- Phase 10d follow-up B: bring Forgejo up + provision ---
echo "--- bringing up forgejo synchronously ---" >&2
(cd "$COMPOSE_DIR" && docker compose --env-file "$ENV_FILE" up -d --wait forgejo >&2)

# Idempotent admin-user create. `forgejo admin user create` exits
# non-zero if the user exists; the change-password fallback handles
# the re-run case.
echo "--- provisioning forgejo eden user ---" >&2
if ! (cd "$COMPOSE_DIR" && docker compose --env-file "$ENV_FILE" \
        exec -T forgejo forgejo admin user create \
            --username eden \
            --password "$FORGEJO_REMOTE_PASSWORD" \
            --email eden@invalid \
            --admin \
            --must-change-password=false) \
        >&2 2>&1
then
    (cd "$COMPOSE_DIR" && docker compose --env-file "$ENV_FILE" \
        exec -T forgejo forgejo admin user change-password \
            --username eden \
            --password "$FORGEJO_REMOTE_PASSWORD" \
            --must-change-password=false) >&2
fi

echo "--- creating forgejo repo eden/${EXPERIMENT_ID} ---" >&2
# 201 on first create, 409 on re-run; both are acceptable.
http_status=$(
    curl -fsS -o /dev/null -w '%{http_code}' \
        -u "eden:${FORGEJO_REMOTE_PASSWORD}" \
        -X POST "http://localhost:${FORGEJO_HOST_PORT}/api/v1/user/repos" \
        -H "Content-Type: application/json" \
        -d "{\"name\":\"${EXPERIMENT_ID}\",\"private\":true,\"auto_init\":false}" \
        || true
)
case "$http_status" in
    201|409) ;;
    *)
        echo "forgejo repo create failed (http=$http_status)" >&2
        exit 1
        ;;
esac

# --- Generate the credential-helper script ---
echo "--- writing credential-helper script ---" >&2
mkdir -p "${COMPOSE_DIR}/.forgejo-creds-${EXPERIMENT_ID}"
chmod 0755 "${COMPOSE_DIR}/.forgejo-creds-${EXPERIMENT_ID}"
HELPER_PATH="${COMPOSE_DIR}/.forgejo-creds-${EXPERIMENT_ID}/credential-helper.sh"
cat >"$HELPER_PATH" <<HELPER
#!/bin/sh
# Generated by setup-experiment.sh — do not edit. Regenerate by
# re-running setup-experiment.
case "\$1" in
  get)
    cat <<'EOF_INNER'
username=eden
password=__FORGEJO_REMOTE_PASSWORD__
EOF_INNER
    ;;
esac
HELPER
# Substitute the password into the heredoc (we use a sentinel so the
# heredoc doesn't expand variables in its body — keeps shell-special
# characters in the password from breaking the helper).
sed -i.bak "s|__FORGEJO_REMOTE_PASSWORD__|${FORGEJO_REMOTE_PASSWORD}|" "$HELPER_PATH"
rm -f "${HELPER_PATH}.bak"
chmod 0755 "$HELPER_PATH"

# --- Seed the bare-repo volume + push to Forgejo ---
echo "--- seeding bare-repo volume + pushing seed to forgejo ---" >&2
# eden-repo-init's --push-to flag uses the credential helper at
# /etc/eden/credential-helper.sh (bind-mounted via the run command
# below). The `--no-deps` ensures we don't accidentally start
# unrelated services for the seed step.
FORGEJO_REMOTE_URL_SHELL="http://forgejo:3000/eden/${EXPERIMENT_ID}.git"
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
    # content. Resolved via closed issue #55. The volume has explicit
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
            --push-to "${FORGEJO_REMOTE_URL_SHELL}" \
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

# --- #128: reserved-group + initial-admin + infra-worker bootstrap ---
#
# Under the opaque-id model setup-experiment is the SOLE minter of the
# per-experiment infra workers and reserved groups. The server mints
# every id (`wkr_*` / `grp_*`) at register-time and returns it; we
# capture the minted id (and, for workers, the registration_token),
# persist the id to `.env`, and persist the token to the per-host
# credentials dir so each container's startup
# `bootstrap_worker_credential` finds a usable credential without an
# admin reissue.
#
# Idempotency lives in the .env file: a non-empty EDEN_..._WORKER_ID /
# EDEN_..._GROUP_ID means a prior run already minted it. We REUSE it and
# SKIP re-minting (registering again would mint a NEW id, orphaning the
# prior registry row + credential). Reserved groups are created under
# the admin bearer (which is allowed to mint reserved-named groups);
# ordinary workers cannot.
echo "--- bringing up task-store-server for registry bootstrap ---" >&2
(cd "$COMPOSE_DIR" && docker compose --env-file "$ENV_FILE" \
    up -d --wait task-store-server >&2)

EXP_BASE="/v0/experiments/${EXPERIMENT_ID}"

bootstrap_curl_body() {
    # Issue an admin-token-authenticated wire call from inside the
    # task-store-server container and return the RESPONSE BODY on
    # stdout (so we can parse the server-minted id). HTTP errors fail
    # the script (curl -f → non-zero); the caller decides whether to
    # tolerate that (e.g. a GET ?name= lookup that may 404).
    local method="$1" path="$2" body="${3:-}"
    local args=(
        -fsS
        -X "$method"
        -H "Authorization: Bearer admin:${EDEN_ADMIN_TOKEN}"
        -H "X-Eden-Experiment-Id: ${EXPERIMENT_ID}"
        -H "Content-Type: application/json"
    )
    if [[ -n "$body" ]]; then
        args+=(-d "$body")
    fi
    args+=("http://localhost:8080${path}")
    (cd "$COMPOSE_DIR" && docker compose --env-file "$ENV_FILE" \
        exec -T task-store-server curl "${args[@]}")
}

# Parse a single top-level string field out of a JSON object body
# using python3 (guaranteed present; jq is not on the host in every
# environment). Empty stdout if absent/null.
json_field() {
    python3 -c 'import json,sys; d=json.load(sys.stdin); v=d.get(sys.argv[1]); sys.stdout.write(v if isinstance(v,str) else "")' "$1"
}

# Replace KEY=… in $ENV_FILE in place (the line always exists — it was
# written by the heredoc above, possibly empty). Portable sed for BSD
# + GNU; the value is an opaque id (no sed-special chars), so a plain
# s||| with `|` delimiter is safe.
upsert_env_key() {
    local key="$1" value="$2" tmp
    tmp="$(mktemp)"
    sed -E "s|^${key}=.*|${key}=${value}|" "$ENV_FILE" > "$tmp"
    mv "$tmp" "$ENV_FILE"
}

# Persist a per-worker credential token to <credentials_dir>/<id>.token
# (matches eden_service_common.auth.credential_path).
#
# Mode 0644 (world-READABLE), NOT 0600: setup-experiment runs as the HOST
# user, but the worker-host containers read this token as eden:1000. On a
# Linux native bind-mount the host-uid-owned 0600 file is unreadable by
# eden:1000 (PermissionError at startup); macOS Docker Desktop's uid
# mapping masks this, so 0600 only fails on Linux/CI. 0644 matches the
# already-0777 credentials dir's documented bind-mount posture (this
# reference deployment trades token-file secrecy on the host fs for
# cross-uid host↔container access; a hardened deployment uses matching
# uids or a secrets manager — chapter 01 §13.5 token-storage hygiene).
persist_token() {
    local cred_dir="$1" worker_id="$2" token="$3"
    local path="${EDEN_EXPERIMENT_DATA_ROOT}/credentials/${cred_dir}/${worker_id}.token"
    printf '%s' "$token" > "$path"
    chmod 0644 "$path" 2>/dev/null || true
}

# Mint a reserved group by NAME under the admin bearer (allowed to
# create reserved-named groups) IF its id isn't already in `.env`.
# Echoes the resolved group id. On a re-run with a populated `.env`
# we trust the persisted id (idempotent reuse, no wire call).
mint_group() {
    local name="$1" existing_id="$2" body gid
    if [[ -n "$existing_id" ]]; then
        printf '%s' "$existing_id"
        return 0
    fi
    body="$(bootstrap_curl_body POST "${EXP_BASE}/groups" \
        "{\"name\":\"${name}\"}")"
    gid="$(printf '%s' "$body" | json_field group_id)"
    if [[ -z "$gid" ]]; then
        echo "register_group(name=${name}) returned no group_id: $body" >&2
        exit 1
    fi
    printf '%s' "$gid"
}

# Mint an infra worker by NAME under the admin bearer IF its id isn't
# already in `.env`. Persists the minted id to `.env` (key=$3) and the
# returned registration_token to the per-host credentials dir (=$4).
# Echoes the resolved worker id.
mint_worker() {
    local name="$1" existing_id="$2" env_key="$3" cred_dir="$4" body wid token
    if [[ -n "$existing_id" ]]; then
        printf '%s' "$existing_id"
        return 0
    fi
    body="$(bootstrap_curl_body POST "${EXP_BASE}/workers" \
        "{\"name\":\"${name}\"}")"
    wid="$(printf '%s' "$body" | json_field worker_id)"
    token="$(printf '%s' "$body" | json_field registration_token)"
    if [[ -z "$wid" || -z "$token" ]]; then
        echo "register_worker(name=${name}) missing worker_id/registration_token: $body" >&2
        exit 1
    fi
    upsert_env_key "$env_key" "$wid"
    persist_token "$cred_dir" "$wid" "$token"
    printf '%s' "$wid"
}

# add_to_group(member, group) — admin-gated; idempotent on existing
# membership (200). The URL uses the OPAQUE group id; the body's
# member_id is the opaque worker (or group) id.
add_to_group() {
    local grp_id="$1" member_id="$2"
    bootstrap_curl_body POST "${EXP_BASE}/groups/${grp_id}/members" \
        "{\"member_id\":\"${member_id}\"}" >/dev/null
}

echo "--- minting reserved groups + infra workers ---" >&2

# 1. Reserved groups (admins, orchestrators). `orchestrators` is
# created empty; auto-orchestrator instances add themselves at startup
# via `_ensure_orchestrators_membership`.
EDEN_ORCHESTRATORS_GROUP_ID="$(mint_group orchestrators "$EDEN_ORCHESTRATORS_GROUP_ID")"
upsert_env_key EDEN_ORCHESTRATORS_GROUP_ID "$EDEN_ORCHESTRATORS_GROUP_ID"
EDEN_ADMINS_GROUP_ID="$(mint_group admins "$EDEN_ADMINS_GROUP_ID")"
upsert_env_key EDEN_ADMINS_GROUP_ID "$EDEN_ADMINS_GROUP_ID"

# 2. Initial admin worker ("operator"). Operators acting through the
# web UI authenticate as this worker; it is the principal that drives
# reassign_task / update_dispatch_mode / create_task(kind=execution)
# under the §3.7 admins-gated authority. Its token lands in
# credentials/operator/<id>.token for operator inspection (no
# container consumes it directly).
EDEN_ADMINS_INITIAL_MEMBER="$(mint_worker operator "$EDEN_ADMINS_INITIAL_MEMBER" \
    EDEN_ADMINS_INITIAL_MEMBER operator)"

# 3. web-ui-1 worker — the deployment-level admin actor for the web-ui
# container's StoreClient. Token lands in credentials/web-ui so the
# container's bootstrap_worker_credential reuses it (no admin reissue).
EDEN_WEB_UI_WORKER_ID="$(mint_worker web-ui-1 "$EDEN_WEB_UI_WORKER_ID" \
    EDEN_WEB_UI_WORKER_ID web-ui)"

# 4. orchestrator worker. Token lands in credentials/orchestrator.
EDEN_ORCHESTRATOR_WORKER_ID="$(mint_worker orchestrator "$EDEN_ORCHESTRATOR_WORKER_ID" \
    EDEN_ORCHESTRATOR_WORKER_ID orchestrator)"

# 5. admins-group memberships: initial-admin + web-ui both need it so
# their bearer (their own opaque worker id) passes the §3.7 admins gate
# on PATCH /dispatch_mode + POST /tasks/{T}/reassign.
add_to_group "$EDEN_ADMINS_GROUP_ID" "$EDEN_ADMINS_INITIAL_MEMBER"
add_to_group "$EDEN_ADMINS_GROUP_ID" "$EDEN_WEB_UI_WORKER_ID"

# 6. Headless worker-host workers (ideator-host-1, executor-host-1,
# evaluator-host-1). Pre-minting matters for the reassign route's
# unknown-target check (the route validates new_target against the live
# registry); without it, e2e drills that reassign to a host that hasn't
# yet self-bootstrapped see error=unknown-target. Each host container's
# startup bootstrap_worker_credential reuses the persisted token.
#
# --no-auto-host-workers skips this for fully-manual experiments that
# won't run the auto-host services. Tradeoff: a reassign to one of these
# ids before the host comes up returns error=unknown-target — the right
# failure mode when the host isn't coming.
if [[ -z "$ARG_NO_AUTO_HOST_WORKERS" ]]; then
    EDEN_IDEATOR_HOST_WORKER_ID="$(mint_worker ideator-host-1 "$EDEN_IDEATOR_HOST_WORKER_ID" \
        EDEN_IDEATOR_HOST_WORKER_ID ideator)"
    EDEN_EXECUTOR_HOST_WORKER_ID="$(mint_worker executor-host-1 "$EDEN_EXECUTOR_HOST_WORKER_ID" \
        EDEN_EXECUTOR_HOST_WORKER_ID executor)"
    EDEN_EVALUATOR_HOST_WORKER_ID="$(mint_worker evaluator-host-1 "$EDEN_EVALUATOR_HOST_WORKER_ID" \
        EDEN_EVALUATOR_HOST_WORKER_ID evaluator)"
    bootstrap_summary="worker hosts minted (ideator/executor/evaluator)"
else
    bootstrap_summary="auto-host workers NOT minted (--no-auto-host-workers)"
fi

echo "--- bootstrap complete: admins=${EDEN_ADMINS_GROUP_ID} orchestrators=${EDEN_ORCHESTRATORS_GROUP_ID}; initial admin=${EDEN_ADMINS_INITIAL_MEMBER}; web-ui=${EDEN_WEB_UI_WORKER_ID}; orchestrator=${EDEN_ORCHESTRATOR_WORKER_ID}; ${bootstrap_summary} ---" >&2

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

  minted ids (opaque, system-minted — see ${ENV_FILE}):
    admins group:       ${EDEN_ADMINS_GROUP_ID}
    orchestrators group:${EDEN_ORCHESTRATORS_GROUP_ID}
    operator (admin):   ${EDEN_ADMINS_INITIAL_MEMBER}
    web-ui:             ${EDEN_WEB_UI_WORKER_ID}
    orchestrator:       ${EDEN_ORCHESTRATOR_WORKER_ID}

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
