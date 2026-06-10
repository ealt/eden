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
# docker + jq + python3 installed. Shared harness in ci-smoke-lib.sh.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CHART_DIR="$SCRIPT_DIR"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=ci-smoke-lib.sh
. "${SCRIPT_DIR}/ci-smoke-lib.sh"

eden_require_tools kind kubectl helm docker jq python3

CLUSTER="${EDEN_KIND_CLUSTER:-eden-helm-smoke}"
NAMESPACE="eden-ci"
RELEASE="eden"
IMAGE="eden-reference:ci"
EXPERIMENT_ID="$(eden_mint_experiment_id)"
# Quiescence budget: the orchestrator runs forever (maxQuiescentIterations=0),
# so we poll for the integration end-state rather than for a clean exit.
DEADLINE_SECONDS="${EDEN_HELM_SMOKE_DEADLINE:-240}"

eden_create_kind_cluster
eden_build_and_load_image

echo "--- running setup-experiment-helm.sh ---" >&2
bash "${REPO_ROOT}/reference/scripts/setup-experiment-helm.sh" \
    --namespace "$NAMESPACE" \
    --release "$RELEASE" \
    --chart "$CHART_DIR" \
    --values "${CHART_DIR}/ci-values.yaml" \
    --experiment-config "${REPO_ROOT}/tests/fixtures/experiment/.eden/config.yaml" \
    --experiment-id "$EXPERIMENT_ID" \
    --timeout 5m

eden_resolve_task_store
eden_wait_for_integrated 3 "$DEADLINE_SECONDS" || true
eden_assert_end_state

echo "--- running helm test (connection probe) ---" >&2
helm test "$RELEASE" -n "$NAMESPACE" --timeout 2m

echo "--- helm-smoke PASSED ---" >&2
