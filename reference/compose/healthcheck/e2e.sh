#!/usr/bin/env bash
set -euo pipefail

# Phase 10e end-to-end smoke for the EDEN reference Compose stack.
# Beyond compose-smoke (headless 3-variant loop) and
# compose-smoke-subprocess (subprocess-mode loop), this drill exercises:
#
#   1. Web UI ideator walkthrough (sign-in, claim, submit)
#   2. Admin-reclaim drill (claim, do-not-submit, admin-reclaim)
#   3. End-state assertions adjusted for MAX_TOTAL=4 ideation tasks
#   4. Termination drill (compose stop --timeout 10; verify no SIGKILL)
#
# See docs/archive/eden-phase-10e-compose-e2e.md for the design rationale,
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
# Phase 12a-1g: per-smoke ephemeral data root, cleaned up on every
# exit path.
SMOKE_DATA_ROOT="$(mktemp -d -t eden-e2e-XXXXXX)"
EXPERIMENT_ID="e2e-exp"

# Stage 1's wait-list — deliberately omits gitea (deferred 10d
# follow-up, not consumed by this drill) and ideator-host (the whole
# point of staging is to keep ideator-host away from the tasks the
# UI claims). The legacy `blob-init` service was removed in
# Phase 12a-1g alongside the eden-blob-data volume.
STAGE1_SERVICES=(
    postgres
    task-store-server orchestrator
    executor-host evaluator-host web-ui
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
    # Phase 12a-1g hotfix: see smoke.sh for the full rationale —
    # bind-mount subdirs are populated with files the host can't `rm`
    # because container-created subdirectories are not world-writable.
    # Delete from inside a root-as-uid-0 sibling container first.
    # Defense-in-depth empty / "/" guard — see smoke.sh.
    if [[ -n "${SMOKE_DATA_ROOT:-}" \
        && "$SMOKE_DATA_ROOT" != "/" \
        && -d "$SMOKE_DATA_ROOT" ]]; then
        docker run --rm -v "$SMOKE_DATA_ROOT:/cleanup" alpine:3.20 \
            sh -c 'find /cleanup -mindepth 1 -delete' >/dev/null 2>&1 || true
    fi
    # `|| true` so a cleanup-side failure can't mask the script's
    # real exit code — see smoke.sh for the full rationale.
    rm -rf "$SMOKE_DATA_ROOT" || true
    exit "$rc"
}
trap cleanup EXIT

echo "--- running setup-experiment ---"
bash "${REPO_ROOT}/reference/scripts/setup-experiment/setup-experiment.sh" \
    "${REPO_ROOT}/tests/fixtures/experiment/.eden/config.yaml" \
    --experiment-id "$EXPERIMENT_ID" \
    --env-file "$ENV_FILE" \
    --data-root "$SMOKE_DATA_ROOT"

# 12a-2 wave 7: the policy-driven dispatch creates UUID-suffixed task
# ids (not the pre-12a-2 fixed ``ideation-NNNN`` shape). Set the
# target-pending depth to 4 and cap lifetime ideation at 4 so the
# orchestrator quiesces after 4 variants land. The e2e_drive.py
# script discovers the task ids dynamically from the wire — it
# can't assume ideation-0001..ideation-0004 anymore.
sed -i.bak \
    -e 's/^EDEN_IDEATION_POLICY_TARGET_PENDING=.*/EDEN_IDEATION_POLICY_TARGET_PENDING=4/' \
    -e 's/^EDEN_IDEATION_POLICY_MAX_TOTAL=.*/EDEN_IDEATION_POLICY_MAX_TOTAL=4/' \
    "$ENV_FILE"
rm -f "${ENV_FILE}.bak"

echo "--- stage 1: bring up everything except gitea + ideator-host ---"
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

# --- Run the python driver (ideator walkthrough + admin reclaim) ---
WEB_UI_HOST_PORT="$(grep -E '^WEB_UI_HOST_PORT=' "$ENV_FILE" | cut -d= -f2-)"
EDEN_BASE_COMMIT_SHA="$(grep -E '^EDEN_BASE_COMMIT_SHA=' "$ENV_FILE" | cut -d= -f2-)"
test -n "$WEB_UI_HOST_PORT"
test -n "$EDEN_BASE_COMMIT_SHA"

echo "--- running e2e_drive.py against http://localhost:${WEB_UI_HOST_PORT} ---"
EDEN_E2E_WEB_UI_URL="http://localhost:${WEB_UI_HOST_PORT}" \
    EDEN_BASE_COMMIT_SHA="$EDEN_BASE_COMMIT_SHA" \
    python3 "${SCRIPT_DIR}/e2e_drive.py"

echo "--- stage 2: bring up ideator-host ---"
docker compose -f compose.yaml --env-file "$ENV_FILE" up -d \
    --wait --wait-timeout 60 ideator-host

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
        logs --tail 60 orchestrator ideator-host \
        executor-host evaluator-host >&2
    exit 1
}
test "$(docker inspect --format '{{.State.ExitCode}}' eden-orchestrator)" = "0" || {
    echo "orchestrator exited non-zero" >&2
    docker compose -f compose.yaml --env-file "$ENV_FILE" \
        logs --tail 60 orchestrator >&2
    exit 1
}

echo "--- asserting final task-store state ---"
EDEN_ADMIN_TOKEN="$(grep -E '^EDEN_ADMIN_TOKEN=' "$ENV_FILE" | cut -d= -f2-)"
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
test "$VARIANT_INTEGRATED" -ge 4 || {
    echo "expected >= 4 variant.integrated events; got $VARIANT_INTEGRATED" >&2
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
IDEATION_COMPLETED="$(
    echo "$EVENTS_JSON" \
        | jq '(.events // .) | [.[] | select(
              .type == "task.completed"
              and (.data.task_id | startswith("ideation-"))
            )] | length'
)"
test "$IDEATION_COMPLETED" -ge 4 || {
    echo "expected >= 4 ideation-task.completed events; got $IDEATION_COMPLETED" >&2
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

# 12a-2 wave 7: the e2e drill reassigns one ideation task via the
# admin UI. Verify the resulting `task.reassigned` event matches the
# exact shape the drill requested: target is worker:ideator-1, AND
# reassigned_by is stamped from the web-ui's worker_id (the
# admins-group principal that drove the wire call). A bare
# "task.reassigned count >= 1" assertion would pass even if the
# event recorded a different target or attribution, so filter by
# all three fields.
EDEN_WEB_UI_WORKER_ID="$(grep -E '^EDEN_WEB_UI_WORKER_ID=' "$ENV_FILE" | cut -d= -f2-)"
test -n "$EDEN_WEB_UI_WORKER_ID"
TASK_REASSIGNED="$(
    echo "$EVENTS_JSON" \
        | jq --arg actor "$EDEN_WEB_UI_WORKER_ID" '(.events // .) | [.[] | select(
            .type == "task.reassigned"
            and .data.new_target.kind == "worker"
            and .data.new_target.id == "ideator-1"
            and .data.reassigned_by == $actor
          )] | length'
)"
test "$TASK_REASSIGNED" -ge 1 || {
    echo "expected >= 1 task.reassigned event with new_target=worker:ideator-1 reassigned_by=${EDEN_WEB_UI_WORKER_ID}; got $TASK_REASSIGNED" >&2
    echo "all task.reassigned events:" >&2
    echo "$EVENTS_JSON" | jq '(.events // .) | [.[] | select(.type == "task.reassigned")]' >&2 || true
    exit 1
}

# 12a-2 wave 7: the dispatch-mode toggle drill flips integration to
# manual + back to auto. Verify both `experiment.dispatch_mode_changed`
# events landed (or that at least one diff-bearing event is in the
# log — the flip-back-to-auto MAY collapse to no event depending on
# starting state, but the initial flip-to-manual MUST emit at least
# one event with `integration: manual` in `changed`).
DISPATCH_MODE_CHANGED="$(
    echo "$EVENTS_JSON" \
        | jq '(.events // .) | [.[] | select(.type == "experiment.dispatch_mode_changed")] | length'
)"
test "$DISPATCH_MODE_CHANGED" -ge 1 || {
    echo "expected >= 1 experiment.dispatch_mode_changed event; got $DISPATCH_MODE_CHANGED" >&2
    exit 1
}

echo "--- termination drill: stop with 10s budget ---"
docker compose -f compose.yaml --env-file "$ENV_FILE" stop --timeout 10

# Iterate every container compose still knows about. With
# eden-repo-init removed by `compose run --rm`, this naturally covers
# postgres, task-store-server, orchestrator, ideator-host,
# executor-host, evaluator-host, and web-ui. (Gitea was never
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
