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
# Phase 12a-1g: per-smoke ephemeral data root so the bind-mount
# substrate tree is isolated from the operator's real experiments
# under ~/.eden/. The trap cleans up on every exit path.
SMOKE_DATA_ROOT="$(mktemp -d -t eden-smoke-XXXXXX)"
EXPERIMENT_ID="smoke-exp"

cleanup() {
    local rc=$?
    docker compose -f compose.yaml --env-file "$ENV_FILE" down -v >/dev/null 2>&1 || true
    rm -f "$ENV_FILE"
    rm -f "${COMPOSE_DIR}/experiment-config.yaml"
    # Phase 12a-1g hotfix: substrate bind-mount subdirs (postgres,
    # gitea, *-repo) contain files written by containers as uids the
    # host runner doesn't match (postgres=70, gitea/eden=1000), inside
    # subdirectories the containers created with the container's
    # umask (mode 0755 — NOT world-writable). The host's `rm -rf` then
    # fails with EACCES on every file under those subdirs. Delete
    # via a sibling container running as root, where uid mismatches
    # don't matter; then rmdir the now-empty bind-mount root from the
    # host side. The `|| true` keeps a cleanup failure from masking
    # the script's actual exit code.
    if [[ -d "$SMOKE_DATA_ROOT" ]]; then
        docker run --rm -v "$SMOKE_DATA_ROOT:/cleanup" alpine:3.20 \
            sh -c 'find /cleanup -mindepth 1 -delete' >/dev/null 2>&1 || true
    fi
    rm -rf "$SMOKE_DATA_ROOT"
    exit "$rc"
}
trap cleanup EXIT

echo "--- running setup-experiment ---"
bash "${REPO_ROOT}/reference/scripts/setup-experiment/setup-experiment.sh" \
    "${REPO_ROOT}/tests/fixtures/experiment/.eden/config.yaml" \
    --experiment-id "$EXPERIMENT_ID" \
    --env-file "$ENV_FILE" \
    --data-root "$SMOKE_DATA_ROOT"

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

echo "--- asserting expected substrate bind-mounts exist ---"
# Phase 12a-1g: durable substrates live as host directories under
# $SMOKE_DATA_ROOT (chapter 01 §13). setup-experiment creates every
# substrate subdir unconditionally (regardless of which overlay is
# layered on later), so the existence assertion covers ALL of them
# even though scripted-mode skips evaluator-repo at runtime.
for sub in postgres gitea orchestrator-repo web-ui-repo executor-repo \
           evaluator-repo artifacts \
           credentials/orchestrator credentials/ideator \
           credentials/executor credentials/evaluator credentials/web-ui; do
    test -d "${SMOKE_DATA_ROOT}/${sub}" || {
        echo "missing substrate directory: ${SMOKE_DATA_ROOT}/${sub}" >&2
        exit 1
    }
done
# eden-repo-init-staging stays a named volume (intentionally
# ephemeral); eden-worktrees similarly (only present under the
# subprocess overlay, so not asserted by the scripted smoke).
docker volume inspect eden-repo-init-staging >/dev/null

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
EDEN_ADMIN_TOKEN="$(grep -E '^EDEN_ADMIN_TOKEN=' "$ENV_FILE" | cut -d= -f2-)"
# 12a-1 wave 6: each worker host registers itself at startup via the
# admin bearer (chapter 02 §6.3 + plan §D.1). The smoke verifies the
# registry is non-empty before reading the event stream — if any
# host failed to register, the orchestrator's task-completed
# assertions below would mask it as "no progress" rather than "auth
# wiring broken".
WORKERS_JSON="$(
    docker compose -f compose.yaml --env-file "$ENV_FILE" \
        exec -T task-store-server \
        curl -fsS \
            -H "Authorization: Bearer admin:${EDEN_ADMIN_TOKEN}" \
            -H "X-Eden-Experiment-Id: ${EXPERIMENT_ID}" \
            "http://localhost:8080/v0/experiments/${EXPERIMENT_ID}/workers"
)"
REGISTERED_WORKERS="$(echo "$WORKERS_JSON" | jq '[.workers[] | .worker_id] | length')"
test "$REGISTERED_WORKERS" -ge 4 || {
    echo "expected >= 4 registered workers (orchestrator + ideator-1 + executor-1 + evaluator-1); got $REGISTERED_WORKERS" >&2
    echo "workers response: $WORKERS_JSON" >&2
    exit 1
}
EVENTS_JSON="$(
    docker compose -f compose.yaml --env-file "$ENV_FILE" \
        exec -T task-store-server \
        curl -fsS \
            -H "Authorization: Bearer admin:${EDEN_ADMIN_TOKEN}" \
            -H "X-Eden-Experiment-Id: ${EXPERIMENT_ID}" \
            "http://localhost:8080/v0/experiments/${EXPERIMENT_ID}/events"
)"
VARIANT_INTEGRATED="$(
    echo "$EVENTS_JSON" \
        | jq '(.events // .) | [.[] | select(.type == "variant.integrated")] | length'
)"
test "$VARIANT_INTEGRATED" -ge 3 || {
    echo "expected >= 3 variant.integrated events; got $VARIANT_INTEGRATED" >&2
    exit 1
}

# Plan section G step 6: assert each ideation task reached a terminal
# state. The terminal task event is `task.completed` (or
# `task.failed` / `task.cancelled`) — `task.terminated` per the plan
# refers to "any terminal task event" rather than a specific event
# name. With 3 ideation tasks + 3 execution tasks + 3 evaluation tasks all
# completing on the success path, we expect >= 9 task.completed.
TASK_COMPLETED="$(
    echo "$EVENTS_JSON" \
        | jq '(.events // .) | [.[] | select(.type == "task.completed")] | length'
)"
test "$TASK_COMPLETED" -ge 9 || {
    echo "expected >= 9 task.completed events; got $TASK_COMPLETED" >&2
    exit 1
}

# Each of the 3 ideation tasks specifically must reach `task.completed`.
IDEATION_COMPLETED="$(
    echo "$EVENTS_JSON" \
        | jq '(.events // .) | [.[] | select(
              .type == "task.completed"
              and (.data.task_id | startswith("ideation-"))
            )] | length'
)"
test "$IDEATION_COMPLETED" -ge 3 || {
    echo "expected >= 3 ideation-task.completed events; got $IDEATION_COMPLETED" >&2
    exit 1
}

echo "--- asserting variant/* refs published to gitea ---"
# Phase 10d follow-up B §D.6: variant/* refs must be visible on the
# Gitea remote after integration. Each `variant.integrated` event
# corresponds to a published ref.
VARIANT_REMOTE_REFS="$(
    docker compose -f compose.yaml --env-file "$ENV_FILE" \
        run --rm --no-deps \
        --entrypoint sh \
        eden-repo-init \
        -c "git ls-remote http://eden:${GITEA_REMOTE_PASSWORD}@gitea:3000/eden/${EDEN_EXPERIMENT_ID}.git 'refs/heads/variant/*' | wc -l" \
        | tr -d '[:space:]'
)"
test "$VARIANT_REMOTE_REFS" -ge 3 || {
    echo "expected >= 3 variant/* refs on gitea; got $VARIANT_REMOTE_REFS" >&2
    exit 1
}

echo "PASS"
