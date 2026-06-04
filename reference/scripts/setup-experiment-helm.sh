#!/usr/bin/env bash
set -euo pipefail

# setup-experiment-helm — bootstrap an EDEN experiment on a Helm-deployed
# stack. The Helm equivalent of setup-experiment.sh (Compose).
#
# The reference task-store-server is single-experiment per process and records
# its seed commit at first experiment-row creation, so the APP TIER must not
# start until the seed SHA is known. This script therefore runs in two phases:
#
#   1. `helm upgrade --install` with experiment.baseCommitSha empty → only the
#      INFRA tier (Postgres, Forgejo, control-plane) comes up.
#   2. Provision the Forgejo eden user, seed the bare repo via a one-shot Job
#      (push auto-creates the repo), read the seed SHA, then
#      `helm upgrade --set experiment.baseCommitSha=<sha>` → the APP TIER comes
#      up (task-store-server, orchestrator, worker hosts, web-ui).
#   3. Register the experiment with the control plane (lease-driven mode) and
#      bootstrap the reserved groups + initial admin / web-ui workers.
#
# Re-running with the same --experiment-id is safe: secrets are read back from
# the cluster, Forgejo user/repo creation is idempotent, the repo-init Job
# short-circuits on an already-seeded repo, and register_experiment is
# idempotent on (experiment_id, config_uri).
#
# kubectl + helm access to the target cluster is required.

usage() {
    cat <<'EOF' >&2
Usage:
  setup-experiment-helm.sh [--experiment-config <path>]
                           [--experiment-id <id>]
                           [--namespace <ns>]
                           [--release <name>]
                           [--chart <path>]
                           [--values <file>]
                           [--image <repository:tag>]
                           [--timeout <duration>]

Defaults: --namespace eden, --release eden, --chart <repo>/reference/helm/eden,
--experiment-config <repo>/tests/fixtures/experiment/.eden/config.yaml.

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

# --- Default experiment-id from the config's parent dir (mirrors Compose) ---
if [[ -z "$EXPERIMENT_ID" ]]; then
    parent="$(dirname "$CONFIG_PATH")"
    grandparent="$(dirname "$parent")"
    if [[ "$(basename "$parent")" == ".eden" ]]; then
        EXPERIMENT_ID="$(basename "$grandparent")"
    else
        EXPERIMENT_ID="$(basename "$parent")"
    fi
fi

SECRET_NAME="${RELEASE}-secrets"
HELPER_CONFIGMAP="${RELEASE}-git-credential-helper"
FORGEJO_REMOTE_URL="http://${RELEASE}-forgejo:3000/eden/${EXPERIMENT_ID}.git"

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
    HELM_VALUE_ARGS+=(--set "image.repository=${IMAGE_REF%%:*}")
    HELM_VALUE_ARGS+=(--set "image.tag=${IMAGE_REF##*:}")
fi

# Embed the experiment config object under experiment.config via a generated
# overlay (highest precedence) so the operator's YAML drives the ConfigMap.
CONFIG_OVERLAY="$(mktemp)"
trap 'rm -f "$CONFIG_OVERLAY"' EXIT
python3 - "$CONFIG_PATH" "$EXPERIMENT_ID" > "$CONFIG_OVERLAY" <<'PY'
import sys, yaml
config = yaml.safe_load(open(sys.argv[1])) or {}
overlay = {"experiment": {"id": sys.argv[2], "config": config}}
yaml.safe_dump(overlay, sys.stdout, default_flow_style=False, sort_keys=False)
PY
HELM_VALUE_ARGS+=(-f "$CONFIG_OVERLAY")

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

# --- Phase 1: infra tier (baseCommitSha empty) ---
echo "--- phase 1: helm upgrade --install ${RELEASE} (infra tier) ---" >&2
helm upgrade --install "$RELEASE" "$CHART" \
    --namespace "$NAMESPACE" --create-namespace \
    "${HELM_VALUE_ARGS[@]}" \
    --set "experiment.baseCommitSha=" \
    --wait --timeout "$TIMEOUT"

kubectl rollout status "statefulset/${RELEASE}-postgres" -n "$NAMESPACE" --timeout="$TIMEOUT"
kubectl rollout status "statefulset/${RELEASE}-forgejo" -n "$NAMESPACE" --timeout="$TIMEOUT"
kubectl rollout status "deployment/${RELEASE}-control-plane" -n "$NAMESPACE" --timeout="$TIMEOUT"

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
IMAGE_PULL_POLICY="$(helm get values "$RELEASE" -n "$NAMESPACE" -a -o json \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("image",{}).get("pullPolicy","IfNotPresent"))')"
EDEN_IMAGE="$(helm get values "$RELEASE" -n "$NAMESPACE" -a -o json \
    | python3 -c 'import json,sys; v=json.load(sys.stdin)["image"]; print(f"{v[\"repository\"]}:{v[\"tag\"]}")')"
JOB_NAME="${RELEASE}-repo-init-${EXPERIMENT_ID}"

# Render the out-of-tree Job template (string.Template ${VAR} substitution via
# python — avoids a gettext/envsubst dependency) and apply it.
kubectl delete job "$JOB_NAME" -n "$NAMESPACE" --ignore-not-found >/dev/null 2>&1 || true
EDEN_NAMESPACE="$NAMESPACE" EDEN_JOB_NAME="$JOB_NAME" EDEN_IMAGE="$EDEN_IMAGE" \
EDEN_IMAGE_PULL_POLICY="$IMAGE_PULL_POLICY" EDEN_SECRET_NAME="$SECRET_NAME" \
EDEN_HELPER_CONFIGMAP="$HELPER_CONFIGMAP" EDEN_FORGEJO_REMOTE_URL="$FORGEJO_REMOTE_URL" \
    python3 - "${CHART}/bootstrap/repo-init-job.yaml.tmpl" <<'PY' \
    | kubectl apply -n "$NAMESPACE" -f -
import os, string, sys
keys = (
    "EDEN_NAMESPACE", "EDEN_JOB_NAME", "EDEN_IMAGE", "EDEN_IMAGE_PULL_POLICY",
    "EDEN_SECRET_NAME", "EDEN_HELPER_CONFIGMAP", "EDEN_FORGEJO_REMOTE_URL",
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

# --- Phase 2: app tier (baseCommitSha set) ---
echo "--- phase 2: helm upgrade ${RELEASE} (app tier, baseCommitSha set) ---" >&2
helm upgrade "$RELEASE" "$CHART" \
    --namespace "$NAMESPACE" \
    "${HELM_VALUE_ARGS[@]}" \
    --set "experiment.baseCommitSha=${SEED_SHA}" \
    --wait --timeout "$TIMEOUT"

kubectl rollout status "deployment/${RELEASE}-task-store-server" -n "$NAMESPACE" --timeout="$TIMEOUT"
kubectl rollout status "statefulset/${RELEASE}-orchestrator" -n "$NAMESPACE" --timeout="$TIMEOUT"

# --- Register the experiment + bootstrap groups/workers ---
ADMIN_TOKEN="$(kubectl get secret "$SECRET_NAME" -n "$NAMESPACE" \
    -o 'jsonpath={.data.EDEN_ADMIN_TOKEN}' | base64 -d)"
CONTROL_PLANE_POD="$(kubectl get pod -n "$NAMESPACE" \
    -l app.kubernetes.io/component=control-plane -o name | head -n1)"
TASK_STORE_POD="$(kubectl get pod -n "$NAMESPACE" \
    -l app.kubernetes.io/component=task-store-server -o name | head -n1)"

cp_curl() {
    # admin-authenticated control-plane call from inside the control-plane pod.
    local method="$1" path="$2" body="${3:-}"
    local args=(-fsS -o /dev/null -w '%{http_code}' -X "$method"
        -H "Authorization: Bearer admin:${ADMIN_TOKEN}"
        -H "Content-Type: application/json")
    if [[ -n "$body" ]]; then args+=(-d "$body"); fi
    args+=("http://localhost:8081${path}")
    kubectl exec -n "$NAMESPACE" "$CONTROL_PLANE_POD" -- curl "${args[@]}" || true
}

store_curl() {
    local method="$1" path="$2" body="${3:-}"
    local args=(-fsS -o /dev/null -w '%{http_code}' -X "$method"
        -H "Authorization: Bearer admin:${ADMIN_TOKEN}"
        -H "X-Eden-Experiment-Id: ${EXPERIMENT_ID}"
        -H "Content-Type: application/json")
    if [[ -n "$body" ]]; then args+=(-d "$body"); fi
    args+=("http://localhost:8080${path}")
    kubectl exec -n "$NAMESPACE" "$TASK_STORE_POD" -- curl "${args[@]}" || true
}

echo "--- registering experiment ${EXPERIMENT_ID} with the control plane ---" >&2
rc="$(cp_curl POST /v0/control/experiments \
    "{\"experiment_id\":\"${EXPERIMENT_ID}\",\"config_uri\":\"${FORGEJO_REMOTE_URL}\"}")"
case "$rc" in
    200|201) ;;
    *) echo "register_experiment failed: http=$rc" >&2; exit 1 ;;
esac

# Reserved groups + initial admin / web-ui workers (mirrors Compose §5.7).
ADMINS_MEMBER="$(helm get values "$RELEASE" -n "$NAMESPACE" -a -o json \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("config",{}).get("adminsInitialMember","operator"))')"
WEB_UI_WID="$(helm get values "$RELEASE" -n "$NAMESPACE" -a -o json \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("config",{}).get("webUiWorkerId","web-ui-1"))')"
EXP_BASE="/v0/experiments/${EXPERIMENT_ID}"
echo "--- bootstrapping reserved groups + admin/web-ui workers ---" >&2
for group in orchestrators admins; do
    rc="$(store_curl POST "${EXP_BASE}/groups" "{\"group_id\":\"${group}\"}")"
    case "$rc" in 200|409) ;; *) echo "register_group($group) failed: http=$rc" >&2; exit 1 ;; esac
done
for wid in "$ADMINS_MEMBER" "$WEB_UI_WID"; do
    rc="$(store_curl POST "${EXP_BASE}/workers" "{\"worker_id\":\"${wid}\"}")"
    case "$rc" in 200) ;; *) echo "register_worker($wid) failed: http=$rc" >&2; exit 1 ;; esac
    rc="$(store_curl POST "${EXP_BASE}/groups/admins/members" "{\"member_id\":\"${wid}\"}")"
    case "$rc" in 200) ;; *) echo "add_to_group(admins, $wid) failed: http=$rc" >&2; exit 1 ;; esac
done

cat <<EOF
setup-experiment-helm complete.

  experiment id:    ${EXPERIMENT_ID}
  namespace:        ${NAMESPACE}
  release:          ${RELEASE}
  base commit SHA:  ${SEED_SHA}

The orchestrator StatefulSet is running in lease-driven mode and will acquire
the experiment's lease within one poll interval.

  # Web UI (port-forward; offset to 18090 to avoid Compose's 8090):
  kubectl -n ${NAMESPACE} port-forward svc/${RELEASE}-web-ui 18090:8090
  open http://localhost:18090/

Re-running setup-experiment-helm with the same --experiment-id is safe.
EOF
