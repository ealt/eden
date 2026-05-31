#!/usr/bin/env bash
set -euo pipefail

# Smoke test for the Phase 12b portable-checkpoint round-trip via the
# Compose stack. Backfills the CHANGELOG-narrated deferral from Phase
# 12b ("Compose smoke for checkpoint — no `compose-smoke-checkpoint`
# job yet; deferred until the deployment-substrate integration lands").
# Issue: https://github.com/ealt/eden/issues/152
#
# Flow:
#   1. setup-experiment + bring up the full stack
#   2. Wait for the orchestrator to exit on quiescence (same posture as
#      smoke.sh) so the experiment has real Store-managed state to
#      export.
#   3. Snapshot the pre-checkpoint wire state (event/variant/idea
#      counts + id sets) for post-import comparison.
#   4. Export via `POST /v0/experiments/<id>/checkpoint` (admin-gated
#      per chapter 7 §13.1). Save the tar archive to a host path.
#   5. `compose down -v` + wipe the substrate bind-mount data root.
#      Recreate the substrate dirs so the receiver-side bind-mounts
#      resolve.
#   6. Bring up ONLY postgres + task-store-server against the SAME
#      .env (so admin token + experiment_id + DB password match). The
#      receiver store is freshly-empty (no setup-experiment re-run,
#      so no reserved-group / initial-admin bootstrap runs against
#      the receiver), which satisfies chapter 10 §9's
#      "store-must-be-empty" precondition.
#   7. Import via `POST /v0/checkpoints/import`.
#   8. Re-read the wire state and assert it matches the pre-checkpoint
#      snapshot (chapter 10 §9 round-trip semantics).
#
# Mirrors smoke.sh's preflight / cleanup / project-name conventions.

# Tooling preflight — fail fast with a clear message if a required
# binary is missing.
for tool in docker jq curl python3; do
    command -v "$tool" >/dev/null || {
        echo "smoke-checkpoint.sh requires '$tool' on PATH" >&2
        exit 2
    }
done
docker compose version >/dev/null || {
    echo "smoke-checkpoint.sh requires the 'docker compose' v2 plugin" >&2
    exit 2
}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${COMPOSE_DIR}/../.." && pwd)"

cd "$COMPOSE_DIR"

ENV_FILE="$(mktemp)"
SMOKE_DATA_ROOT="$(mktemp -d -t eden-smoke-ckpt-XXXXXX)"
CHECKPOINT_TAR="$(mktemp -t eden-checkpoint-XXXXXX.tar)"
IMPORT_BODY="$(mktemp -t eden-import-resp-XXXXXX.json)"
EXPERIMENT_ID="smoke-checkpoint-exp"

cleanup() {
    local rc=$?
    docker compose -f compose.yaml --env-file "$ENV_FILE" down -v >/dev/null 2>&1 || true
    rm -f "$ENV_FILE"
    rm -f "$CHECKPOINT_TAR"
    rm -f "$IMPORT_BODY"
    rm -f "${COMPOSE_DIR}/experiment-config.yaml"
    # Same posture as smoke.sh: substrate bind-mount subdirs contain
    # files owned by container uids the host doesn't match. Delete
    # via a sibling container running as root, then rmdir from the
    # host. Defense-in-depth empty/`/` guard.
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

echo "=== Phase 1: bring up full stack, run experiment to quiescence ==="

echo "--- running setup-experiment ---"
bash "${REPO_ROOT}/reference/scripts/setup-experiment/setup-experiment.sh" \
    "${REPO_ROOT}/tests/fixtures/experiment/.eden/config.yaml" \
    --experiment-id "$EXPERIMENT_ID" \
    --env-file "$ENV_FILE" \
    --data-root "$SMOKE_DATA_ROOT"

rm -f "${ENV_FILE}.bak"

# Issue #157: max_quiescent_iterations is now an experiment-config field
# (the single-experiment orchestrator no longer reads the retired
# EDEN_MAX_QUIESCENT_ITERATIONS env var). 30 reproduces the retired
# compose-level default. The fixture config carries ideation_policy
# already; append the quiescence budget to the copied config.
EXPERIMENT_CONFIG="${REPO_ROOT}/reference/compose/experiment-config.yaml"
cat >>"$EXPERIMENT_CONFIG" <<'YAML'
max_quiescent_iterations: 30
YAML

EDEN_ADMIN_TOKEN="$(grep -E '^EDEN_ADMIN_TOKEN=' "$ENV_FILE" | cut -d= -f2-)"
test -n "$EDEN_ADMIN_TOKEN"
POSTGRES_USER="$(grep -E '^POSTGRES_USER=' "$ENV_FILE" | cut -d= -f2-)"
POSTGRES_DB="$(grep -E '^POSTGRES_DB=' "$ENV_FILE" | cut -d= -f2-)"

echo "--- bringing up the full stack ---"
docker compose -f compose.yaml --env-file "$ENV_FILE" up -d --wait --wait-timeout 240

echo "--- waiting for orchestrator to exit on quiescence ---"
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
    docker compose -f compose.yaml --env-file "$ENV_FILE" logs --tail 30 orchestrator >&2
    exit 1
}
test "$(docker inspect --format '{{.State.ExitCode}}' eden-orchestrator)" = "0" || {
    echo "orchestrator exited non-zero" >&2
    docker compose -f compose.yaml --env-file "$ENV_FILE" logs --tail 30 orchestrator >&2
    exit 1
}

# Small helper to call task-store-server via the host port. The
# task-store-server publishes port 8080 to the host (compose.yaml's
# ports: block); using the host port instead of `compose exec` keeps
# the curl invocation symmetrical between sender and receiver phases
# without depending on whether worker-host containers are running.
TASK_STORE_PORT="$(grep -E '^TASK_STORE_HOST_PORT=' "$ENV_FILE" | cut -d= -f2- || echo 8080)"
test -n "$TASK_STORE_PORT" || TASK_STORE_PORT=8080
TS_BASE="http://localhost:${TASK_STORE_PORT}"

ts_get() {
    local path="$1"
    curl -fsS \
        -H "Authorization: Bearer admin:${EDEN_ADMIN_TOKEN}" \
        -H "X-Eden-Experiment-Id: ${EXPERIMENT_ID}" \
        "${TS_BASE}${path}"
}

echo "=== Phase 2: snapshot pre-checkpoint state ==="

PRE_EVENTS_JSON="$(ts_get "/v0/experiments/${EXPERIMENT_ID}/events")"
PRE_VARIANTS_JSON="$(ts_get "/v0/experiments/${EXPERIMENT_ID}/variants")"
PRE_IDEAS_JSON="$(ts_get "/v0/experiments/${EXPERIMENT_ID}/ideas")"
PRE_TASKS_JSON="$(ts_get "/v0/experiments/${EXPERIMENT_ID}/tasks")"

# `/events` returns `{events: [...], cursor: N}`; `/variants`,
# `/ideas`, `/tasks` return bare arrays. The `as_list` filter handles
# both shapes.
PRE_EVENT_COUNT="$(echo "$PRE_EVENTS_JSON" | jq '.events | length')"
PRE_VARIANT_COUNT="$(echo "$PRE_VARIANTS_JSON" | jq 'length')"
PRE_IDEA_COUNT="$(echo "$PRE_IDEAS_JSON" | jq 'length')"
PRE_TASK_COUNT="$(echo "$PRE_TASKS_JSON" | jq 'length')"
PRE_VARIANT_INTEGRATED="$(echo "$PRE_EVENTS_JSON" \
    | jq '[.events[] | select(.type == "variant.integrated")] | length')"
PRE_TASK_COMPLETED="$(echo "$PRE_EVENTS_JSON" \
    | jq '[.events[] | select(.type == "task.completed")] | length')"

# Stable sorted id sets so post-import equality is order-insensitive
# (the wire surface doesn't promise insertion order across backends).
PRE_VARIANT_IDS="$(echo "$PRE_VARIANTS_JSON" | jq -c '[.[] | .variant_id] | sort')"
PRE_IDEA_IDS="$(echo "$PRE_IDEAS_JSON" | jq -c '[.[] | .idea_id] | sort')"
PRE_TASK_IDS="$(echo "$PRE_TASKS_JSON" | jq -c '[.[] | .task_id] | sort')"
PRE_EVENT_IDS="$(echo "$PRE_EVENTS_JSON" | jq -c '[.events[] | .event_id] | sort')"

echo "pre-snapshot: events=${PRE_EVENT_COUNT} variants=${PRE_VARIANT_COUNT} ideas=${PRE_IDEA_COUNT} tasks=${PRE_TASK_COUNT}"

# Sanity: the same minimum-progress assertions smoke.sh makes. If the
# exporter is fed an under-baked experiment, every downstream
# assertion would still pass trivially (3 == 3 etc.).
test "$PRE_VARIANT_INTEGRATED" -ge 3 || {
    echo "pre-checkpoint: expected >= 3 variant.integrated; got $PRE_VARIANT_INTEGRATED" >&2
    exit 1
}
test "$PRE_TASK_COMPLETED" -ge 9 || {
    echo "pre-checkpoint: expected >= 9 task.completed; got $PRE_TASK_COMPLETED" >&2
    exit 1
}

echo "=== Phase 3: export checkpoint ==="

# Admin-gated per chapter 7 §13.1: must use the literal `admin`
# principal, not an admins-group member. `-f` is intentionally
# omitted on this POST so we can pull the HTTP status code on
# failure; we check the status explicitly.
HTTP_CODE="$(curl -sS -o "$CHECKPOINT_TAR" -w '%{http_code}' \
    -X POST \
    -H "Authorization: Bearer admin:${EDEN_ADMIN_TOKEN}" \
    -H "X-Eden-Experiment-Id: ${EXPERIMENT_ID}" \
    "${TS_BASE}/v0/experiments/${EXPERIMENT_ID}/checkpoint")"
test "$HTTP_CODE" = "200" || {
    echo "checkpoint export failed: HTTP $HTTP_CODE" >&2
    echo "--- response body ---" >&2
    cat "$CHECKPOINT_TAR" >&2
    exit 1
}

# A zero-byte tarball would let downstream import return a non-trivial
# error or worse, an empty-merge no-op. Match the assert_snapshot_nonempty
# discipline from eden-experiment's checkpoint subcommand.
CKPT_BYTES="$(wc -c < "$CHECKPOINT_TAR" | tr -d '[:space:]')"
test "$CKPT_BYTES" -ge 1024 || {
    echo "checkpoint tar suspiciously small (${CKPT_BYTES} bytes); refusing to import" >&2
    exit 1
}
echo "checkpoint exported: ${CKPT_BYTES} bytes"

# Verify the archive parses as tar and carries the expected manifest.
tar -tf "$CHECKPOINT_TAR" >/dev/null
tar -tf "$CHECKPOINT_TAR" | grep -q 'manifest\.json$' || {
    echo "checkpoint tar missing manifest.json" >&2
    tar -tf "$CHECKPOINT_TAR" >&2
    exit 1
}

echo "=== Phase 4: tear down sender, wipe data root, bring up receiver ==="

echo "--- compose down -v ---"
docker compose -f compose.yaml --env-file "$ENV_FILE" down -v

echo "--- wiping substrate dirs under ${SMOKE_DATA_ROOT} ---"
# Same uid-mismatch dance as cleanup() — files inside the substrate
# subdirs are written by container uids (postgres=70, forgejo=1000)
# that the host runner doesn't match. Delete from inside a sibling
# container, then re-create the subdir layout so the receiver-side
# bind-mounts resolve.
if [[ -n "$SMOKE_DATA_ROOT" && "$SMOKE_DATA_ROOT" != "/" && -d "$SMOKE_DATA_ROOT" ]]; then
    docker run --rm -v "$SMOKE_DATA_ROOT:/cleanup" alpine:3.20 \
        sh -c 'find /cleanup -mindepth 1 -delete'
fi
# Recreate every substrate subdir setup-experiment originally
# materialized — even ones the receiver phase won't bind-mount in
# anger (forgejo, etc.) — because their absence would 4xx any later
# compose-config validation and silently shift symptoms.
for sub in postgres forgejo forgejo-etc orchestrator-repo web-ui-repo executor-repo \
           evaluator-repo artifacts \
           credentials/orchestrator credentials/ideator \
           credentials/executor credentials/evaluator credentials/web-ui; do
    mkdir -p "${SMOKE_DATA_ROOT}/${sub}"
    chmod 0777 "${SMOKE_DATA_ROOT}/${sub}"
done

echo "--- bringing up postgres + task-store-server (no orchestrator / workers / forgejo) ---"
# The receiver intentionally brings up the MINIMUM service set
# (postgres + task-store-server) so:
#   - No setup-experiment re-run → no reserved-group / initial-admin
#     bootstrap rows → store remains empty → chapter 10 §9 import
#     precondition holds.
#   - No orchestrator → no dispatch loop racing against the import.
#   - No worker hosts → no claim attempts against the pre-import
#     store.
#   - No forgejo → import doesn't push anything to a git remote;
#     chapter 10 §12 bundle cross-reference validation runs against
#     the bundle bytes inside the archive.
docker compose -f compose.yaml --env-file "$ENV_FILE" up -d --wait --wait-timeout 240 \
    postgres task-store-server

# Sanity: confirm the receiver store is in fact empty before importing.
# A non-empty store would surface as ExperimentIdConflict from
# `import_checkpoint`; we'd rather fail with a clear message here.
RECEIVER_EVENTS_BEFORE="$(ts_get "/v0/experiments/${EXPERIMENT_ID}/events" \
    | jq '.events | length')"
test "$RECEIVER_EVENTS_BEFORE" = "0" || {
    echo "receiver store is not empty before import: $RECEIVER_EVENTS_BEFORE events" >&2
    exit 1
}

echo "=== Phase 5: import checkpoint ==="

HTTP_CODE="$(curl -sS -o "$IMPORT_BODY" -w '%{http_code}' \
    -X POST \
    -H "Authorization: Bearer admin:${EDEN_ADMIN_TOKEN}" \
    -H "Content-Type: application/x-eden-checkpoint+tar" \
    --data-binary "@${CHECKPOINT_TAR}" \
    "${TS_BASE}/v0/checkpoints/import")"
# Chapter 10 §10 + wire chapter 7 §14.2 mandate 201 Created on a
# successful import.
test "$HTTP_CODE" = "201" || {
    echo "checkpoint import failed: HTTP $HTTP_CODE" >&2
    echo "--- response body ---" >&2
    cat "$IMPORT_BODY" >&2
    exit 1
}

# The import response carries the warnings array (per chapter 7 §14.2
# + issue #150). We don't assert a particular warning is or isn't
# present — the credentials-dir flag is intentionally unset on this
# compose deployment (see issue #150 changelog), so we expect either
# the "tokens NOT persisted" warning OR (if the deployment grows a
# credentials-dir wire-up in the future) a "persisted" warning. Both
# are valid round-trip outcomes; surface them in the log so a future
# debugger can see what the receiver actually did.
echo "--- import response ---"
jq . < "$IMPORT_BODY"

echo "=== Phase 6: assert restored state matches pre-checkpoint snapshot ==="

POST_EVENTS_JSON="$(ts_get "/v0/experiments/${EXPERIMENT_ID}/events")"
POST_VARIANTS_JSON="$(ts_get "/v0/experiments/${EXPERIMENT_ID}/variants")"
POST_IDEAS_JSON="$(ts_get "/v0/experiments/${EXPERIMENT_ID}/ideas")"
POST_TASKS_JSON="$(ts_get "/v0/experiments/${EXPERIMENT_ID}/tasks")"

POST_EVENT_COUNT="$(echo "$POST_EVENTS_JSON" | jq '.events | length')"
POST_VARIANT_COUNT="$(echo "$POST_VARIANTS_JSON" | jq 'length')"
POST_IDEA_COUNT="$(echo "$POST_IDEAS_JSON" | jq 'length')"
POST_TASK_COUNT="$(echo "$POST_TASKS_JSON" | jq 'length')"

POST_VARIANT_IDS="$(echo "$POST_VARIANTS_JSON" | jq -c '[.[] | .variant_id] | sort')"
POST_IDEA_IDS="$(echo "$POST_IDEAS_JSON" | jq -c '[.[] | .idea_id] | sort')"
POST_TASK_IDS="$(echo "$POST_TASKS_JSON" | jq -c '[.[] | .task_id] | sort')"
POST_EVENT_IDS="$(echo "$POST_EVENTS_JSON" | jq -c '[.events[] | .event_id] | sort')"

echo "post-snapshot: events=${POST_EVENT_COUNT} variants=${POST_VARIANT_COUNT} ideas=${POST_IDEA_COUNT} tasks=${POST_TASK_COUNT}"

assert_eq() {
    local label="$1" pre="$2" post="$3"
    if [[ "$pre" != "$post" ]]; then
        echo "post-import mismatch [$label]:" >&2
        echo "  pre : $pre" >&2
        echo "  post: $post" >&2
        exit 1
    fi
}

assert_eq "event count"    "$PRE_EVENT_COUNT"    "$POST_EVENT_COUNT"
assert_eq "variant count"  "$PRE_VARIANT_COUNT"  "$POST_VARIANT_COUNT"
assert_eq "idea count"     "$PRE_IDEA_COUNT"     "$POST_IDEA_COUNT"
assert_eq "task count"     "$PRE_TASK_COUNT"     "$POST_TASK_COUNT"
assert_eq "variant ids"    "$PRE_VARIANT_IDS"    "$POST_VARIANT_IDS"
assert_eq "idea ids"       "$PRE_IDEA_IDS"       "$POST_IDEA_IDS"
assert_eq "task ids"       "$PRE_TASK_IDS"       "$POST_TASK_IDS"
assert_eq "event ids"      "$PRE_EVENT_IDS"      "$POST_EVENT_IDS"

# Chapter 10 §10: the receiver-side experiment row carries an
# `imported_from` field with the source's `checkpoint_exported_at`
# + `checkpoint_format_version`. Its presence is the load-bearing
# round-trip signal — the data identity matched, AND the receiver
# correctly stamped the import provenance.
POST_EXPERIMENT_JSON="$(ts_get "/v0/experiments/${EXPERIMENT_ID}")"
echo "$POST_EXPERIMENT_JSON" | jq -e '.imported_from.checkpoint_exported_at' >/dev/null || {
    echo "post-import experiment row missing imported_from.checkpoint_exported_at" >&2
    echo "$POST_EXPERIMENT_JSON" >&2
    exit 1
}

echo "PASS"
