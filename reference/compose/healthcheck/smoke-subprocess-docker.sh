#!/usr/bin/env bash
set -euo pipefail

# Phase 10d follow-up A subprocess-mode + container-isolated smoke.
# Mirrors smoke-subprocess.sh but runs setup-experiment with
# `--exec-mode docker` so each *_command runs in a sibling container
# spawned via the host docker daemon (DooD), instead of inline on
# the worker host.
#
# After the experiment quiesces, asserts the same end-state numbers
# as smoke-subprocess.sh PLUS one new assertion: no leftover
# eden.host=<worker-hostname> sibling containers (catches a
# regression in the cidfile / post_kill_callback cleanup path).

for tool in docker jq curl python3; do
    command -v "$tool" >/dev/null || {
        echo "smoke-subprocess-docker.sh requires '$tool' on PATH" >&2
        exit 2
    }
done
docker compose version >/dev/null || {
    echo "smoke-subprocess-docker.sh requires the 'docker compose' v2 plugin" >&2
    exit 2
}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${COMPOSE_DIR}/../.." && pwd)"
EXPERIMENT_DIR="${REPO_ROOT}/tests/fixtures/experiment"

cd "$COMPOSE_DIR"

ENV_FILE="$(mktemp)"
EXPERIMENT_ID="smoke-exp-sub-docker"

cleanup() {
    local rc=$?
    docker compose -f compose.yaml -f compose.subprocess.yaml -f compose.docker-exec.yaml \
        --env-file "$ENV_FILE" down -v >/dev/null 2>&1 || true
    rm -f "$ENV_FILE"
    rm -f "${COMPOSE_DIR}/experiment-config.yaml"
    # Remove the host-side cidfile dir setup-experiment created.
    rm -rf "${COMPOSE_DIR}/.cidfiles-${EXPERIMENT_ID}"
    exit "$rc"
}
trap cleanup EXIT

echo "--- running setup-experiment (subprocess + docker exec mode) ---"
bash "${REPO_ROOT}/reference/scripts/setup-experiment/setup-experiment.sh" \
    "${EXPERIMENT_DIR}/.eden/config.yaml" \
    --experiment-id "$EXPERIMENT_ID" \
    --experiment-dir "$EXPERIMENT_DIR" \
    --env-file "$ENV_FILE" \
    --exec-mode docker

EDEN_BASE_COMMIT_SHA="$(grep -E '^EDEN_BASE_COMMIT_SHA=' "$ENV_FILE" | cut -d= -f2-)"
test -n "$EDEN_BASE_COMMIT_SHA"

EDEN_EXEC_MODE="$(grep -E '^EDEN_EXEC_MODE=' "$ENV_FILE" | cut -d= -f2-)"
test "$EDEN_EXEC_MODE" = "docker" || {
    echo "setup-experiment did not set EDEN_EXEC_MODE=docker (got '$EDEN_EXEC_MODE')" >&2
    exit 1
}

EDEN_EXEC_IMAGE="$(grep -E '^EDEN_EXEC_IMAGE=' "$ENV_FILE" | cut -d= -f2-)"
test -n "$EDEN_EXEC_IMAGE"
docker image inspect "$EDEN_EXEC_IMAGE" >/dev/null || {
    echo "EDEN_EXEC_IMAGE=$EDEN_EXEC_IMAGE not present after setup-experiment" >&2
    exit 1
}

echo "--- bringing up the full stack with subprocess + docker overlay ---"
docker compose -f compose.yaml -f compose.subprocess.yaml -f compose.docker-exec.yaml \
    --env-file "$ENV_FILE" up -d --wait --wait-timeout 240

# Sanity: assert the docker socket bind landed via the docker-exec
# overlay (regression catch — without compose.docker-exec.yaml the
# overlay should NOT mount the socket).
docker inspect eden-implementer-host --format \
    '{{range .HostConfig.Binds}}{{println .}}{{end}}' \
    | grep -q '/var/run/docker.sock' || {
    echo "implementer-host is missing /var/run/docker.sock bind in docker-exec mode" >&2
    exit 1
}

echo "--- waiting for orchestrator to exit on quiescence ---"
deadline=$((SECONDS + 300))
while [[ $SECONDS -lt $deadline ]]; do
    status="$(docker inspect --format '{{.State.Status}}' eden-orchestrator)"
    if [[ "$status" = "exited" ]]; then
        break
    fi
    sleep 2
done
test "$(docker inspect --format '{{.State.Status}}' eden-orchestrator)" = "exited" || {
    echo "orchestrator did not exit within 300s; current status: $status" >&2
    docker compose -f compose.yaml -f compose.subprocess.yaml -f compose.docker-exec.yaml \
        --env-file "$ENV_FILE" logs --tail 60 orchestrator planner-host \
        implementer-host evaluator-host >&2
    exit 1
}
test "$(docker inspect --format '{{.State.ExitCode}}' eden-orchestrator)" = "0" || {
    echo "orchestrator exited non-zero" >&2
    docker compose -f compose.yaml -f compose.subprocess.yaml -f compose.docker-exec.yaml \
        --env-file "$ENV_FILE" logs --tail 60 orchestrator >&2
    exit 1
}

echo "--- asserting final task-store state (subprocess + docker mode) ---"
EDEN_SHARED_TOKEN="$(grep -E '^EDEN_SHARED_TOKEN=' "$ENV_FILE" | cut -d= -f2-)"
EVENTS_JSON="$(
    docker compose -f compose.yaml -f compose.subprocess.yaml -f compose.docker-exec.yaml \
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

echo "--- asserting no per-task orphan sibling containers (implementer/evaluator) ---"
# Per-task short-lived spawns must terminate before quiescence. The
# planner subprocess is long-running and still alive at this point —
# it's only torn down when its worker-host container shuts down, so
# we exclude planner here and assert it separately after compose
# stop.
ORPHAN_COUNT=0
for role in implementer evaluator; do
    n="$(docker ps -aq --filter "label=eden.role=${role}" 2>/dev/null | wc -l | tr -d ' ')"
    ORPHAN_COUNT=$((ORPHAN_COUNT + n))
done
test "$ORPHAN_COUNT" -eq 0 || {
    echo "found $ORPHAN_COUNT orphan implementer/evaluator containers after quiescence" >&2
    for role in implementer evaluator; do
        docker ps -a --filter "label=eden.role=${role}" >&2
    done
    exit 1
}

echo "--- stopping worker hosts and asserting no planner sibling remains ---"
# After `compose stop`, no `eden.role=planner` sibling container
# may remain. This proves clean teardown end-to-end (clean SIGTERM
# OR SIGKILL escalation — either is acceptable for this smoke).
# The dedicated SIGKILL-escalation invariant is exercised by the
# `pytest.mark.docker` test
# `test_terminate_sigkill_path_invokes_post_kill_callback`.
docker compose -f compose.yaml -f compose.subprocess.yaml \
    --env-file "$ENV_FILE" stop --timeout 15 >/dev/null

PLANNER_LEFTOVER="$(docker ps -aq --filter label=eden.role=planner 2>/dev/null | wc -l | tr -d ' ')"
test "$PLANNER_LEFTOVER" -eq 0 || {
    echo "planner sibling container survived compose stop (count=$PLANNER_LEFTOVER)" >&2
    docker ps -a --filter label=eden.role=planner >&2
    exit 1
}

echo "PASS (subprocess + docker mode)"
