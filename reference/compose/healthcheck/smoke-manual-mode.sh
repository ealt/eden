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
# a single deterministic ideation task → execution-dispatch decision
# (issue #133).
# Issue #157: max_quiescent_iterations is now an experiment-config field
# (30 reproduces the retired EDEN_MAX_QUIESCENT_ITERATIONS:-30 default).
EXPERIMENT_CONFIG="${REPO_ROOT}/reference/compose/experiment-config.yaml"
cat >>"$EXPERIMENT_CONFIG" <<'YAML'
ideation_policy:
  kind: fixed_total
  total: 1
max_quiescent_iterations: 30
YAML

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

# Per spec §3.7, `update_dispatch_mode` requires a registered worker
# in the `admins` group; the literal `"admin"` bearer is bootstrap-
# only and MUST NOT drive business-op routes. Reissue the initial
# admin worker's credential (admin-token-gated, so the admin bearer
# is allowed here) and use `Bearer <worker_id>:<token>` for the
# subsequent PATCH /dispatch_mode calls.
#
# The reissue + PATCH MUST happen BEFORE bringing up the orchestrator
# + worker-host services. The fixture ideator-host claims and submits
# in ~milliseconds, and the orchestrator's first iteration both
# finalizes the ideation submission AND auto-dispatches the execution
# task in the same pass. By the time a smoke running AFTER `compose
# up --wait` could fire its PATCH, the execution task has already
# been auto-dispatched — the race is unwinnable from outside the
# orchestrator's first iteration. setup-experiment already brought
# up task-store-server (for the group bootstrap), so PATCH against
# the running task-store-server before the workers come up.
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

echo "--- flipping execution_dispatch to manual (BEFORE orchestrator starts) ---"
call_wire PATCH "${EXP_BASE}/dispatch_mode" \
    '{"execution_dispatch":"manual"}' >/dev/null

echo "--- bringing up the full stack ---"
docker compose -f compose.yaml --env-file "$ENV_FILE" up -d --wait --wait-timeout 240

# Wait for the ideation task to complete. The orchestrator's first
# iteration sees execution_dispatch=manual already, so it'll create
# an ideation task (ideation_creation is auto), the ideator submits,
# the orchestrator finalizes it (state=completed), but skips
# execution dispatch entirely. We poll for `task.completed` of
# `ideation-*` shape so the assertion is durable across the
# ideator's lifecycle (drafting → ready → completed).
echo "--- waiting for ideation task to complete ---"
deadline=$((SECONDS + 30))
ideation_completed=0
while [[ $SECONDS -lt $deadline ]]; do
    events="$(call_wire GET "${EXP_BASE}/events")"
    ideation_completed="$(
        echo "$events" \
            | jq '.events | [.[] | select(
                .type == "task.completed"
                and (.data.task_id | startswith("ideation-"))
              )] | length'
    )"
    if [[ "$ideation_completed" -ge 1 ]]; then
        break
    fi
    sleep 1
done
test "$ideation_completed" -ge 1 || {
    echo "no ideation task.completed event within 30s; ideator may be wedged" >&2
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
