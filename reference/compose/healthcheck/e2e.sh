#!/usr/bin/env bash
set -euo pipefail

# Phase 10e end-to-end smoke for the EDEN reference Compose stack.
# Beyond compose-smoke (headless 3-trial loop) and
# compose-smoke-subprocess (subprocess-mode loop), this drill exercises:
#
#   1. Web UI planner walkthrough (sign-in, claim, submit)
#   2. Admin-reclaim drill (claim, do-not-submit, admin-reclaim)
#   3. End-state assertions adjusted for EDEN_PLAN_TASKS=4
#   4. Termination drill (compose stop --timeout 10; verify no SIGKILL)
#
# See docs/plans/eden-phase-10e-compose-e2e.md for the design rationale,
# in particular §B (race-free staged bring-up).

# Tooling preflight.
for tool in docker jq curl python3; do
    command -v "$tool" >/dev/null || {
        echo "e2e.sh requires '$tool' on PATH" >&2
        exit 2
    }
done
docker compose version >/dev/null || {
    echo "e2e.sh requires the 'docker compose' v2 plugin" >&2
    exit 2
}
python3 -c 'import httpx' 2>/dev/null || {
    echo "e2e.sh requires 'httpx' to be importable from python3" >&2
    echo "  install with: pip install 'httpx>=0.27,<1'" >&2
    exit 2
}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${COMPOSE_DIR}/../.." && pwd)"

cd "$COMPOSE_DIR"

ENV_FILE="$(mktemp)"
EXPERIMENT_ID="e2e-exp"

# Stage 1's wait-list — deliberately omits gitea (deferred 10d
# follow-up, not consumed by this drill) and planner-host (the whole
# point of staging is to keep planner-host away from the tasks the
# UI claims).
STAGE1_SERVICES=(
    postgres blob-init
    task-store-server orchestrator
    implementer-host evaluator-host web-ui
)

cleanup() {
    local rc=$?
    if [[ $rc -ne 0 ]]; then
        echo "--- cleanup: dumping compose state for diagnostics ---" >&2
        docker compose -f compose.yaml --env-file "$ENV_FILE" \
            ps -a >&2 2>&1 || true
        docker compose -f compose.yaml --env-file "$ENV_FILE" \
            logs --tail 60 >&2 2>&1 || true
    fi
    docker compose -f compose.yaml --env-file "$ENV_FILE" \
        down -v >/dev/null 2>&1 || true
    rm -f "$ENV_FILE"
    rm -f "${COMPOSE_DIR}/experiment-config.yaml"
    exit "$rc"
}
trap cleanup EXIT

echo "--- running setup-experiment ---"
bash "${REPO_ROOT}/reference/scripts/setup-experiment/setup-experiment.sh" \
    "${REPO_ROOT}/tests/fixtures/experiment/.eden/config.yaml" \
    --experiment-id "$EXPERIMENT_ID" \
    --env-file "$ENV_FILE"

# Override EDEN_PLAN_TASKS to 4 (default is 3). The four deterministic
# IDs plan-0001..plan-0004 are seeded; the drill claims plan-0001 and
# plan-0002, leaving plan-0003 and plan-0004 (plus reclaimed plan-0002)
# for the headless planner-host stage 2 picks up.
sed -i.bak 's/^EDEN_PLAN_TASKS=.*/EDEN_PLAN_TASKS=4/' "$ENV_FILE"
rm -f "${ENV_FILE}.bak"

echo "--- stage 1: bring up everything except gitea + planner-host ---"
docker compose -f compose.yaml --env-file "$ENV_FILE" up -d --wait \
    --wait-timeout 120 \
    "${STAGE1_SERVICES[@]}"

# Hard sanity check — codex round-1 finding: if stage 1's --wait took
# too long, the orchestrator could have quiesced and exited 0 in the
# meantime, leaving us with no actor to make progress on UI claims.
status="$(docker inspect --format '{{.State.Status}}' eden-orchestrator)"
test "$status" = "running" || {
    echo "orchestrator already exited at end of stage 1 (status=$status)" >&2
    docker compose -f compose.yaml --env-file "$ENV_FILE" \
        logs --tail 60 orchestrator >&2
    exit 1
}

# --- Run the python driver (planner walkthrough + admin reclaim) ---
WEB_UI_HOST_PORT="$(grep -E '^WEB_UI_HOST_PORT=' "$ENV_FILE" | cut -d= -f2-)"
EDEN_BASE_COMMIT_SHA="$(grep -E '^EDEN_BASE_COMMIT_SHA=' "$ENV_FILE" | cut -d= -f2-)"
test -n "$WEB_UI_HOST_PORT"
test -n "$EDEN_BASE_COMMIT_SHA"

echo "--- running e2e_drive.py against http://localhost:${WEB_UI_HOST_PORT} ---"
EDEN_E2E_WEB_UI_URL="http://localhost:${WEB_UI_HOST_PORT}" \
    EDEN_BASE_COMMIT_SHA="$EDEN_BASE_COMMIT_SHA" \
    python3 "${SCRIPT_DIR}/e2e_drive.py"

echo "--- stage 2: bring up planner-host ---"
docker compose -f compose.yaml --env-file "$ENV_FILE" up -d \
    --wait --wait-timeout 60 planner-host

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
    docker compose -f compose.yaml --env-file "$ENV_FILE" \
        logs --tail 60 orchestrator planner-host \
        implementer-host evaluator-host >&2
    exit 1
}
test "$(docker inspect --format '{{.State.ExitCode}}' eden-orchestrator)" = "0" || {
    echo "orchestrator exited non-zero" >&2
    docker compose -f compose.yaml --env-file "$ENV_FILE" \
        logs --tail 60 orchestrator >&2
    exit 1
}

echo "--- asserting final task-store state ---"
EDEN_SHARED_TOKEN="$(grep -E '^EDEN_SHARED_TOKEN=' "$ENV_FILE" | cut -d= -f2-)"
EVENTS_JSON="$(
    docker compose -f compose.yaml --env-file "$ENV_FILE" \
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
test "$TRIAL_INTEGRATED" -ge 4 || {
    echo "expected >= 4 trial.integrated events; got $TRIAL_INTEGRATED" >&2
    exit 1
}
TASK_COMPLETED="$(
    echo "$EVENTS_JSON" \
        | jq '(.events // .) | [.[] | select(.type == "task.completed")] | length'
)"
test "$TASK_COMPLETED" -ge 12 || {
    echo "expected >= 12 task.completed events; got $TASK_COMPLETED" >&2
    exit 1
}
PLAN_COMPLETED="$(
    echo "$EVENTS_JSON" \
        | jq '(.events // .) | [.[] | select(
              .type == "task.completed"
              and (.data.task_id | startswith("plan-"))
            )] | length'
)"
test "$PLAN_COMPLETED" -ge 4 || {
    echo "expected >= 4 plan-task.completed events; got $PLAN_COMPLETED" >&2
    exit 1
}
RECLAIMED_OPERATOR="$(
    echo "$EVENTS_JSON" \
        | jq '(.events // .) | [.[] | select(
              .type == "task.reclaimed"
              and .data.cause == "operator"
            )] | length'
)"
test "$RECLAIMED_OPERATOR" -ge 1 || {
    echo "expected >= 1 task.reclaimed (cause=operator) event; got $RECLAIMED_OPERATOR" >&2
    exit 1
}

echo "--- termination drill: stop with 10s budget ---"
docker compose -f compose.yaml --env-file "$ENV_FILE" stop --timeout 10

# Iterate every container compose still knows about. With
# eden-repo-init removed by `compose run --rm`, this naturally covers
# postgres, blob-init, task-store-server, orchestrator, planner-host,
# implementer-host, evaluator-host, and web-ui. (Gitea was never
# brought up so it does not appear here — codex round-1 fix.)
#
# Use process substitution + while-read instead of `mapfile` so this
# works under bash 3.2 (macOS default) and CI's bash 5+.
CONTAINERS=()
while IFS= read -r name; do
    [[ -n "$name" ]] && CONTAINERS+=("$name")
done < <(
    docker compose -f compose.yaml --env-file "$ENV_FILE" \
        ps -a --format '{{.Name}}'
)
test "${#CONTAINERS[@]}" -gt 0 || {
    echo "compose ps -a returned no containers; nothing to assert" >&2
    exit 1
}

for name in "${CONTAINERS[@]}"; do
    status="$(docker inspect --format '{{.State.Status}}' "$name")"
    code="$(docker inspect --format '{{.State.ExitCode}}' "$name")"
    test "$status" = "exited" || {
        echo "$name still in status $status after stop --timeout 10" >&2
        exit 1
    }
    test "$code" != "137" || {
        echo "$name was SIGKILLed (exit code 137)" >&2
        docker compose -f compose.yaml --env-file "$ENV_FILE" \
            logs --tail 30 "$name" >&2
        exit 1
    }
done

echo "PASS"
