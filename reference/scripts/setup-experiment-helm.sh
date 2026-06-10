#!/usr/bin/env bash
set -euo pipefail

# setup-experiment-helm — bootstrap an EDEN experiment on a Helm-deployed stack.
# The Helm equivalent of setup-experiment.sh (Compose), ported to the #128
# opaque-id identity model.
#
# Identity model (#128): the experiment id and every infra worker / group id
# are opaque, system-minted (exp_* / wkr_* / grp_*) — NOT operator-typed. This
# script mints the experiment id (or accepts a pre-minted exp_* via
# --experiment-id), brings the task-store-server up, then mints the reserved
# groups (admins, orchestrators) and the per-service infra workers (operator,
# web-ui, orchestrator, ideator-host, executor-host, evaluator-host) under the
# admin bearer, capturing each minted worker_id + one-time registration_token.
# It hands each worker_id + token to the chart as values.identity.<svc>.* so the
# chart provisions a per-service credential the pod verifies via /whoami.
#
# Because the task-store-server records its seed commit at first experiment-row
# creation, AND identity-consuming pods cannot start before their worker_id is
# minted, the bring-up runs in THREE phases:
#
#   1. `helm upgrade --install` baseCommitSha empty → INFRA tier only (Postgres,
#      Forgejo, + control-plane when lease mode is on).
#   2. Provision the Forgejo eden user, seed the bare repo via a one-shot Job,
#      read the seed SHA, then `helm upgrade --set experiment.baseCommitSha=<sha>`
#      → the task-store-server comes up (still no identity-consuming pods).
#   3. Mint the reserved groups + infra workers against the task-store-server,
#      then `helm upgrade` with the minted identity values → the orchestrator,
#      worker hosts, and web-ui come up with pre-provisioned credentials.
#
# Lease-driven HA mode (orchestrator.leaseMode.enabled, default false) is opt-in
# and DEFERRED + unvalidated behind #281. When enabled, the control-plane is
# deployed and the experiment is registered with it; the DEFAULT path is
# single-experiment, mirroring the Compose default stack that compose-smoke
# validates.
#
# Re-running with the same --experiment-id is safe: secrets are read back from
# the cluster, minted identities are read back from the release values (so they
# are NOT re-minted — re-minting would orphan the prior registry rows),
# Forgejo user/repo creation is idempotent, and the repo-init Job short-circuits
# on an already-seeded repo.
#
# kubectl + helm + python3 access to the target cluster is required.

usage() {
    cat <<'EOF' >&2
Usage:
  setup-experiment-helm.sh [--experiment-config <path>]
                           [--experiment-id <exp_*>]
                           [--namespace <ns>]
                           [--release <name>]
                           [--chart <path>]
                           [--values <file>]
                           [--image <repository:tag>]
                           [--timeout <duration>]

Defaults: --namespace eden, --release eden, --chart <repo>/reference/helm/eden,
--experiment-config <repo>/tests/fixtures/experiment/.eden/config.yaml.

--experiment-id, when omitted, is freshly MINTED as an opaque exp_* id (the
post-#128 grammar; an operator-typed mnemonic like "exp-1" is rejected by the
event model at runtime).

--values is passed through to every `helm upgrade` (operator/CI-supplied image
+ secrets, e.g. reference/helm/eden/ci-values.yaml). When omitted, the script
generates fresh dev secrets (read back from the cluster on re-run) and requires
--image.
EOF
}

# --- Tooling preflight ---
for tool in helm kubectl python3; do
    command -v "$tool" >/dev/null || {
        echo "setup-experiment-helm.sh requires '$tool' on PATH" >&2
        exit 2
    }
done

# --- Resolve paths ---
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# --- Parse args ---
CONFIG_PATH=""
EXPERIMENT_ID=""
NAMESPACE="eden"
RELEASE="eden"
CHART="${REPO_ROOT}/reference/helm/eden"
VALUES_FILE=""
IMAGE_REF=""
TIMEOUT="5m"

require_value() {
    local flag="$1" remaining="$2"
    if [[ "$remaining" -lt 2 ]]; then
        echo "$flag requires a value" >&2
        usage
        exit 2
    fi
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --experiment-config) require_value "$1" "$#"; CONFIG_PATH="$2";   shift 2 ;;
        --experiment-id)     require_value "$1" "$#"; EXPERIMENT_ID="$2"; shift 2 ;;
        --namespace)         require_value "$1" "$#"; NAMESPACE="$2";     shift 2 ;;
        --release)           require_value "$1" "$#"; RELEASE="$2";       shift 2 ;;
        --chart)             require_value "$1" "$#"; CHART="$2";         shift 2 ;;
        --values)            require_value "$1" "$#"; VALUES_FILE="$2";   shift 2 ;;
        --image)             require_value "$1" "$#"; IMAGE_REF="$2";     shift 2 ;;
        --timeout)           require_value "$1" "$#"; TIMEOUT="$2";       shift 2 ;;
        -h|--help)           usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
    esac
done

if [[ -z "$CONFIG_PATH" ]]; then
    CONFIG_PATH="${REPO_ROOT}/tests/fixtures/experiment/.eden/config.yaml"
fi
if [[ ! -f "$CONFIG_PATH" ]]; then
    echo "experiment config not found: $CONFIG_PATH" >&2
    exit 2
fi
CONFIG_PATH="$(cd "$(dirname "$CONFIG_PATH")" && pwd)/$(basename "$CONFIG_PATH")"

# --- #128 opaque-id minting (Crockford base32 ULID) ---
# Mirrors eden_contracts.mint_opaque_id / setup-experiment.sh so a setup-minted
# id is indistinguishable from a server-minted one (spec §1.6 grammar:
# ^<prefix>_[0-9a-hjkmnp-tv-z]{26}$).
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

# --- Resolve the opaque experiment id (#128) ---
# Precedence: --experiment-id flag; else the EDEN_EXPERIMENT_ID baked into an
# existing release (idempotent re-run); else mint a fresh exp_*.
CHART_NAME="eden"
if [[ "$RELEASE" == *"$CHART_NAME"* ]]; then
    FULLNAME="$RELEASE"
else
    FULLNAME="${RELEASE}-${CHART_NAME}"
fi
# Mirror the chart's eden.fullname truncation EXACTLY: trunc 40 then
# `trimSuffix "-"` (removes a SINGLE trailing hyphen, not a run).
FULLNAME="$(printf '%s' "$FULLNAME" | cut -c1-40 | sed 's/-$//')"

# Read the existing release's user-supplied values once (empty JSON when the
# release does not yet exist). Used for idempotent reuse of the seed SHA + the
# minted experiment id + the minted per-service identities.
RELEASE_VALUES="{}"
if helm status "$RELEASE" -n "$NAMESPACE" >/dev/null 2>&1; then
    RELEASE_VALUES="$(helm get values "$RELEASE" -n "$NAMESPACE" -a -o json 2>/dev/null || echo '{}')"
fi

values_get() {
    # values_get <python-expression-over-`v`> — read a value out of the merged
    # release values JSON (v is the parsed dict). Prints empty on absence.
    printf '%s' "$RELEASE_VALUES" | python3 -c "import json,sys; v=json.load(sys.stdin); print($1 or '')"
}

if [[ -z "$EXPERIMENT_ID" ]]; then
    EXISTING_EXP_ID="$(values_get 'v.get("experiment",{}).get("id")')"
    if [[ -n "$EXISTING_EXP_ID" ]]; then
        EXPERIMENT_ID="$EXISTING_EXP_ID"
    else
        EXPERIMENT_ID="$(mint_opaque_id exp)"
    fi
fi

SECRET_NAME="${FULLNAME}-secrets"
HELPER_CONFIGMAP="${FULLNAME}-git-credential-helper"
FORGEJO_REMOTE_URL="http://${FULLNAME}-forgejo:3000/eden/${EXPERIMENT_ID}.git"

gen_hex() {
    python3 -c 'import secrets,sys; sys.stdout.write(secrets.token_hex(int(sys.argv[1])))' "${1:-32}"
}

# --- Build the helm value args (image + experiment config + optional secrets) ---
HELM_VALUE_ARGS=()
if [[ -n "$VALUES_FILE" ]]; then
    if [[ ! -f "$VALUES_FILE" ]]; then
        echo "--values file not found: $VALUES_FILE" >&2
        exit 2
    fi
    HELM_VALUE_ARGS+=(-f "$VALUES_FILE")
fi
if [[ -n "$IMAGE_REF" ]]; then
    # Split on the LAST colon so registries with a port keep it in the
    # repository. --set-string so numeric/bool-looking tags aren't type-coerced.
    HELM_VALUE_ARGS+=(--set-string "image.repository=${IMAGE_REF%:*}")
    HELM_VALUE_ARGS+=(--set-string "image.tag=${IMAGE_REF##*:}")
fi

# Drive the experiment identity + config. The config file is passed verbatim as
# a raw string via --set-file (the chart's ConfigMap uses experiment.configRaw
# when set), so this script needs no YAML library.
HELM_VALUE_ARGS+=(--set-string "experiment.id=${EXPERIMENT_ID}")
HELM_VALUE_ARGS+=(--set-file "experiment.configRaw=${CONFIG_PATH}")

# When no values file was supplied, generate (or read back) dev secrets.
if [[ -z "$VALUES_FILE" ]]; then
    if [[ -z "$IMAGE_REF" ]]; then
        echo "--image is required when --values is not supplied" >&2
        exit 2
    fi
    read_secret_key() {
        kubectl get secret "$SECRET_NAME" -n "$NAMESPACE" \
            -o "jsonpath={.data.$1}" 2>/dev/null | base64 -d 2>/dev/null || true
    }
    for pair in \
        "adminToken:EDEN_ADMIN_TOKEN" \
        "sessionSecret:EDEN_SESSION_SECRET" \
        "postgresPassword:POSTGRES_PASSWORD" \
        "readonlyPassword:EDEN_READONLY_PASSWORD" \
        "forgejoRemotePassword:FORGEJO_REMOTE_PASSWORD" \
        "forgejoSecretKey:FORGEJO_SECRET_KEY" \
        "forgejoInternalToken:FORGEJO_INTERNAL_TOKEN"; do
        value_key="${pair%%:*}"
        secret_key="${pair##*:}"
        existing="$(read_secret_key "$secret_key")"
        if [[ -z "$existing" ]]; then
            existing="$(gen_hex 32)"
        fi
        HELM_VALUE_ARGS+=(--set "secrets.${value_key}=${existing}")
    done
fi

# --- Phase 1: bring up (or refresh) infra. ---
# On a FRESH install baseCommitSha is empty so only the infra tier renders. On a
# RERUN we PRESERVE the existing SHA so phase 1 does not tear the live app tier
# down (the repo-init Job is idempotent and phase 2/3 re-apply it).
EXISTING_SHA="$(values_get 'v.get("experiment",{}).get("baseCommitSha")')"

# helm upgrade does NOT retain prior `--set` values, so on a re-run we must
# re-supply any already-minted identities in EVERY phase — otherwise phase 1/2
# would reset identity.* to the (empty) chart defaults and tear the live app
# tier down until phase 3 re-applies it. Empty on a fresh install.
EXISTING_IDENTITY_ARGS=()
for _key in orchestrator webUi ideatorHost executorHost evaluatorHost; do
    _wid="$(values_get "v.get('identity',{}).get('${_key}',{}).get('workerId')")"
    _tok="$(values_get "v.get('identity',{}).get('${_key}',{}).get('token')")"
    if [[ -n "$_wid" && -n "$_tok" ]]; then
        EXISTING_IDENTITY_ARGS+=(--set-string "identity.${_key}.workerId=${_wid}")
        EXISTING_IDENTITY_ARGS+=(--set-string "identity.${_key}.token=${_tok}")
    fi
done
echo "--- phase 1: helm upgrade --install ${RELEASE} (infra tier) ---" >&2
helm upgrade --install "$RELEASE" "$CHART" \
    --namespace "$NAMESPACE" --create-namespace \
    "${HELM_VALUE_ARGS[@]}" \
    ${EXISTING_IDENTITY_ARGS[@]+"${EXISTING_IDENTITY_ARGS[@]}"} \
    --set-string "experiment.baseCommitSha=${EXISTING_SHA}" \
    --wait --timeout "$TIMEOUT"

# Resolve the actual secret name + lease-mode flag from the merged release
# values: secrets.existingSecret wins over the chart-managed default.
MERGED_VALUES="$(helm get values "$RELEASE" -n "$NAMESPACE" -a -o json)"
merged_get() {
    printf '%s' "$MERGED_VALUES" | python3 -c "import json,sys; v=json.load(sys.stdin); print($1)"
}
EXISTING_SECRET="$(merged_get 'v.get("secrets",{}).get("existingSecret") or ""')"
if [[ -n "$EXISTING_SECRET" ]]; then
    SECRET_NAME="$EXISTING_SECRET"
fi
LEASE_MODE="$(merged_get 'str(bool((v.get("orchestrator") or {}).get("leaseMode",{}).get("enabled"))).lower()')"
# postgres.mode=external (13c) deploys NO in-cluster Postgres StatefulSet — the
# operator supplies a managed DSN — so there is nothing to wait on. Default
# embedded keeps the 13a behavior.
POSTGRES_MODE="$(merged_get 'v.get("postgres",{}).get("mode") or "embedded"')"

if [[ "$POSTGRES_MODE" != "external" ]]; then
    kubectl rollout status "statefulset/${FULLNAME}-postgres" -n "$NAMESPACE" --timeout="$TIMEOUT"
fi
kubectl rollout status "statefulset/${FULLNAME}-forgejo" -n "$NAMESPACE" --timeout="$TIMEOUT"
if [[ "$LEASE_MODE" == "true" ]]; then
    kubectl rollout status "deployment/${FULLNAME}-control-plane" -n "$NAMESPACE" --timeout="$TIMEOUT"
fi

# --- Provision the Forgejo eden user (idempotent) ---
FORGEJO_PASSWORD="$(kubectl get secret "$SECRET_NAME" -n "$NAMESPACE" \
    -o 'jsonpath={.data.FORGEJO_REMOTE_PASSWORD}' | base64 -d)"
FORGEJO_POD="$(kubectl get pod -n "$NAMESPACE" \
    -l app.kubernetes.io/component=forgejo -o name | head -n1)"
echo "--- provisioning forgejo eden user ---" >&2
if ! kubectl exec -n "$NAMESPACE" "$FORGEJO_POD" -- \
        forgejo admin user create --username eden \
            --password "$FORGEJO_PASSWORD" --email eden@invalid \
            --admin --must-change-password=false >/dev/null 2>&1; then
    kubectl exec -n "$NAMESPACE" "$FORGEJO_POD" -- \
        forgejo admin user change-password --username eden \
            --password "$FORGEJO_PASSWORD" --must-change-password=false \
            >/dev/null 2>&1 || true
fi

# --- Seed the bare repo via a one-shot Job (push auto-creates the repo) ---
echo "--- seeding bare repo via repo-init Job ---" >&2
IMAGE_PULL_POLICY="$(merged_get 'v.get("image",{}).get("pullPolicy","IfNotPresent")')"
EDEN_IMAGE="$(merged_get 'v["image"]["repository"] + ":" + v["image"]["tag"]')"
# Render the imagePullSecrets block (or empty) for the out-of-tree Job.
IMAGE_PULL_SECRETS="$(printf '%s' "$MERGED_VALUES" | python3 -c '
import json, sys
secrets = json.load(sys.stdin).get("image", {}).get("pullSecrets") or []
if secrets:
    lines = ["      imagePullSecrets:"]
    lines += ["        - name: " + s["name"] for s in secrets]
    sys.stdout.write("\n".join(lines))
')"
# Render nodeSelector/tolerations/affinity from the release values so the seed
# Job schedules where the chart pods do (JSON is valid YAML flow style).
POD_SCHEDULING="$(printf '%s' "$MERGED_VALUES" | python3 -c '
import json, sys
v = json.load(sys.stdin)
out = []
for key in ("nodeSelector", "tolerations", "affinity"):
    val = v.get(key)
    if val:
        out.append("      " + key + ": " + json.dumps(val))
sys.stdout.write("\n".join(out))
')"
# Derive a DNS-safe Job name from the (possibly opaque) experiment id.
SAFE_ID="$(printf '%s' "$EXPERIMENT_ID" | tr '[:upper:]' '[:lower:]' \
    | tr -c 'a-z0-9' '-' | cut -c1-20 | sed 's/^-*//; s/-*$//')"
ID_HASH="$(printf '%s' "$EXPERIMENT_ID" \
    | python3 -c 'import hashlib,sys; print(hashlib.sha1(sys.stdin.buffer.read()).hexdigest()[:8])')"
JOB_PREFIX="$(printf '%s' "${FULLNAME}-repo-init-${SAFE_ID}" | cut -c1-54 | sed 's/-*$//')"
JOB_NAME="${JOB_PREFIX}-${ID_HASH}"

kubectl delete job "$JOB_NAME" -n "$NAMESPACE" --ignore-not-found >/dev/null 2>&1 || true
EDEN_NAMESPACE="$NAMESPACE" EDEN_JOB_NAME="$JOB_NAME" EDEN_IMAGE="$EDEN_IMAGE" \
EDEN_IMAGE_PULL_POLICY="$IMAGE_PULL_POLICY" EDEN_SECRET_NAME="$SECRET_NAME" \
EDEN_HELPER_CONFIGMAP="$HELPER_CONFIGMAP" EDEN_FORGEJO_REMOTE_URL="$FORGEJO_REMOTE_URL" \
EDEN_IMAGE_PULL_SECRETS="$IMAGE_PULL_SECRETS" EDEN_POD_SCHEDULING="$POD_SCHEDULING" \
    python3 - "${CHART}/bootstrap/repo-init-job.yaml.tmpl" <<'PY' \
    | kubectl apply -n "$NAMESPACE" -f -
import os, string, sys
keys = (
    "EDEN_NAMESPACE", "EDEN_JOB_NAME", "EDEN_IMAGE", "EDEN_IMAGE_PULL_POLICY",
    "EDEN_SECRET_NAME", "EDEN_HELPER_CONFIGMAP", "EDEN_FORGEJO_REMOTE_URL",
    "EDEN_IMAGE_PULL_SECRETS", "EDEN_POD_SCHEDULING",
)
mapping = {k: os.environ[k] for k in keys}
sys.stdout.write(string.Template(open(sys.argv[1]).read()).substitute(mapping))
PY

if ! kubectl wait --for=condition=complete "job/${JOB_NAME}" \
        -n "$NAMESPACE" --timeout="$TIMEOUT"; then
    echo "repo-init Job did not complete; logs:" >&2
    kubectl logs "job/${JOB_NAME}" -n "$NAMESPACE" >&2 || true
    exit 1
fi
SEED_OUTPUT="$(kubectl logs "job/${JOB_NAME}" -n "$NAMESPACE")"
SEED_SHA="$(echo "$SEED_OUTPUT" \
    | sed -nE 's/^EDEN_REPO_(SEEDED|ALREADY_SEEDED) sha=([0-9a-f]{40})$/\2/p' | head -n1)"
if [[ -z "$SEED_SHA" ]]; then
    echo "failed to parse seed SHA from repo-init Job output:" >&2
    echo "$SEED_OUTPUT" >&2
    exit 1
fi
echo "--- seed commit: ${SEED_SHA} ---" >&2

# --- Phase 2: bring the task-store-server up (baseCommitSha set, identities not
# yet provisioned, so the identity-consuming pods stay held back). ---
echo "--- phase 2: helm upgrade ${RELEASE} (task-store-server, baseCommitSha set) ---" >&2
helm upgrade "$RELEASE" "$CHART" \
    --namespace "$NAMESPACE" \
    "${HELM_VALUE_ARGS[@]}" \
    ${EXISTING_IDENTITY_ARGS[@]+"${EXISTING_IDENTITY_ARGS[@]}"} \
    --set "experiment.baseCommitSha=${SEED_SHA}" \
    --wait --timeout "$TIMEOUT"
kubectl rollout status "deployment/${FULLNAME}-task-store-server" -n "$NAMESPACE" --timeout="$TIMEOUT"

# --- Mint reserved groups + per-service infra workers (#128) ---
ADMIN_TOKEN="$(kubectl get secret "$SECRET_NAME" -n "$NAMESPACE" \
    -o 'jsonpath={.data.EDEN_ADMIN_TOKEN}' | base64 -d)"
TASK_STORE_POD="$(kubectl get pod -n "$NAMESPACE" \
    -l app.kubernetes.io/component=task-store-server -o name | head -n1)"
EXP_BASE="/v0/experiments/${EXPERIMENT_ID}"

store_body() {
    # store_body <METHOD> <path> [json-body] — admin-authenticated task-store
    # call from inside the task-store-server pod, returning the RESPONSE BODY on
    # stdout. curl -f → non-zero on HTTP error (caller decides tolerance).
    local method="$1" path="$2" body="${3:-}"
    local args=(-fsS -X "$method"
        -H "Authorization: Bearer admin:${ADMIN_TOKEN}"
        -H "X-Eden-Experiment-Id: ${EXPERIMENT_ID}"
        -H "Content-Type: application/json")
    if [[ -n "$body" ]]; then args+=(-d "$body"); fi
    args+=("http://localhost:8080${path}")
    kubectl exec -n "$NAMESPACE" "$TASK_STORE_POD" -- curl "${args[@]}"
}

json_field() {
    # Parse a single top-level string field out of a JSON object on stdin.
    python3 -c 'import json,sys; d=json.load(sys.stdin); v=d.get(sys.argv[1]); sys.stdout.write(v if isinstance(v,str) else "")' "$1"
}

first_list_id() {
    # first_list_id <list-key> <id-field> — from {"<list-key>": [ {...}, ...]}
    # on stdin, print the <id-field> of the first element (empty if none).
    python3 -c '
import json, sys
d = json.load(sys.stdin)
items = d.get(sys.argv[1]) or []
sys.stdout.write(items[0][sys.argv[2]] if items else "")
' "$1" "$2"
}

# Mint a reserved group by NAME (admin may create reserved names), or reuse the
# existing one on a re-run. Echoes the resolved group_id.
mint_or_get_group() {
    local name="$1" body gid
    body="$(store_body GET "${EXP_BASE}/groups?name=${name}")"
    gid="$(printf '%s' "$body" | first_list_id groups group_id)"
    if [[ -z "$gid" ]]; then
        body="$(store_body POST "${EXP_BASE}/groups" "{\"name\":\"${name}\"}")"
        gid="$(printf '%s' "$body" | json_field group_id)"
    fi
    if [[ -z "$gid" ]]; then
        echo "mint_or_get_group(${name}) returned no group_id" >&2
        exit 1
    fi
    printf '%s' "$gid"
}

# Reuse-or-mint a per-service worker. Reuses the worker_id + token already baked
# into the release values on a re-run (re-minting would orphan the prior row +
# credential); otherwise mints a fresh worker and captures its registration
# token. Sets MINTED_WORKER_ID + MINTED_TOKEN globals.
reuse_or_mint_worker() {
    local name="$1" values_key="$2" existing_id existing_token body
    existing_id="$(values_get "v.get('identity',{}).get('${values_key}',{}).get('workerId')")"
    existing_token="$(values_get "v.get('identity',{}).get('${values_key}',{}).get('token')")"
    if [[ -n "$existing_id" && -n "$existing_token" ]]; then
        MINTED_WORKER_ID="$existing_id"
        MINTED_TOKEN="$existing_token"
        return 0
    fi
    body="$(store_body POST "${EXP_BASE}/workers" "{\"name\":\"${name}\"}")"
    MINTED_WORKER_ID="$(printf '%s' "$body" | json_field worker_id)"
    MINTED_TOKEN="$(printf '%s' "$body" | json_field registration_token)"
    if [[ -z "$MINTED_WORKER_ID" || -z "$MINTED_TOKEN" ]]; then
        echo "register_worker(name=${name}) missing worker_id/registration_token: $body" >&2
        exit 1
    fi
}

# Reuse-or-mint a worker by NAME with no token capture (its token is consumed by
# no pod — e.g. the operator admin actor). Reuse via list-by-name on a re-run.
# Echoes the resolved worker_id.
get_or_mint_worker() {
    local name="$1" body wid
    body="$(store_body GET "${EXP_BASE}/workers?name=${name}")"
    wid="$(printf '%s' "$body" | first_list_id workers worker_id)"
    if [[ -z "$wid" ]]; then
        body="$(store_body POST "${EXP_BASE}/workers" "{\"name\":\"${name}\"}")"
        wid="$(printf '%s' "$body" | json_field worker_id)"
    fi
    if [[ -z "$wid" ]]; then
        echo "get_or_mint_worker(${name}) returned no worker_id" >&2
        exit 1
    fi
    printf '%s' "$wid"
}

add_to_group() {
    # admin-gated; idempotent on existing membership (200).
    local gid="$1" member="$2"
    store_body POST "${EXP_BASE}/groups/${gid}/members" \
        "{\"member_id\":\"${member}\"}" >/dev/null
}

echo "--- minting reserved groups + infra workers ---" >&2
# Reserved groups. `orchestrators` is created empty; the orchestrator self-joins
# at startup under its admin token (eden_orchestrator._ensure_orchestrators_membership).
ADMINS_GID="$(mint_or_get_group admins)"
mint_or_get_group orchestrators >/dev/null

# Initial admin worker ("operator"): the principal an operator drives the web UI
# admin actions as. Its token is not consumed by any pod, so no capture.
OPERATOR_WID="$(get_or_mint_worker operator)"
add_to_group "$ADMINS_GID" "$OPERATOR_WID"

# Per-service infra workers. Each minted worker_id + token is handed to the chart
# as values.identity.<svc>.* (phase 3). The web-ui worker is also an admins
# member (it drives §3.7 admin-gated routes).
IDENTITY_SET_ARGS=()
add_identity() {
    # add_identity <name> <values-key>
    reuse_or_mint_worker "$1" "$2"
    IDENTITY_SET_ARGS+=(--set-string "identity.${2}.workerId=${MINTED_WORKER_ID}")
    IDENTITY_SET_ARGS+=(--set-string "identity.${2}.token=${MINTED_TOKEN}")
}

add_identity orchestrator    orchestrator
add_identity web-ui-1        webUi
WEB_UI_WID="$MINTED_WORKER_ID"
add_to_group "$ADMINS_GID" "$WEB_UI_WID"
add_identity ideator-host-1  ideatorHost
add_identity executor-host-1 executorHost
add_identity evaluator-host-1 evaluatorHost

echo "--- minted: admins=${ADMINS_GID} operator=${OPERATOR_WID} web-ui=${WEB_UI_WID} ---" >&2

# --- Phase 3: app tier (identities provisioned) ---
echo "--- phase 3: helm upgrade ${RELEASE} (app tier, identities provisioned) ---" >&2
helm upgrade "$RELEASE" "$CHART" \
    --namespace "$NAMESPACE" \
    "${HELM_VALUE_ARGS[@]}" \
    --set "experiment.baseCommitSha=${SEED_SHA}" \
    "${IDENTITY_SET_ARGS[@]}" \
    --wait --timeout "$TIMEOUT"
kubectl rollout status "statefulset/${FULLNAME}-orchestrator" -n "$NAMESPACE" --timeout="$TIMEOUT"
kubectl rollout status "statefulset/${FULLNAME}-web-ui" -n "$NAMESPACE" --timeout="$TIMEOUT"

# --- Lease mode (opt-in, deferred #281): register the experiment with the
# control plane so a lease-driven orchestrator can acquire it. ---
if [[ "$LEASE_MODE" == "true" ]]; then
    echo "--- lease mode: registering experiment ${EXPERIMENT_ID} with the control plane ---" >&2
    CONTROL_PLANE_POD="$(kubectl get pod -n "$NAMESPACE" \
        -l app.kubernetes.io/component=control-plane -o name | head -n1)"
    CONFIG_URI="file:///etc/eden/experiment-config.yaml"
    rc="$(kubectl exec -n "$NAMESPACE" "$CONTROL_PLANE_POD" -- curl -fsS -o /dev/null \
        -w '%{http_code}' -X POST \
        -H "Authorization: Bearer admin:${ADMIN_TOKEN}" \
        -H "Content-Type: application/json" \
        -d "{\"experiment_id\":\"${EXPERIMENT_ID}\",\"config_uri\":\"${CONFIG_URI}\"}" \
        "http://localhost:8081/v0/control/experiments" || true)"
    case "$rc" in
        200|201) ;;
        *) echo "register_experiment failed: http=$rc" >&2; exit 1 ;;
    esac
fi

cat <<EOF
setup-experiment-helm complete.

  experiment id:    ${EXPERIMENT_ID}
  namespace:        ${NAMESPACE}
  release:          ${RELEASE}
  base commit SHA:  ${SEED_SHA}
  lease mode:       ${LEASE_MODE}

  minted ids (opaque, system-minted):
    admins group:   ${ADMINS_GID}
    operator:       ${OPERATOR_WID}
    web-ui:         ${WEB_UI_WID}

The orchestrator is running in $( [[ "$LEASE_MODE" == "true" ]] && echo "lease-driven" || echo "single-experiment" ) mode.

  # Web UI (port-forward; offset to 18090 to avoid Compose's 8090):
  kubectl -n ${NAMESPACE} port-forward svc/${FULLNAME}-web-ui 18090:8090
  open http://localhost:18090/

Re-running setup-experiment-helm with the same --experiment-id is safe: minted
identities are read back from the release and reused, not re-minted.
EOF
