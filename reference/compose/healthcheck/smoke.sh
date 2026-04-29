#!/usr/bin/env bash
set -euo pipefail

# Smoke test for the EDEN reference Compose stack. Wraps
# setup-experiment + `compose up --wait`, asserts the seeded ref,
# waits for the orchestrator to exit cleanly on quiescence, and
# verifies the final task-store state. Tears the stack down on exit.

# Tooling preflight — fail fast with a clear message if a required
# binary is missing.
for tool in docker jq curl python3; do
    command -v "$tool" >/dev/null || {
        echo "smoke.sh requires '$tool' on PATH" >&2
        exit 2
    }
done
docker compose version >/dev/null || {
    echo "smoke.sh requires the 'docker compose' v2 plugin" >&2
    exit 2
}

# Resolve to the compose dir regardless of where the script was
# invoked from (the CI job runs `bash healthcheck/smoke.sh` from
# `reference/compose/`; a developer might run it from anywhere).
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${COMPOSE_DIR}/../.." && pwd)"

cd "$COMPOSE_DIR"

ENV_FILE="$(mktemp)"
EXPERIMENT_ID="smoke-exp"

cleanup() {
    local rc=$?
    docker compose -f compose.yaml --env-file "$ENV_FILE" down -v >/dev/null 2>&1 || true
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

# Resolve the project name from compose config and assert it
# matches the value pinned in compose.yaml. Same pattern as 10a.
PROJECT="$(docker compose -f compose.yaml --env-file "$ENV_FILE" \
            config --format json | jq -r '.name')"
test "$PROJECT" = "eden-reference" || {
    echo "unexpected project name: $PROJECT" >&2
    exit 1
}

# Read the seed SHA from the env file so the post-up assertion can
# compare against it.
EDEN_BASE_COMMIT_SHA="$(grep -E '^EDEN_BASE_COMMIT_SHA=' "$ENV_FILE" | cut -d= -f2-)"
test -n "$EDEN_BASE_COMMIT_SHA" || {
    echo "setup-experiment did not write EDEN_BASE_COMMIT_SHA" >&2
    exit 1
}

echo "--- asserting seeded ref published to gitea ---"
# Phase 10d follow-up B: the seed lives on Gitea, not on a shared
# bare-repo volume. Probe via `git ls-remote` against the gitea
# container (Gitea was brought up by setup-experiment).
GITEA_REMOTE_PASSWORD="$(grep -E '^GITEA_REMOTE_PASSWORD=' "$ENV_FILE" | cut -d= -f2-)"
test -n "$GITEA_REMOTE_PASSWORD"
EDEN_EXPERIMENT_ID="$(grep -E '^EDEN_EXPERIMENT_ID=' "$ENV_FILE" | cut -d= -f2-)"
test -n "$EDEN_EXPERIMENT_ID"
SEEDED_SHA="$(
    docker compose -f compose.yaml --env-file "$ENV_FILE" \
        run --rm --no-deps \
        --entrypoint sh \
        eden-repo-init \
        -c "git ls-remote http://eden:${GITEA_REMOTE_PASSWORD}@gitea:3000/eden/${EDEN_EXPERIMENT_ID}.git refs/heads/main | awk '{print \$1}'"
)"
SEEDED_SHA="$(echo "$SEEDED_SHA" | tr -d '[:space:]')"
test "$SEEDED_SHA" = "$EDEN_BASE_COMMIT_SHA" || {
    echo "gitea seed mismatch: $SEEDED_SHA != $EDEN_BASE_COMMIT_SHA" >&2
    exit 1
}

echo "--- bringing up the full stack ---"
docker compose -f compose.yaml --env-file "$ENV_FILE" up -d --wait --wait-timeout 240

echo "--- compose ps ---"
docker compose -f compose.yaml --env-file "$ENV_FILE" ps -a

echo "--- asserting expected named volumes exist ---"
# Phase 10d follow-up B: per-service repo volumes (no shared
# eden-bare-repo anymore). eden-artifacts-data has explicit
# `name:` for the docker-exec wrap; other compose-prefixed volumes
# keep the default `<project>_<volume>` shape.
for vol in eden-postgres-data eden-gitea-data eden-blob-data \
           eden-orchestrator-repo eden-implementer-repo \
           eden-web-ui-repo; do
    docker volume inspect "${PROJECT}_${vol}" >/dev/null
done
# eden-evaluator-repo is declared in compose.yaml but only mounted
# in subprocess-mode (compose.subprocess.yaml's evaluator-host) —
# scripted-mode evaluator-host (compose.yaml) doesn't need git, so
# the volume isn't materialized. Subprocess-mode smokes assert it
# separately.
docker volume inspect eden-artifacts-data >/dev/null

echo "--- asserting blob-init exited 0 ---"
test "$(docker inspect --format '{{.State.ExitCode}}' eden-blob-init)" = "0"

echo "--- asserting Postgres accepts connections ---"
PG_USER="$(grep -E '^POSTGRES_USER=' "$ENV_FILE" | cut -d= -f2-)"
PG_DB="$(grep -E '^POSTGRES_DB=' "$ENV_FILE" | cut -d= -f2-)"
docker compose -f compose.yaml --env-file "$ENV_FILE" exec -T postgres \
    pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null

echo "--- asserting Gitea API responds ---"
GITEA_PORT="$(grep -E '^GITEA_HOST_PORT=' "$ENV_FILE" | cut -d= -f2-)"
curl -fsS "http://localhost:${GITEA_PORT}/api/v1/version" | jq -e '.version' >/dev/null

echo "--- waiting for orchestrator to exit on quiescence ---"
deadline=$((SECONDS + 180))
while [[ $SECONDS -lt $deadline ]]; do
    status="$(docker inspect --format '{{.State.Status}}' eden-orchestrator)"
    if [[ "$status" = "exited" ]]; then
        break
    fi
    sleep 2
done
test "$(docker inspect --format '{{.State.Status}}' eden-orchestrator)" = "exited" || {
    echo "orchestrator did not exit within 180s; current status: $status" >&2
    docker compose -f compose.yaml --env-file "$ENV_FILE" logs --tail 30 orchestrator >&2
    exit 1
}
test "$(docker inspect --format '{{.State.ExitCode}}' eden-orchestrator)" = "0" || {
    echo "orchestrator exited non-zero" >&2
    docker compose -f compose.yaml --env-file "$ENV_FILE" logs --tail 30 orchestrator >&2
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
test "$TRIAL_INTEGRATED" -ge 3 || {
    echo "expected >= 3 trial.integrated events; got $TRIAL_INTEGRATED" >&2
    exit 1
}

# Plan section G step 6: assert each plan task reached a terminal
# state. The terminal task event is `task.completed` (or
# `task.failed` / `task.cancelled`) — `task.terminated` per the plan
# refers to "any terminal task event" rather than a specific event
# name. With 3 plan tasks + 3 implement tasks + 3 evaluate tasks all
# completing on the success path, we expect >= 9 task.completed.
TASK_COMPLETED="$(
    echo "$EVENTS_JSON" \
        | jq '(.events // .) | [.[] | select(.type == "task.completed")] | length'
)"
test "$TASK_COMPLETED" -ge 9 || {
    echo "expected >= 9 task.completed events; got $TASK_COMPLETED" >&2
    exit 1
}

# Each of the 3 plan tasks specifically must reach `task.completed`.
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

echo "--- asserting trial/* refs published to gitea ---"
# Phase 10d follow-up B §D.6: trial/* refs must be visible on the
# Gitea remote after integration. Each `trial.integrated` event
# corresponds to a published ref.
TRIAL_REMOTE_REFS="$(
    docker compose -f compose.yaml --env-file "$ENV_FILE" \
        run --rm --no-deps \
        --entrypoint sh \
        eden-repo-init \
        -c "git ls-remote http://eden:${GITEA_REMOTE_PASSWORD}@gitea:3000/eden/${EDEN_EXPERIMENT_ID}.git 'refs/heads/trial/*' | wc -l" \
        | tr -d '[:space:]'
)"
test "$TRIAL_REMOTE_REFS" -ge 3 || {
    echo "expected >= 3 trial/* refs on gitea; got $TRIAL_REMOTE_REFS" >&2
    exit 1
}

echo "PASS"
