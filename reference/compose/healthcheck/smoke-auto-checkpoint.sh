#!/usr/bin/env bash
set -euo pipefail

# Smoke test for issue #131 automatic checkpointing via the Compose
# stack. Auto-checkpointing is a pure CONSUMER of the Phase 12b
# portable-checkpoint export endpoint (07-wire-protocol.md §14.1): the
# orchestrator periodically exports a checkpoint on a cadence and once
# when the experiment terminates.
#
# Flow:
#   1. setup-experiment + append an `auto_checkpoint`-enabled config
#      (short interval, small retention) AND a max_variants(3)
#      termination policy so the orchestrator drives the experiment to a
#      deterministic `terminated` state over its own orchestrators-group
#      worker bearer — the clean path that needs no extra admin wiring.
#   2. Bring up the full stack; wait for the orchestrator to exit (it
#      terminates via the policy, drains, then quiesces and exits 0).
#   3. Assert the host checkpoints dir carries:
#        - >= 1 periodic <safe_exp>-<TS>.tar archive,
#        - the periodic count never exceeds retention_count,
#        - each periodic archive parses as tar and carries manifest.json,
#        - exactly 1 terminal <safe_exp>-terminated-<TS>.tar archive.
#
# The smoke asserts STRUCTURAL validity, NOT repo-bundle completeness:
# the compose task-store-server carries no --repo-path today, so the
# git bundle inside the archive is empty (inherited 12b gap, tracked as
# a follow-up; see the CHANGELOG entry). Wire state round-trips; git
# history does not yet under Compose.
#
# The terminal assertion is gated on a REAL terminated state, NOT on
# orchestrator exit (a quiescent-but-running exit must produce no
# terminal archive). We let the orchestrator's own termination policy
# commit the transition rather than hand-rolling an admin-bearer
# terminate (POST .../terminate is group-gated to (admins,
# orchestrators) — the literal deployment-admin bearer is valid only for
# the bootstrap-class checkpoint export/import endpoints, §14).
#
# Bash-3.2 clean (no mapfile / declare -A) per AGENTS.md. Mirrors
# smoke-checkpoint.sh's preflight / cleanup / project-name conventions.

for tool in docker jq curl python3; do
    command -v "$tool" >/dev/null || {
        echo "smoke-auto-checkpoint.sh requires '$tool' on PATH" >&2
        exit 2
    }
done
docker compose version >/dev/null || {
    echo "smoke-auto-checkpoint.sh requires the 'docker compose' v2 plugin" >&2
    exit 2
}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${COMPOSE_DIR}/../.." && pwd)"

cd "$COMPOSE_DIR"

ENV_FILE="$(mktemp)"
SMOKE_DATA_ROOT="$(mktemp -d -t eden-smoke-autockpt-XXXXXX)"

# #128: mint an opaque exp_* experiment id (the wire grammar rejects the
# old typed mnemonic form).
EXPERIMENT_ID="$(python3 - <<'PY'
import secrets, time
alphabet = "0123456789abcdefghjkmnpqrstvwxyz"
value = ((int(time.time() * 1000) & ((1 << 48) - 1)) << 80) | secrets.randbits(80)
print("exp_" + "".join(alphabet[(value >> (5 * i)) & 31] for i in range(26))[::-1])
PY
)"

# The on-tree filename prefix the scheduler derives:
# sanitize(experiment_id) + "-" + sha256(experiment_id)[:8]. exp_* ids
# are already filesystem-safe, so sanitize is the identity here.
SAFE_PREFIX="$(python3 - "$EXPERIMENT_ID" <<'PY'
import hashlib, re, sys
exp = sys.argv[1]
sanitized = re.sub(r"[^A-Za-z0-9._-]", "_", exp)
print(f"{sanitized}-{hashlib.sha256(exp.encode()).hexdigest()[:8]}")
PY
)"

RETENTION_COUNT=3

cleanup() {
    local rc=$?
    docker compose -f compose.yaml --env-file "$ENV_FILE" down -v >/dev/null 2>&1 || true
    rm -f "$ENV_FILE"
    rm -f "${ENV_FILE}.bak"
    rm -f "${COMPOSE_DIR}/experiment-config.yaml"
    if [[ -n "${SMOKE_DATA_ROOT:-}" \
        && "$SMOKE_DATA_ROOT" != "/" \
        && -d "$SMOKE_DATA_ROOT" ]]; then
        docker run --rm -v "$SMOKE_DATA_ROOT:/cleanup" alpine:3.20 \
            sh -c 'find /cleanup -mindepth 1 -delete' >/dev/null 2>&1 || true
    fi
    rm -rf "$SMOKE_DATA_ROOT" || true
    exit "$rc"
}
trap cleanup EXIT

echo "=== Phase 1: setup-experiment + enable auto_checkpoint ==="

bash "${REPO_ROOT}/reference/scripts/setup-experiment/setup-experiment.sh" \
    "${REPO_ROOT}/tests/fixtures/experiment/.eden/config.yaml" \
    --experiment-id "$EXPERIMENT_ID" \
    --env-file "$ENV_FILE" \
    --data-root "$SMOKE_DATA_ROOT"

rm -f "${ENV_FILE}.bak"

# Append the auto_checkpoint + termination blocks to the copied compose
# config (same posture as smoke-checkpoint.sh appending
# max_quiescent_iterations). dispatch_mode.termination=auto +
# max_variants(3) drives a deterministic terminate after 3 variants; the
# short interval + small retention exercise the cadence + ring.
EXPERIMENT_CONFIG="${COMPOSE_DIR}/experiment-config.yaml"
cat >>"$EXPERIMENT_CONFIG" <<YAML
max_quiescent_iterations: 30
dispatch_mode:
  termination: auto
termination_policy:
  kind: max_variants
  target: 3
auto_checkpoint:
  enabled: true
  interval_seconds: 1
  retention_count: ${RETENTION_COUNT}
YAML

echo "--- bringing up the full stack ---"
docker compose -f compose.yaml --env-file "$ENV_FILE" up -d --wait --wait-timeout 240

# The experiment-config carries dispatch_mode.termination=auto, but the
# config does NOT seed the STORE's dispatch_mode (the store defaults
# termination=manual; dispatch_mode is operator-controlled at runtime).
# Flip it via PATCH /dispatch_mode so the orchestrator consults the
# max_variants(3) policy and drives a real `terminated` state. The
# orchestrator re-reads dispatch_mode every iteration, so flipping right
# after bring-up takes effect well before the experiment quiesces (it is
# still making progress dispatching the 3 variants). Once the policy
# fires the orchestrator commits the transition over its OWN
# orchestrators-group worker bearer (terminate is group-gated to admins
# OR orchestrators per #256) — no admin wiring needed for the terminate
# itself.
#
# PATCH /dispatch_mode is gated on the `admins` GROUP, which the literal
# `admin:<token>` deployment bearer is NOT a member of (that bearer is
# valid only for the bootstrap-class checkpoint export/import endpoints,
# §14). setup-experiment mints an `operator` worker INTO the admins
# group and persists its token; use that bearer.
TASK_STORE_PORT="$(grep -E '^TASK_STORE_HOST_PORT=' "$ENV_FILE" | cut -d= -f2- || echo 8080)"
test -n "$TASK_STORE_PORT" || TASK_STORE_PORT=8080
TS_BASE="http://localhost:${TASK_STORE_PORT}"
OPERATOR_WORKER_ID="$(grep -E '^EDEN_ADMINS_INITIAL_MEMBER=' "$ENV_FILE" | cut -d= -f2-)"
test -n "$OPERATOR_WORKER_ID"
OPERATOR_TOKEN="$(cat "${SMOKE_DATA_ROOT}/credentials/operator/${OPERATOR_WORKER_ID}.token")"
test -n "$OPERATOR_TOKEN"
OPERATOR_BEARER="${OPERATOR_WORKER_ID}:${OPERATOR_TOKEN}"

echo "--- flipping dispatch_mode.termination -> auto (operator bearer) ---"
curl -fsS -X PATCH \
    -H "Authorization: Bearer ${OPERATOR_BEARER}" \
    -H "X-Eden-Experiment-Id: ${EXPERIMENT_ID}" \
    -H "Content-Type: application/json" \
    -d '{"termination": "auto"}' \
    "${TS_BASE}/v0/experiments/${EXPERIMENT_ID}/dispatch_mode" >/dev/null
# Confirm the flip took before relying on it.
OBSERVED_TERMINATION="$(curl -fsS \
    -H "Authorization: Bearer ${OPERATOR_BEARER}" \
    -H "X-Eden-Experiment-Id: ${EXPERIMENT_ID}" \
    "${TS_BASE}/v0/experiments/${EXPERIMENT_ID}/dispatch_mode" | jq -r '.termination')"
test "$OBSERVED_TERMINATION" = "auto" || {
    echo "dispatch_mode.termination did not flip to auto (got: $OBSERVED_TERMINATION)" >&2
    exit 1
}

echo "=== Phase 2: wait for orchestrator to terminate + exit ==="
deadline=$((SECONDS + 600))
while [[ $SECONDS -lt $deadline ]]; do
    status="$(docker inspect --format '{{.State.Status}}' eden-orchestrator)"
    if [[ "$status" = "exited" ]]; then
        break
    fi
    sleep 2
done
test "$(docker inspect --format '{{.State.Status}}' eden-orchestrator)" = "exited" || {
    echo "orchestrator did not exit within 600s; current status: $status" >&2
    docker compose -f compose.yaml --env-file "$ENV_FILE" logs --tail 50 orchestrator >&2
    exit 1
}
test "$(docker inspect --format '{{.State.ExitCode}}' eden-orchestrator)" = "0" || {
    echo "orchestrator exited non-zero" >&2
    docker compose -f compose.yaml --env-file "$ENV_FILE" logs --tail 50 orchestrator >&2
    exit 1
}

echo "=== Phase 3: assert checkpoint archives on the host ==="

CKPT_DIR="${SMOKE_DATA_ROOT}/checkpoints"

# The orchestrator container writes the archives as uid 1000; a sibling
# root container makes them host-readable regardless of the runner uid.
docker run --rm -v "${CKPT_DIR}:/ckpt" alpine:3.20 \
    sh -c 'chmod -R a+rX /ckpt' >/dev/null 2>&1 || true

test -d "$CKPT_DIR" || {
    echo "checkpoints dir missing: $CKPT_DIR" >&2
    exit 1
}

# Classify archives by name (bash-3.2 array build, no mapfile). Only this
# experiment writes here, so prefix-match is unambiguous.
periodic=()
terminal=()
while IFS= read -r f; do
    [[ -n "$f" ]] || continue
    case "$f" in
        *-terminated-*.tar) terminal+=("$f") ;;
        *.tar)              periodic+=("$f") ;;
    esac
done < <(find "$CKPT_DIR" -maxdepth 1 -name "${SAFE_PREFIX}-*.tar" -type f | sort)

echo "periodic archives: ${#periodic[@]}; terminal archives: ${#terminal[@]}"

test "${#periodic[@]}" -ge 1 || {
    echo "expected >= 1 periodic checkpoint; found ${#periodic[@]}" >&2
    ls -la "$CKPT_DIR" >&2
    exit 1
}
test "${#periodic[@]}" -le "$RETENTION_COUNT" || {
    echo "periodic count ${#periodic[@]} exceeds retention_count ${RETENTION_COUNT}" >&2
    ls -la "$CKPT_DIR" >&2
    exit 1
}
test "${#terminal[@]}" -eq 1 || {
    echo "expected exactly 1 terminal checkpoint; found ${#terminal[@]}" >&2
    ls -la "$CKPT_DIR" >&2
    exit 1
}

# Each archive parses as tar and carries manifest.json (structural
# validity; NOT repo-bundle completeness — inherited 12b gap).
for f in "${periodic[@]}" "${terminal[@]}"; do
    tar -tf "$f" >/dev/null || {
        echo "archive does not parse as tar: $f" >&2
        exit 1
    }
    tar -tf "$f" | grep -q 'manifest\.json$' || {
        echo "archive missing manifest.json: $f" >&2
        tar -tf "$f" >&2
        exit 1
    }
done

echo "PASS"
