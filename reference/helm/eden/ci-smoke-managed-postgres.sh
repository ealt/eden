#!/usr/bin/env bash
set -euo pipefail

# helm-smoke-managed-postgres — the external-Postgres analogue of ci-smoke.sh (13c).
#
# Proves postgres.mode=external drives an experiment to the same end-state as the
# embedded path, against a Postgres the chart did NOT create. A sibling
# postgres:16.6-alpine container runs on the kind Docker network; the chart's
# task-store-server reaches OUT to it by IP via an operator-supplied DSN Secret.
#
# Why IP, not container name: kind's in-cluster CoreDNS resolves only Service DNS
# — a sibling container on the kind Docker network is reachable by IP but not by
# its docker name from inside pods. So the DSN embeds the container's IP.
#
# Mirrors the CI helm-smoke-managed-postgres job; runnable locally with kind +
# helm + kubectl + docker + jq + python3 installed.

for tool in kind kubectl helm docker jq python3; do
    command -v "$tool" >/dev/null || {
        echo "ci-smoke-managed-postgres.sh requires '$tool' on PATH" >&2
        exit 2
    }
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CHART_DIR="$SCRIPT_DIR"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

CLUSTER="${EDEN_KIND_CLUSTER:-eden-helm-smoke-mp}"
NAMESPACE="eden-ci"
RELEASE="eden"
IMAGE="eden-reference:ci"
PG_CONTAINER="eden-ci-managed-pg"
PG_USER="eden"
PG_PASSWORD="ci-managed-pg-password"
PG_DB="eden"
DEADLINE_SECONDS="${EDEN_HELM_SMOKE_DEADLINE:-240}"

# Mint a valid opaque exp_* id (#128 grammar ^exp_[0-9a-hjkmnp-tv-z]{26}$).
EXPERIMENT_ID="$(python3 -c '
import secrets, time
alphabet = "0123456789abcdefghjkmnpqrstvwxyz"
value = ((int(time.time() * 1000) & ((1 << 48) - 1)) << 80) | secrets.randbits(80)
print("exp_" + "".join(alphabet[(value >> (5 * i)) & 31] for i in range(26))[::-1])
')"

# Only tear down what THIS script created.
CREATED_CLUSTER=0
cleanup() {
    local rc=$?
    docker rm -f "$PG_CONTAINER" >/dev/null 2>&1 || true
    if [[ "$CREATED_CLUSTER" -eq 1 ]]; then
        echo "--- tearing down kind cluster ${CLUSTER} ---" >&2
        kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true
    fi
    exit "$rc"
}
trap cleanup EXIT

if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
    echo "kind cluster '${CLUSTER}' already exists; refusing to reuse/delete it." >&2
    echo "Delete it yourself or set EDEN_KIND_CLUSTER to a fresh name." >&2
    exit 2
fi
echo "--- creating kind cluster ${CLUSTER} ---" >&2
kind create cluster --name "$CLUSTER" --wait 120s
CREATED_CLUSTER=1

# --- Start the sibling (chart-external) Postgres on the kind Docker network ---
# kind connects every node container to the shared 'kind' Docker network; a
# sibling on the same network is routable from pods by IP.
echo "--- starting sibling Postgres ${PG_CONTAINER} on the kind network ---" >&2
docker rm -f "$PG_CONTAINER" >/dev/null 2>&1 || true
docker run -d --name "$PG_CONTAINER" --network kind \
    -e POSTGRES_USER="$PG_USER" \
    -e POSTGRES_PASSWORD="$PG_PASSWORD" \
    -e POSTGRES_DB="$PG_DB" \
    postgres:16.6-alpine >/dev/null
for _ in $(seq 1 30); do
    if docker exec "$PG_CONTAINER" pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
# The container is attached only to the 'kind' network, so its single network
# IP is the one pods route to.
PG_IP="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$PG_CONTAINER")"
if [[ -z "$PG_IP" ]]; then
    echo "could not resolve sibling Postgres IP on the kind network" >&2
    docker inspect "$PG_CONTAINER" >&2 || true
    exit 1
fi
echo "--- sibling Postgres reachable at ${PG_IP}:5432 ---" >&2

echo "--- building + loading ${IMAGE} ---" >&2
docker build -t "$IMAGE" -f "${REPO_ROOT}/reference/compose/Dockerfile" "$REPO_ROOT"
kind load docker-image "$IMAGE" --name "$CLUSTER"

# --- Create the operator-supplied DSN Secret the chart references via
#     postgres.external.existingSecret (ci-values-managed-postgres.yaml). ---
kubectl create namespace "$NAMESPACE" >/dev/null 2>&1 || true
kubectl create secret generic eden-managed-pg -n "$NAMESPACE" \
    --from-literal=EDEN_STORE_URL="postgresql://${PG_USER}:${PG_PASSWORD}@${PG_IP}:5432/${PG_DB}" \
    --dry-run=client -o yaml | kubectl apply -f -

echo "--- running setup-experiment-helm.sh (mode=external) ---" >&2
bash "${REPO_ROOT}/reference/scripts/setup-experiment-helm.sh" \
    --namespace "$NAMESPACE" \
    --release "$RELEASE" \
    --chart "$CHART_DIR" \
    --values "${CHART_DIR}/ci-values-managed-postgres.yaml" \
    --experiment-config "${REPO_ROOT}/tests/fixtures/experiment/.eden/config.yaml" \
    --experiment-id "$EXPERIMENT_ID" \
    --timeout 5m

TASK_STORE_POD="$(kubectl get pod -n "$NAMESPACE" \
    -l app.kubernetes.io/component=task-store-server -o name | head -n1)"
ADMIN_TOKEN="$(kubectl get secret "${RELEASE}-secrets" -n "$NAMESPACE" \
    -o 'jsonpath={.data.EDEN_ADMIN_TOKEN}' | base64 -d)"

fetch_events() {
    kubectl exec -n "$NAMESPACE" "$TASK_STORE_POD" -- curl -fsS \
        -H "Authorization: Bearer admin:${ADMIN_TOKEN}" \
        -H "X-Eden-Experiment-Id: ${EXPERIMENT_ID}" \
        "http://localhost:8080/v0/experiments/${EXPERIMENT_ID}/events"
}

count_type() {
    if [[ -n "${3:-}" ]]; then
        echo "$1" | jq --arg p "$3" '(.events // .) | [.[] | select(
            .type == "task.completed" and (.data.task_id | startswith($p)))] | length'
    else
        echo "$1" | jq --arg t "$2" '(.events // .) | [.[] | select(.type == $t)] | length'
    fi
}

echo "--- waiting for >= 3 variant.integrated (budget ${DEADLINE_SECONDS}s) ---" >&2
elapsed=0
integrated=0
while [[ "$elapsed" -lt "$DEADLINE_SECONDS" ]]; do
    events="$(fetch_events 2>/dev/null || echo '{}')"
    integrated="$(count_type "$events" "variant.integrated")"
    if [[ "${integrated:-0}" -ge 3 ]]; then
        break
    fi
    sleep 5
    elapsed=$((elapsed + 5))
done

events="$(fetch_events)"
integrated="$(count_type "$events" "variant.integrated")"
completed="$(count_type "$events" "task.completed")"
ideation_completed="$(count_type "$events" "task.completed" "ideation-")"

echo "--- final counts: variant.integrated=${integrated} task.completed=${completed} ideation=${ideation_completed} ---" >&2
test "${integrated:-0}" -ge 3 || { echo "expected >= 3 variant.integrated; got ${integrated}" >&2; exit 1; }
test "${completed:-0}" -ge 9 || { echo "expected >= 9 task.completed; got ${completed}" >&2; exit 1; }
test "${ideation_completed:-0}" -ge 3 || { echo "expected >= 3 ideation task.completed; got ${ideation_completed}" >&2; exit 1; }

# Prove no in-cluster Postgres was deployed (the whole point of mode=external).
if kubectl get statefulset "${RELEASE}-postgres" -n "$NAMESPACE" >/dev/null 2>&1; then
    echo "mode=external should deploy NO postgres StatefulSet, but one exists" >&2
    exit 1
fi
echo "--- confirmed: no chart-managed postgres StatefulSet ---" >&2

echo "--- helm-smoke-managed-postgres PASSED ---" >&2
