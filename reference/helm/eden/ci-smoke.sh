#!/usr/bin/env bash
set -euo pipefail

# helm-smoke — the Helm analogue of reference/compose/healthcheck/smoke.sh.
#
# Spins up a kind cluster, builds + loads the reference image, runs
# setup-experiment-helm.sh against the fixture experiment, waits for the
# orchestrator to drive 3 ideation tasks → 3 variants → 3 integrations, and
# asserts the same end-state as compose-smoke: >= 3 variant.integrated, >= 9
# task.completed, >= 3 ideation-task task.completed events. Tears the cluster
# down on every exit path.
#
# Mirrors the CI helm-smoke job; runnable locally with kind + helm + kubectl +
# docker + jq + python3 installed.

for tool in kind kubectl helm docker jq python3; do
    command -v "$tool" >/dev/null || {
        echo "ci-smoke.sh requires '$tool' on PATH" >&2
        exit 2
    }
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CHART_DIR="$SCRIPT_DIR"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

CLUSTER="${EDEN_KIND_CLUSTER:-eden-helm-smoke}"
NAMESPACE="eden-ci"
RELEASE="eden"
IMAGE="eden-reference:ci"
EXPERIMENT_ID="exp-1"
# Quiescence budget: the orchestrator runs forever (maxQuiescentIterations=0),
# so we poll for the integration end-state rather than for a clean exit.
DEADLINE_SECONDS="${EDEN_HELM_SMOKE_DEADLINE:-240}"

cleanup() {
    local rc=$?
    echo "--- tearing down kind cluster ${CLUSTER} ---" >&2
    kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true
    exit "$rc"
}
trap cleanup EXIT

echo "--- creating kind cluster ${CLUSTER} ---" >&2
kind create cluster --name "$CLUSTER" --wait 120s

echo "--- building + loading ${IMAGE} ---" >&2
docker build -t "$IMAGE" -f "${REPO_ROOT}/reference/compose/Dockerfile" "$REPO_ROOT"
kind load docker-image "$IMAGE" --name "$CLUSTER"

echo "--- running setup-experiment-helm.sh ---" >&2
bash "${REPO_ROOT}/reference/scripts/setup-experiment-helm.sh" \
    --namespace "$NAMESPACE" \
    --release "$RELEASE" \
    --chart "$CHART_DIR" \
    --values "${CHART_DIR}/ci-values.yaml" \
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
    # count_type <events-json> <type> [task-id-prefix]
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

echo "--- running helm test (connection probe) ---" >&2
helm test "$RELEASE" -n "$NAMESPACE" --timeout 2m

echo "--- helm-smoke PASSED ---" >&2
