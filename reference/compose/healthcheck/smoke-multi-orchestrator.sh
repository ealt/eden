#!/usr/bin/env bash
set -euo pipefail

# 12a-2 wave 7 — multi-orchestrator smoke for the EDEN reference
# Compose stack.
#
# Asserts the §6.4 multi-instance safety invariants on a deployed
# two-orchestrator stack:
#
#   - Both replicas register cleanly into the `orchestrators` group
#     (each runs `_ensure_orchestrators_membership` at startup).
#   - Exact-idempotent decisions collapse cleanly: exactly one
#     execution task per idea reaches a terminal state, exactly one
#     evaluation task per variant, exactly N variant.integrated events.
#   - The chaos test: kill replica 1; replica 2 takes over and the
#     experiment still proceeds to quiescence.
#
# Bounded-overshoot ideation (the §6.4 ``N * T`` bound) is asserted
# at the unit-test level in
# eden-dispatch/tests/test_dispatch_mode_gating.py; this smoke
# focuses on the deployed-substrate observables.

for tool in docker jq curl python3; do
    command -v "$tool" >/dev/null || {
        echo "smoke-multi-orchestrator.sh requires '$tool' on PATH" >&2
        exit 2
    }
done
docker compose version >/dev/null || {
    echo "smoke-multi-orchestrator.sh requires the 'docker compose' v2 plugin" >&2
    exit 2
}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${COMPOSE_DIR}/../.." && pwd)"

cd "$COMPOSE_DIR"

ENV_FILE="$(mktemp)"
EXPERIMENT_ID="smoke-multi"

COMPOSE_FILES=(-f compose.yaml -f compose.multi-orchestrator.yaml)

cleanup() {
    local rc=$?
    if [[ $rc -ne 0 ]]; then
        echo "--- cleanup: dumping compose state for diagnostics ---" >&2
        docker compose "${COMPOSE_FILES[@]}" --env-file "$ENV_FILE" \
            ps -a >&2 2>&1 || true
        docker compose "${COMPOSE_FILES[@]}" --env-file "$ENV_FILE" \
            logs --tail 60 orchestrator orchestrator-2 >&2 2>&1 || true
    fi
    docker compose "${COMPOSE_FILES[@]}" --env-file "$ENV_FILE" \
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

# Pin lifetime ideation count to 3 so the §6.4 exact-idempotent
# decisions have a finite target to converge on.
sed -i.bak \
    -e 's/^EDEN_IDEATION_POLICY_TARGET_PENDING=.*/EDEN_IDEATION_POLICY_TARGET_PENDING=3/' \
    -e 's/^EDEN_IDEATION_POLICY_MAX_TOTAL=.*/EDEN_IDEATION_POLICY_MAX_TOTAL=3/' \
    "$ENV_FILE"
rm -f "${ENV_FILE}.bak"

EDEN_ADMIN_TOKEN="$(grep -E '^EDEN_ADMIN_TOKEN=' "$ENV_FILE" | cut -d= -f2-)"
test -n "$EDEN_ADMIN_TOKEN"
EXP_BASE="/v0/experiments/${EXPERIMENT_ID}"

call_wire() {
    local method="$1" path="$2"
    docker compose "${COMPOSE_FILES[@]}" --env-file "$ENV_FILE" \
        exec -T task-store-server curl -fsS \
            -X "$method" \
            -H "Authorization: Bearer admin:${EDEN_ADMIN_TOKEN}" \
            -H "X-Eden-Experiment-Id: ${EXPERIMENT_ID}" \
            "http://localhost:8080${path}"
}

echo "--- bringing up the full stack with multi-orchestrator overlay ---"
docker compose "${COMPOSE_FILES[@]}" --env-file "$ENV_FILE" \
    up -d --wait --wait-timeout 240

# Both replicas should have joined the `orchestrators` group at
# startup via the wave-4 `_ensure_orchestrators_membership` helper.
# Assert both worker_ids are members.
echo "--- asserting both orchestrators joined the orchestrators group ---"
ORCH_JSON="$(call_wire GET "${EXP_BASE}/groups/orchestrators")"
for wid in orchestrator orchestrator-2; do
    echo "$ORCH_JSON" | jq -e --arg w "$wid" '.members | index($w)' >/dev/null \
        || {
            echo "orchestrators group missing $wid: $ORCH_JSON" >&2
            exit 1
        }
done

# Chaos test: kill the primary orchestrator after a brief window.
# The secondary MUST drive the experiment to completion on its own.
# We can't pin _which_ replica wins each decision (the §6.4
# exact-idempotent path collapses cleanly to either), only the final
# end-state.
echo "--- killing primary orchestrator ---"
docker kill eden-orchestrator >/dev/null
# `restart: "on-failure"` would restart on non-zero exit; SIGKILL
# bypasses graceful shutdown and the container exits 137 → restart
# kicks in. Wait for it to come back up to keep the chaos test
# deterministic (the secondary still drives the actual work).
sleep 3

echo "--- waiting for orchestrator-2 to exit on quiescence ---"
deadline=$((SECONDS + 240))
while [[ $SECONDS -lt $deadline ]]; do
    status="$(docker inspect --format '{{.State.Status}}' eden-orchestrator-2)"
    if [[ "$status" = "exited" ]]; then
        break
    fi
    sleep 2
done
test "$(docker inspect --format '{{.State.Status}}' eden-orchestrator-2)" = "exited" || {
    echo "orchestrator-2 did not exit within 240s; status=$status" >&2
    docker compose "${COMPOSE_FILES[@]}" --env-file "$ENV_FILE" \
        logs --tail 60 orchestrator-2 >&2
    exit 1
}
test "$(docker inspect --format '{{.State.ExitCode}}' eden-orchestrator-2)" = "0" || {
    echo "orchestrator-2 exited non-zero" >&2
    exit 1
}

# §6.4 exact-idempotent: exactly N variant.integrated events for N
# ideation tasks. With MAX_TOTAL=3 and the chaos test, both replicas
# could in principle race; the integrate path's same-value
# idempotency (chapter 7 §5) collapses to one event per variant.
echo "--- asserting end-state ---"
events="$(call_wire GET "${EXP_BASE}/events")"
integrated="$(
    echo "$events" \
        | jq '.events | [.[] | select(.type == "variant.integrated")] | length'
)"
test "$integrated" -eq 3 || {
    echo "expected exactly 3 variant.integrated; got $integrated" >&2
    exit 1
}
# Same posture for execution tasks: 3 ideas × 1 execution task each.
exec_completed="$(
    echo "$events" \
        | jq '.events | [.[] | select(
            .type == "task.completed"
            and (.data.task_id | startswith("execution-"))
          )] | length'
)"
test "$exec_completed" -eq 3 || {
    echo "expected exactly 3 execution-task.completed events; got $exec_completed" >&2
    exit 1
}
# And evaluations.
eval_completed="$(
    echo "$events" \
        | jq '.events | [.[] | select(
            .type == "task.completed"
            and (.data.task_id | startswith("evaluate-"))
          )] | length'
)"
test "$eval_completed" -eq 3 || {
    echo "expected exactly 3 evaluation-task.completed events; got $eval_completed" >&2
    exit 1
}

echo "PASS"
