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

# §6.4 contract:
#   - Ideation-task creation is BOUNDED-OVERSHOOT: with N=2 replicas
#     each targeting T=MAX_TOTAL=3, post-iteration pending MUST be
#     <= N*T = 6 (spec §6.4 / chapter 03 line 222). The fleet can
#     therefore produce anywhere between 3 and 6 ideation tasks
#     before quiescing — both are conforming.
#   - Execution-task dispatch, evaluation-task dispatch, and
#     integration are EXACT-IDEMPOTENT: each ideation task yields
#     at most one execution task, each variant at most one
#     evaluation task, each success variant at most one
#     variant.integrated event.
#
# So the deployment-level assertion is: forward progress (>= 3 of
# each, the MAX_TOTAL target) AND no overshoot beyond the §6.4 bound
# (<= 6 of each, the N*T ceiling). Cardinality across the three
# downstream stages MUST agree (one variant per ideation task that
# succeeded, one evaluation per variant, one integration per success
# variant), so we additionally cross-check that the three counts
# match.
echo "--- asserting end-state ---"
events="$(call_wire GET "${EXP_BASE}/events")"
integrated="$(
    echo "$events" \
        | jq '.events | [.[] | select(.type == "variant.integrated")] | length'
)"
exec_completed="$(
    echo "$events" \
        | jq '.events | [.[] | select(
            .type == "task.completed"
            and (.data.task_id | startswith("execution-"))
          )] | length'
)"
eval_completed="$(
    echo "$events" \
        | jq '.events | [.[] | select(
            .type == "task.completed"
            and (.data.task_id | startswith("evaluate-"))
          )] | length'
)"

# N * T upper bound: 2 replicas * MAX_TOTAL=3 = 6.
MAX_OVERSHOOT=6
MIN_PROGRESS=3
for name in integrated exec_completed eval_completed; do
    count="${!name}"
    if (( count < MIN_PROGRESS )); then
        echo "expected >= ${MIN_PROGRESS} ${name}; got ${count}" >&2
        exit 1
    fi
    if (( count > MAX_OVERSHOOT )); then
        echo "expected <= ${MAX_OVERSHOOT} ${name} (§6.4 N*T bound); got ${count}" >&2
        exit 1
    fi
done
if (( integrated != exec_completed || integrated != eval_completed )); then
    echo "downstream stage counts disagree: integrated=${integrated} exec=${exec_completed} eval=${eval_completed}" >&2
    exit 1
fi

echo "PASS"
