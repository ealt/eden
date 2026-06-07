#!/usr/bin/env bash
set -euo pipefail

# Phase 10d subprocess-mode smoke test for the EDEN reference Compose
# stack. Mirrors smoke.sh but layers on `compose.subprocess.yaml` so
# the ideator / executor / evaluator hosts run the fixture's
# user-supplied `*_command` scripts (`ideation.py` / `execution.py` /
# `evaluation.py`) instead of their scripted profiles.

for tool in docker jq curl python3; do
    command -v "$tool" >/dev/null || {
        echo "smoke-subprocess.sh requires '$tool' on PATH" >&2
        exit 2
    }
done
docker compose version >/dev/null || {
    echo "smoke-subprocess.sh requires the 'docker compose' v2 plugin" >&2
    exit 2
}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${COMPOSE_DIR}/../.." && pwd)"
EXPERIMENT_DIR="${REPO_ROOT}/tests/fixtures/experiment"

cd "$COMPOSE_DIR"

ENV_FILE="$(mktemp)"
# Phase 12a-1g: per-smoke ephemeral data root, cleaned up on every
# exit path. See smoke.sh for rationale.
SMOKE_DATA_ROOT="$(mktemp -d -t eden-smoke-sub-XXXXXX)"
# #128: mint an opaque `exp_*` experiment id (the wire grammar rejects
# the old typed "smoke-exp-sub" mnemonic). Same Crockford-base32 ULID
# one-liner setup-experiment uses; passed via `--experiment-id` and
# echoed back to `.env` as EDEN_EXPERIMENT_ID.
EXPERIMENT_ID="$(python3 - <<'PY'
import secrets, time
alphabet = "0123456789abcdefghjkmnpqrstvwxyz"
value = ((int(time.time() * 1000) & ((1 << 48) - 1)) << 80) | secrets.randbits(80)
print("exp_" + "".join(alphabet[(value >> (5 * i)) & 31] for i in range(26))[::-1])
PY
)"

cleanup() {
    local rc=$?
    docker compose -f compose.yaml -f compose.subprocess.yaml \
        --env-file "$ENV_FILE" down -v >/dev/null 2>&1 || true
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

echo "--- running setup-experiment (subprocess overlay) ---"
bash "${REPO_ROOT}/reference/scripts/setup-experiment/setup-experiment.sh" \
    "${EXPERIMENT_DIR}/.eden/config.yaml" \
    --experiment-id "$EXPERIMENT_ID" \
    --experiment-dir "$EXPERIMENT_DIR" \
    --env-file "$ENV_FILE" \
    --data-root "$SMOKE_DATA_ROOT"

# Issue #133: cap lifetime ideation creation so the policy quiesces
# after 3 variants. Same posture as smoke.sh.
# Issue #157: max_quiescent_iterations is now an experiment-config field
# (30 reproduces the retired EDEN_MAX_QUIESCENT_ITERATIONS:-30 default).
EXPERIMENT_CONFIG="${REPO_ROOT}/reference/compose/experiment-config.yaml"
cat >>"$EXPERIMENT_CONFIG" <<'YAML'
ideation_policy:
  kind: fixed_total
  total: 3
max_quiescent_iterations: 30
YAML

EDEN_BASE_COMMIT_SHA="$(grep -E '^EDEN_BASE_COMMIT_SHA=' "$ENV_FILE" | cut -d= -f2-)"
test -n "$EDEN_BASE_COMMIT_SHA"

EDEN_EXPERIMENT_DIR_HOST="$(grep -E '^EDEN_EXPERIMENT_DIR_HOST=' "$ENV_FILE" | cut -d= -f2-)"
test -n "$EDEN_EXPERIMENT_DIR_HOST" || {
    echo "setup-experiment did not write EDEN_EXPERIMENT_DIR_HOST" >&2
    exit 1
}
test -d "$EDEN_EXPERIMENT_DIR_HOST"

echo "--- bringing up the full stack with subprocess overlay ---"
docker compose -f compose.yaml -f compose.subprocess.yaml \
    --env-file "$ENV_FILE" up -d --wait --wait-timeout 240

# Sanity: assert the docker socket is NOT bound into the worker host
# in plain subprocess (host) mode. Without compose.docker-exec.yaml
# layered on, the socket mount must not appear — this guards against
# accidental DooD privilege drift back into the host-mode overlay.
if docker inspect eden-executor-host --format \
       '{{range .HostConfig.Binds}}{{println .}}{{end}}' \
       | grep -q '/var/run/docker.sock'; then
    echo "executor-host has /var/run/docker.sock bound in HOST mode" >&2
    echo "(this should only happen with compose.docker-exec.yaml layered on)" >&2
    exit 1
fi

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
    docker compose -f compose.yaml -f compose.subprocess.yaml \
        --env-file "$ENV_FILE" logs --tail 50 orchestrator ideator-host \
        executor-host evaluator-host >&2
    exit 1
}
test "$(docker inspect --format '{{.State.ExitCode}}' eden-orchestrator)" = "0" || {
    echo "orchestrator exited non-zero" >&2
    docker compose -f compose.yaml -f compose.subprocess.yaml \
        --env-file "$ENV_FILE" logs --tail 50 orchestrator >&2
    exit 1
}

echo "--- asserting final task-store state (subprocess mode) ---"
EDEN_ADMIN_TOKEN="$(grep -E '^EDEN_ADMIN_TOKEN=' "$ENV_FILE" | cut -d= -f2-)"
# 12a-1 wave 6: each worker host auto-registers at startup. Verify
# the registry is non-empty before reading the event stream so that
# any wiring break surfaces as "no registered workers" rather than
# "no progress".
WORKERS_JSON="$(
    docker compose -f compose.yaml -f compose.subprocess.yaml \
        --env-file "$ENV_FILE" \
        exec -T task-store-server \
        curl -fsS \
            -H "Authorization: Bearer admin:${EDEN_ADMIN_TOKEN}" \
            -H "X-Eden-Experiment-Id: ${EXPERIMENT_ID}" \
            "http://localhost:8080/v0/experiments/${EXPERIMENT_ID}/workers"
)"
REGISTERED_WORKERS="$(echo "$WORKERS_JSON" | jq '[.workers[] | .worker_id] | length')"
test "$REGISTERED_WORKERS" -ge 4 || {
    echo "expected >= 4 registered workers; got $REGISTERED_WORKERS" >&2
    echo "workers response: $WORKERS_JSON" >&2
    exit 1
}
EVENTS_JSON="$(
    docker compose -f compose.yaml -f compose.subprocess.yaml \
        --env-file "$ENV_FILE" \
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
TASK_COMPLETED="$(
    echo "$EVENTS_JSON" \
        | jq '(.events // .) | [.[] | select(.type == "task.completed")] | length'
)"
test "$TASK_COMPLETED" -ge 9 || {
    echo "expected >= 9 task.completed events; got $TASK_COMPLETED" >&2
    exit 1
}
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

# ----------------------------------------------------------------------
# 12a-1f substrate-access smoke (§6.6 of the plan)
# ----------------------------------------------------------------------

echo "--- asserting 12a-1f substrate-access (ideator git repo) ---"
# Subprocess-mode ideator should have a populated bare clone at
# /var/lib/eden/repo. The integrator's variant/* refs land there
# after quiescence; we assert at least one commit is reachable.
# Pass `-c safe.directory=*` so git tolerates the bind-mount's
# host-uid (1001 from setup-experiment) vs container-uid (eden:1000)
# mismatch — matches the eden_git wrapper's internal flag pattern.
IDEATOR_LOG_LINES="$(
    docker compose -f compose.yaml -f compose.subprocess.yaml \
        --env-file "$ENV_FILE" \
        exec -T ideator-host \
        git -c 'safe.directory=*' -C /var/lib/eden/repo log --oneline --all \
        | wc -l | tr -d ' '
)" || {
    echo "ideator-host git log failed; dumping diagnostics:" >&2
    docker compose -f compose.yaml -f compose.subprocess.yaml \
        --env-file "$ENV_FILE" \
        exec -T ideator-host sh -c \
        'ls -la /var/lib/eden/repo/ 2>&1; echo ---; id; echo ---; git -c safe.directory=* -C /var/lib/eden/repo log --oneline --all 2>&1' >&2 || true
    docker compose -f compose.yaml -f compose.subprocess.yaml \
        --env-file "$ENV_FILE" \
        logs --tail 80 ideator-host >&2 || true
    exit 1
}
test "${IDEATOR_LOG_LINES:-0}" -ge 1 || {
    echo "expected >= 1 commit reachable from ideator-host's bare clone; got $IDEATOR_LOG_LINES" >&2
    docker compose -f compose.yaml -f compose.subprocess.yaml \
        --env-file "$ENV_FILE" \
        exec -T ideator-host sh -c \
        'ls -la /var/lib/eden/repo/ 2>&1; echo ---; ls -la /var/lib/eden/repo/refs/heads 2>&1' >&2 || true
    exit 1
}

echo "--- asserting 12a-1f substrate-access (artifact route) ---"
# The ideator subprocess fixture writes content.md under
# /var/lib/eden/artifacts/ideas/<idea_id>/. Pick one and fetch via
# the task-store-server route.
# List artifacts; the volume should contain the ideator-produced
# content files.
ARTIFACT_PATH="$(
    docker compose -f compose.yaml -f compose.subprocess.yaml \
        --env-file "$ENV_FILE" \
        exec -T task-store-server \
        sh -c 'find /var/lib/eden/artifacts/ideas -maxdepth 3 -name content.md -print -quit 2>/dev/null'
)"
ARTIFACT_PATH="$(echo "$ARTIFACT_PATH" | tr -d '\r')"
test -n "$ARTIFACT_PATH" || {
    echo "no artifact files found under /var/lib/eden/artifacts/ideas" >&2
    exit 1
}
# Strip the /var/lib/eden/artifacts/ prefix to get the URL-suffix
# path the route expects.
ARTIFACT_REL="${ARTIFACT_PATH#/var/lib/eden/artifacts/}"
ART_RESPONSE_CODE="$(
    docker compose -f compose.yaml -f compose.subprocess.yaml \
        --env-file "$ENV_FILE" \
        exec -T task-store-server \
        curl -fsS -o /tmp/art-body -w '%{http_code}' \
            -H "Authorization: Bearer admin:${EDEN_ADMIN_TOKEN}" \
            "http://localhost:8080/_reference/experiments/${EXPERIMENT_ID}/artifacts/${ARTIFACT_REL}"
)"
test "$ART_RESPONSE_CODE" = "200" || {
    echo "artifact route returned $ART_RESPONSE_CODE for $ARTIFACT_REL" >&2
    exit 1
}
ART_BODY_LEN="$(
    docker compose -f compose.yaml -f compose.subprocess.yaml \
        --env-file "$ENV_FILE" \
        exec -T task-store-server \
        sh -c 'wc -c < /tmp/art-body' | tr -d ' '
)"
test "$ART_BODY_LEN" -gt 0 || {
    echo "artifact route returned empty body for $ARTIFACT_REL" >&2
    exit 1
}

echo "--- asserting 12a-1f substrate-access (readonly Postgres) ---"
# psql is NOT installed in eden-reference:dev (only git / curl / ca);
# run psql from inside the postgres container which has it natively.
EDEN_READONLY_STORE_URL="$(grep -E '^EDEN_READONLY_STORE_URL=' "$ENV_FILE" | cut -d= -f2-)"
test -n "$EDEN_READONLY_STORE_URL" || {
    echo "setup-experiment did not write EDEN_READONLY_STORE_URL" >&2
    exit 1
}
# 1. SELECT projected columns succeeds.
docker compose -f compose.yaml -f compose.subprocess.yaml \
    --env-file "$ENV_FILE" \
    exec -T postgres \
    psql "$EDEN_READONLY_STORE_URL" \
    -tAc "SELECT COUNT(*) FROM event" >/dev/null || {
    echo "readonly SELECT on event failed" >&2
    exit 1
}
# 2. INSERT (in a transaction we'll rollback) must fail with
# permission denied — using a syntactically valid statement so the
# failure mode is the privilege check, not NOT NULL violation.
if docker compose -f compose.yaml -f compose.subprocess.yaml \
        --env-file "$ENV_FILE" \
        exec -T postgres \
        psql "$EDEN_READONLY_STORE_URL" -v ON_ERROR_STOP=1 \
        -c "BEGIN; INSERT INTO idea (idea_id, state, data) VALUES ('rosmoke', 'drafting', '{}'); ROLLBACK;" \
        2>/dev/null; then
    echo "readonly role accepted INSERT — privilege check is broken" >&2
    exit 1
fi
# 3. Direct credential_hash reference must fail.
if docker compose -f compose.yaml -f compose.subprocess.yaml \
        --env-file "$ENV_FILE" \
        exec -T postgres \
        psql "$EDEN_READONLY_STORE_URL" -v ON_ERROR_STOP=1 \
        -c "SELECT credential_hash FROM worker LIMIT 1" \
        2>/dev/null; then
    echo "readonly role can SELECT credential_hash — column grant broken" >&2
    exit 1
fi
# 4. SELECT * on worker must fail (parser expands * to include
# credential_hash).
if docker compose -f compose.yaml -f compose.subprocess.yaml \
        --env-file "$ENV_FILE" \
        exec -T postgres \
        psql "$EDEN_READONLY_STORE_URL" -v ON_ERROR_STOP=1 \
        -c "SELECT * FROM worker LIMIT 1" \
        2>/dev/null; then
    echo "readonly role can SELECT * FROM worker — column grant broken" >&2
    exit 1
fi

echo "PASS (subprocess mode)"
