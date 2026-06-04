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

# Mirror the chart's eden.fullname helper: when the release name contains the
# chart name ("eden") the resources are prefixed by the release name alone,
# otherwise by "<release>-eden". Resource names below derive from this.
CHART_NAME="eden"
if [[ "$RELEASE" == *"$CHART_NAME"* ]]; then
    FULLNAME="$RELEASE"
else
    FULLNAME="${RELEASE}-${CHART_NAME}"
fi
# Mirror the chart's eden.fullname truncation (trunc 40 + trim trailing '-') so
# the resource names this script targets match what Helm rendered.
FULLNAME="$(printf '%s' "$FULLNAME" | cut -c1-40 | sed 's/-*$//')"

# Default chart-managed secret name; overridden after phase 1 if the operator's
# values set secrets.existingSecret.
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
    # Split on the LAST colon so registries with a port
    # (localhost:5000/eden-reference:ci) keep their port in the repository.
    # --set-string so numeric/bool-looking tags ("1", "true") aren't
    # type-coerced by Helm before the string-typed schema validates them.
    HELM_VALUE_ARGS+=(--set-string "image.repository=${IMAGE_REF%:*}")
    HELM_VALUE_ARGS+=(--set-string "image.tag=${IMAGE_REF##*:}")
fi

# Drive the experiment identity + config. The operator's config file is passed
# verbatim as a raw string via --set-file (the chart's ConfigMap uses
# experiment.configRaw when set), so this script needs no YAML library.
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

# --- Phase 1: infra tier (baseCommitSha empty) ---
echo "--- phase 1: helm upgrade --install ${RELEASE} (infra tier) ---" >&2
helm upgrade --install "$RELEASE" "$CHART" \
    --namespace "$NAMESPACE" --create-namespace \
    "${HELM_VALUE_ARGS[@]}" \
    --set "experiment.baseCommitSha=" \
    --wait --timeout "$TIMEOUT"

# Resolve the actual secret name from the merged release values: the operator's
# secrets.existingSecret wins over the chart-managed "<fullname>-secrets".
MERGED_VALUES="$(helm get values "$RELEASE" -n "$NAMESPACE" -a -o json)"
EXISTING_SECRET="$(echo "$MERGED_VALUES" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("secrets",{}).get("existingSecret") or "")')"
if [[ -n "$EXISTING_SECRET" ]]; then
    SECRET_NAME="$EXISTING_SECRET"
fi

kubectl rollout status "statefulset/${FULLNAME}-postgres" -n "$NAMESPACE" --timeout="$TIMEOUT"
kubectl rollout status "statefulset/${FULLNAME}-forgejo" -n "$NAMESPACE" --timeout="$TIMEOUT"
kubectl rollout status "deployment/${FULLNAME}-control-plane" -n "$NAMESPACE" --timeout="$TIMEOUT"

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
IMAGE_PULL_POLICY="$(echo "$MERGED_VALUES" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("image",{}).get("pullPolicy","IfNotPresent"))')"
EDEN_IMAGE="$(echo "$MERGED_VALUES" \
    | python3 -c 'import json,sys; v=json.load(sys.stdin)["image"]; print(v["repository"] + ":" + v["tag"])')"
# Render the imagePullSecrets block (or empty) so private-registry pulls work
# for the out-of-tree bootstrap Job, mirroring the chart workloads.
IMAGE_PULL_SECRETS="$(echo "$MERGED_VALUES" | python3 -c '
import json, sys
secrets = json.load(sys.stdin).get("image", {}).get("pullSecrets") or []
if secrets:
    lines = ["      imagePullSecrets:"]
    lines += ["        - name: " + s["name"] for s in secrets]
    sys.stdout.write("\n".join(lines))
')"
# Derive a DNS-safe Job name: experiment ids may contain characters valid for
# EDEN (underscores, uppercase) but invalid in Kubernetes object names. Lower-
# case + replace invalid chars, truncate, and append a short hash of the
# original id for uniqueness. The original EXPERIMENT_ID still drives the
# EDEN/Forgejo inputs.
SAFE_ID="$(printf '%s' "$EXPERIMENT_ID" | tr '[:upper:]' '[:lower:]' \
    | tr -c 'a-z0-9' '-' | cut -c1-20 | sed 's/^-*//; s/-*$//')"
ID_HASH="$(printf '%s' "$EXPERIMENT_ID" \
    | python3 -c 'import hashlib,sys; print(hashlib.sha1(sys.stdin.buffer.read()).hexdigest()[:8])')"
JOB_NAME="${FULLNAME}-repo-init-${SAFE_ID}-${ID_HASH}"

# Render the out-of-tree Job template (string.Template ${VAR} substitution via
# python — avoids a gettext/envsubst dependency) and apply it.
kubectl delete job "$JOB_NAME" -n "$NAMESPACE" --ignore-not-found >/dev/null 2>&1 || true
EDEN_NAMESPACE="$NAMESPACE" EDEN_JOB_NAME="$JOB_NAME" EDEN_IMAGE="$EDEN_IMAGE" \
EDEN_IMAGE_PULL_POLICY="$IMAGE_PULL_POLICY" EDEN_SECRET_NAME="$SECRET_NAME" \
EDEN_HELPER_CONFIGMAP="$HELPER_CONFIGMAP" EDEN_FORGEJO_REMOTE_URL="$FORGEJO_REMOTE_URL" \
EDEN_IMAGE_PULL_SECRETS="$IMAGE_PULL_SECRETS" \
    python3 - "${CHART}/bootstrap/repo-init-job.yaml.tmpl" <<'PY' \
    | kubectl apply -n "$NAMESPACE" -f -
import os, string, sys
keys = (
    "EDEN_NAMESPACE", "EDEN_JOB_NAME", "EDEN_IMAGE", "EDEN_IMAGE_PULL_POLICY",
    "EDEN_SECRET_NAME", "EDEN_HELPER_CONFIGMAP", "EDEN_FORGEJO_REMOTE_URL",
    "EDEN_IMAGE_PULL_SECRETS",
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

kubectl rollout status "deployment/${FULLNAME}-task-store-server" -n "$NAMESPACE" --timeout="$TIMEOUT"
kubectl rollout status "statefulset/${FULLNAME}-orchestrator" -n "$NAMESPACE" --timeout="$TIMEOUT"

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

# Bootstrap the task-store groups + members BEFORE registering the experiment
# with the control plane: registration publishes the lease target, and any
# already-running orchestrator can acquire the lease immediately. If the
# task-store `orchestrators` group is not seeded first, the orchestrator's
# baseline-creation runs under a worker not yet in the group, 403s, and the
# per-experiment runtime is cached (no retry until a pod restart) — so the
# default baseline would be missing. Seed groups first, register last.

# Reserved groups + initial admin / web-ui workers (mirrors Compose §5.7).
ADMINS_MEMBER="$(echo "$MERGED_VALUES" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("config",{}).get("adminsInitialMember","operator"))')"
WEB_UI_WID="$(echo "$MERGED_VALUES" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("config",{}).get("webUiWorkerId","web-ui-1"))')"
ORCH_REPLICAS="$(echo "$MERGED_VALUES" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("replicas",{}).get("orchestrator",2))')"
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

# Seed the task-store `orchestrators` group with the orchestrator StatefulSet
# pod worker_ids (<fullname>-orchestrator-<ordinal>, used as --worker-id via
# POD_NAME). In control-plane lease-driven mode the orchestrator self-joins
# only the CONTROL-PLANE orchestrators group, not the task-store one, so
# without this its §3.7-gated task/variant ops would 403 and the experiment
# would make no progress (a proper in-orchestrator fix is tracked under #254).
# Mirrors reference/compose/healthcheck/smoke-multi-experiment.sh.
echo "--- seeding task-store orchestrators group with orchestrator pods ---" >&2
i=0
while [[ "$i" -lt "$ORCH_REPLICAS" ]]; do
    owid="${FULLNAME}-orchestrator-${i}"
    rc="$(store_curl POST "${EXP_BASE}/workers" "{\"worker_id\":\"${owid}\"}")"
    case "$rc" in 200) ;; *) echo "register_worker($owid) failed: http=$rc" >&2; exit 1 ;; esac
    rc="$(store_curl POST "${EXP_BASE}/groups/orchestrators/members" "{\"member_id\":\"${owid}\"}")"
    case "$rc" in 200) ;; *) echo "add_to_group(orchestrators, $owid) failed: http=$rc" >&2; exit 1 ;; esac
    i=$((i + 1))
done

# Now that the groups are seeded, register the experiment with the control
# plane — this publishes the lease target the orchestrators acquire.
echo "--- registering experiment ${EXPERIMENT_ID} with the control plane ---" >&2
# config_uri is the experiment-config resource (chapter 11), NOT the git
# remote. The chart mounts the config at /etc/eden/experiment-config.yaml in
# every service, mirroring the Compose bootstrap's file:// URI.
CONFIG_URI="file:///etc/eden/experiment-config.yaml"
rc="$(cp_curl POST /v0/control/experiments \
    "{\"experiment_id\":\"${EXPERIMENT_ID}\",\"config_uri\":\"${CONFIG_URI}\"}")"
case "$rc" in
    200|201) ;;
    *) echo "register_experiment failed: http=$rc" >&2; exit 1 ;;
esac

cat <<EOF
setup-experiment-helm complete.

  experiment id:    ${EXPERIMENT_ID}
  namespace:        ${NAMESPACE}
  release:          ${RELEASE}
  base commit SHA:  ${SEED_SHA}

The orchestrator StatefulSet is running in lease-driven mode and will acquire
the experiment's lease within one poll interval.

  # Web UI (port-forward; offset to 18090 to avoid Compose's 8090):
  kubectl -n ${NAMESPACE} port-forward svc/${FULLNAME}-web-ui 18090:8090
  open http://localhost:18090/

Re-running setup-experiment-helm with the same --experiment-id is safe.
EOF
