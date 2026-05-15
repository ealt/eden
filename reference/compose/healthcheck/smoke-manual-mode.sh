#!/usr/bin/env bash
set -euo pipefail

# 12a-2 wave 7 — manual-mode smoke for the EDEN reference Compose stack.
#
# Asserts the §6.1 / §6.2 orchestrator-role contract: when a
# dispatch_mode key is "manual", the auto-orchestrator MUST NOT run
# that decision. The drill flips `execution_dispatch` to manual,
# brings up the stack, drives the ideator through one ideation
# submission, and asserts that NO execution task is auto-created.
# Then it flips back to auto and asserts the orchestrator catches up
# (creates the execution task and proceeds to integration).
#
# Companion to `smoke.sh` (default-all-auto): this script's added
# coverage is the wire-observable assertion that manual mode is
# actually honored at the deployment layer, not just the unit-test
# layer.

# Tooling preflight.
for tool in docker jq curl python3; do
    command -v "$tool" >/dev/null || {
        echo "smoke-manual-mode.sh requires '$tool' on PATH" >&2
        exit 2
    }
done
docker compose version >/dev/null || {
    echo "smoke-manual-mode.sh requires the 'docker compose' v2 plugin" >&2
    exit 2
}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${COMPOSE_DIR}/../.." && pwd)"

cd "$COMPOSE_DIR"

ENV_FILE="$(mktemp)"
EXPERIMENT_ID="smoke-manual"

cleanup() {
    local rc=$?
    if [[ $rc -ne 0 ]]; then
        echo "--- cleanup: dumping compose state for diagnostics ---" >&2
        docker compose -f compose.yaml --env-file "$ENV_FILE" \
            ps -a >&2 2>&1 || true
        docker compose -f compose.yaml --env-file "$ENV_FILE" \
            logs --tail 80 orchestrator ideator-host task-store-server >&2 2>&1 || true
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

# Cap lifetime ideation at 1 so we exercise the manual-mode gate on
# a single deterministic ideation task → execution-dispatch decision.
sed -i.bak \
    -e 's/^EDEN_IDEATION_POLICY_TARGET_PENDING=.*/EDEN_IDEATION_POLICY_TARGET_PENDING=1/' \
    -e 's/^EDEN_IDEATION_POLICY_MAX_TOTAL=.*/EDEN_IDEATION_POLICY_MAX_TOTAL=1/' \
    "$ENV_FILE"
rm -f "${ENV_FILE}.bak"

EDEN_ADMIN_TOKEN="$(grep -E '^EDEN_ADMIN_TOKEN=' "$ENV_FILE" | cut -d= -f2-)"
test -n "$EDEN_ADMIN_TOKEN"
EDEN_ADMINS_INITIAL_MEMBER="$(grep -E '^EDEN_ADMINS_INITIAL_MEMBER=' "$ENV_FILE" | cut -d= -f2-)"
test -n "$EDEN_ADMINS_INITIAL_MEMBER"
EXP_BASE="/v0/experiments/${EXPERIMENT_ID}"
# Populated AFTER the full stack comes up; see the reissue step below.
ADMIN_WORKER_BEARER=""

call_wire() {
    # Issue a wire call against the task-store-server. Reads use the
    # admin bearer (broadly allowed); writes that hit §3.7 admins-
    # gated routes (PATCH /dispatch_mode, POST /tasks/{T}/reassign)
    # MUST use the reissued worker bearer instead, since the admin
    # principal is bootstrap-only and gets 403 on those routes.
    local method="$1" path="$2" body="${3:-}"
    local bearer="admin:${EDEN_ADMIN_TOKEN}"
    if [[ "$method" = "PATCH" ]]; then
        if [[ -z "$ADMIN_WORKER_BEARER" ]]; then
            echo "ADMIN_WORKER_BEARER not yet set; call_wire PATCH requires the reissue step to have run" >&2
            return 2
        fi
        bearer="$ADMIN_WORKER_BEARER"
    fi
    local args=(
        -fsS
        -X "$method"
        -H "Authorization: Bearer ${bearer}"
        -H "X-Eden-Experiment-Id: ${EXPERIMENT_ID}"
        -H "Content-Type: application/json"
    )
    if [[ -n "$body" ]]; then
        args+=(-d "$body")
    fi
    args+=("http://localhost:8080${path}")
    docker compose -f compose.yaml --env-file "$ENV_FILE" \
        exec -T task-store-server curl "${args[@]}"
}

echo "--- bringing up the full stack ---"
docker compose -f compose.yaml --env-file "$ENV_FILE" up -d --wait --wait-timeout 240

# Per spec §3.7, `update_dispatch_mode` requires a registered worker
# in the `admins` group; the literal `"admin"` bearer is bootstrap-
# only and MUST NOT drive business-op routes. Reissue the initial
# admin worker's credential (admin-token-gated, so the admin bearer
# is allowed here) and use `Bearer <worker_id>:<token>` for the
# subsequent PATCH /dispatch_mode calls. The reissue MUST run after
# `compose up -d --wait` so we hit the final task-store-server
# container (setup-experiment brought one up too, but `compose up`
# may have recreated it on config drift).
echo "--- reissuing initial admin worker credential for PATCH calls ---"
ADMIN_WORKER_TOKEN="$(
    docker compose -f compose.yaml --env-file "$ENV_FILE" \
        exec -T task-store-server curl -fsS \
            -X POST \
            -H "Authorization: Bearer admin:${EDEN_ADMIN_TOKEN}" \
            -H "X-Eden-Experiment-Id: ${EXPERIMENT_ID}" \
            -H "Content-Type: application/json" \
            "http://localhost:8080${EXP_BASE}/workers/${EDEN_ADMINS_INITIAL_MEMBER}/reissue-credential" \
        | jq -r '.registration_token'
)"
test -n "$ADMIN_WORKER_TOKEN"
test "$ADMIN_WORKER_TOKEN" != "null"
ADMIN_WORKER_BEARER="${EDEN_ADMINS_INITIAL_MEMBER}:${ADMIN_WORKER_TOKEN}"

# The orchestrator + ideator-host are running; the ideation task is
# being created by the policy + claimed by the ideator. We want to
# flip execution_dispatch to manual BEFORE the ideator finishes
# submitting, so the orchestrator doesn't auto-dispatch an execution
# task on the resulting `ready` idea.
#
# In practice the ideator submission is fast (<2s under the fixture);
# the most reliable way to guarantee the flip lands first is to flip
# IMMEDIATELY after `compose up --wait` returns. The orchestrator's
# next iteration (default poll-interval 1s) will see manual.

echo "--- flipping execution_dispatch to manual ---"
call_wire PATCH "${EXP_BASE}/dispatch_mode" \
    '{"execution_dispatch":"manual"}' >/dev/null

# Wait for an idea to reach `ready` state (ideator submitted). Up to
# 30s; the fixture ideator finishes near-instantly.
echo "--- waiting for an idea to reach ready ---"
deadline=$((SECONDS + 30))
ready_count=0
while [[ $SECONDS -lt $deadline ]]; do
    ready_count="$(
        call_wire GET "${EXP_BASE}/ideas?state=ready" | jq 'length'
    )"
    if [[ "$ready_count" -ge 1 ]]; then
        break
    fi
    sleep 1
done
test "$ready_count" -ge 1 || {
    echo "no idea reached 'ready' within 30s; ideator may be wedged" >&2
    exit 1
}

# Wait 10s past the flip + ready transition. If the orchestrator
# were honoring auto mode, an execution task would land in this
# window. Under manual mode, NO execution task should be created.
echo "--- asserting no execution task auto-created under manual mode ---"
sleep 10
exec_tasks="$(
    call_wire GET "${EXP_BASE}/tasks?kind=execution" | jq 'length'
)"
test "$exec_tasks" -eq 0 || {
    echo "expected 0 execution tasks under manual mode; got $exec_tasks" >&2
    echo "execution tasks:" >&2
    call_wire GET "${EXP_BASE}/tasks?kind=execution" >&2 || true
    exit 1
}

# Verify the dispatch_mode flip event landed in the log.
echo "--- asserting dispatch_mode_changed event ---"
events="$(call_wire GET "${EXP_BASE}/events")"
flip_count="$(
    echo "$events" \
        | jq '.events | [.[] | select(
            .type == "experiment.dispatch_mode_changed"
            and .data.changed.execution_dispatch == "manual"
        )] | length'
)"
test "$flip_count" -ge 1 || {
    echo "expected >= 1 dispatch_mode_changed event with changed.execution_dispatch=manual; got $flip_count" >&2
    exit 1
}

echo "--- flipping execution_dispatch back to auto ---"
call_wire PATCH "${EXP_BASE}/dispatch_mode" \
    '{"execution_dispatch":"auto"}' >/dev/null

# Now the orchestrator should catch up: dispatch the execution task,
# the executor claims + submits, the evaluator runs, and integration
# fires.
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
    echo "orchestrator did not exit within 180s after flipping back" >&2
    docker compose -f compose.yaml --env-file "$ENV_FILE" \
        logs --tail 60 orchestrator >&2
    exit 1
}
test "$(docker inspect --format '{{.State.ExitCode}}' eden-orchestrator)" = "0" || {
    echo "orchestrator exited non-zero" >&2
    exit 1
}

echo "--- final assertions: 1 variant.integrated + matching dispatch events ---"
events="$(call_wire GET "${EXP_BASE}/events")"
integrated="$(
    echo "$events" \
        | jq '.events | [.[] | select(.type == "variant.integrated")] | length'
)"
test "$integrated" -ge 1 || {
    echo "expected >= 1 variant.integrated after flip-back to auto; got $integrated" >&2
    exit 1
}
back_to_auto="$(
    echo "$events" \
        | jq '.events | [.[] | select(
            .type == "experiment.dispatch_mode_changed"
            and .data.changed.execution_dispatch == "auto"
        )] | length'
)"
test "$back_to_auto" -ge 1 || {
    echo "expected >= 1 dispatch_mode_changed event flipping execution_dispatch back to auto; got $back_to_auto" >&2
    exit 1
}

echo "PASS"
