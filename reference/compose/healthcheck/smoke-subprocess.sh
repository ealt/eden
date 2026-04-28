#!/usr/bin/env bash
set -euo pipefail

# Phase 10d subprocess-mode smoke test for the EDEN reference Compose
# stack. Mirrors smoke.sh but layers on `compose.subprocess.yaml` so
# the planner / implementer / evaluator hosts run the fixture's
# user-supplied `*_command` scripts (`plan.py` / `implement.py` /
# `eval.py`) instead of their scripted profiles.

for tool in docker jq curl python3; do
    command -v "$tool" >/dev/null || {
        echo "smoke-subprocess.sh requires '$tool' on PATH" >&2
        exit 2
    }
done
docker compose version >/dev/null || {
    echo "smoke-subprocess.sh requires the 'docker compose' v2 plugin" >&2
    exit 2
}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${COMPOSE_DIR}/../.." && pwd)"
EXPERIMENT_DIR="${REPO_ROOT}/tests/fixtures/experiment"

cd "$COMPOSE_DIR"

ENV_FILE="$(mktemp)"
EXPERIMENT_ID="smoke-exp-sub"

cleanup() {
    local rc=$?
    docker compose -f compose.yaml -f compose.subprocess.yaml \
        --env-file "$ENV_FILE" down -v >/dev/null 2>&1 || true
    rm -f "$ENV_FILE"
    rm -f "${COMPOSE_DIR}/experiment-config.yaml"
    exit "$rc"
}
trap cleanup EXIT

echo "--- running setup-experiment (subprocess overlay) ---"
bash "${REPO_ROOT}/reference/scripts/setup-experiment/setup-experiment.sh" \
    "${EXPERIMENT_DIR}/.eden/config.yaml" \
    --experiment-id "$EXPERIMENT_ID" \
    --experiment-dir "$EXPERIMENT_DIR" \
    --env-file "$ENV_FILE"

EDEN_BASE_COMMIT_SHA="$(grep -E '^EDEN_BASE_COMMIT_SHA=' "$ENV_FILE" | cut -d= -f2-)"
test -n "$EDEN_BASE_COMMIT_SHA"

EDEN_EXPERIMENT_DIR_HOST="$(grep -E '^EDEN_EXPERIMENT_DIR_HOST=' "$ENV_FILE" | cut -d= -f2-)"
test -n "$EDEN_EXPERIMENT_DIR_HOST" || {
    echo "setup-experiment did not write EDEN_EXPERIMENT_DIR_HOST" >&2
    exit 1
}
test -d "$EDEN_EXPERIMENT_DIR_HOST"

echo "--- bringing up the full stack with subprocess overlay ---"
docker compose -f compose.yaml -f compose.subprocess.yaml \
    --env-file "$ENV_FILE" up -d --wait --wait-timeout 240

echo "--- waiting for orchestrator to exit on quiescence ---"
deadline=$((SECONDS + 240))
while [[ $SECONDS -lt $deadline ]]; do
    status="$(docker inspect --format '{{.State.Status}}' eden-orchestrator)"
    if [[ "$status" = "exited" ]]; then
        break
    fi
    sleep 2
done
test "$(docker inspect --format '{{.State.Status}}' eden-orchestrator)" = "exited" || {
    echo "orchestrator did not exit within 240s; current status: $status" >&2
    docker compose -f compose.yaml -f compose.subprocess.yaml \
        --env-file "$ENV_FILE" logs --tail 50 orchestrator planner-host \
        implementer-host evaluator-host >&2
    exit 1
}
test "$(docker inspect --format '{{.State.ExitCode}}' eden-orchestrator)" = "0" || {
    echo "orchestrator exited non-zero" >&2
    docker compose -f compose.yaml -f compose.subprocess.yaml \
        --env-file "$ENV_FILE" logs --tail 50 orchestrator >&2
    exit 1
}

echo "--- asserting final task-store state (subprocess mode) ---"
EDEN_SHARED_TOKEN="$(grep -E '^EDEN_SHARED_TOKEN=' "$ENV_FILE" | cut -d= -f2-)"
EVENTS_JSON="$(
    docker compose -f compose.yaml -f compose.subprocess.yaml \
        --env-file "$ENV_FILE" \
        exec -T task-store-server \
        curl -fsS \
            -H "Authorization: Bearer ${EDEN_SHARED_TOKEN}" \
            -H "X-Eden-Experiment-Id: ${EXPERIMENT_ID}" \
            "http://localhost:8080/v0/experiments/${EXPERIMENT_ID}/events"
)"
TRIAL_INTEGRATED="$(
    echo "$EVENTS_JSON" \
        | jq '(.events // .) | [.[] | select(.type == "trial.integrated")] | length'
)"
test "$TRIAL_INTEGRATED" -ge 3 || {
    echo "expected >= 3 trial.integrated events; got $TRIAL_INTEGRATED" >&2
    exit 1
}
TASK_COMPLETED="$(
    echo "$EVENTS_JSON" \
        | jq '(.events // .) | [.[] | select(.type == "task.completed")] | length'
)"
test "$TASK_COMPLETED" -ge 9 || {
    echo "expected >= 9 task.completed events; got $TASK_COMPLETED" >&2
    exit 1
}
PLAN_COMPLETED="$(
    echo "$EVENTS_JSON" \
        | jq '(.events // .) | [.[] | select(
              .type == "task.completed"
              and (.data.task_id | startswith("plan-"))
            )] | length'
)"
test "$PLAN_COMPLETED" -ge 3 || {
    echo "expected >= 3 plan-task.completed events; got $PLAN_COMPLETED" >&2
    exit 1
}

echo "PASS (subprocess mode)"
