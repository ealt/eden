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
# #128: the experiment id is now an opaque, system-minted `exp_*`
# (^exp_[0-9a-hjkmnp-tv-z]{26}$) — a typed mnemonic like the old
# "smoke-exp" no longer satisfies the wire grammar and the
# task-store-server would reject it at first use. Mint one with the
# same Crockford-base32 ULID one-liner setup-experiment uses and pass
# it through `--experiment-id`; setup writes it back to `.env` as
# EDEN_EXPERIMENT_ID and every wire URL below uses it.
EXPERIMENT_ID="$(python3 - <<'PY'
import secrets, time
alphabet = "0123456789abcdefghjkmnpqrstvwxyz"
value = ((int(time.time() * 1000) & ((1 << 48) - 1)) << 80) | secrets.randbits(80)
print("exp_" + "".join(alphabet[(value >> (5 * i)) & 31] for i in range(26))[::-1])
PY
)"

cleanup() {
    local rc=$?
    docker compose -f compose.yaml --env-file "$ENV_FILE" down -v >/dev/null 2>&1 || true
    rm -f "$ENV_FILE"
    rm -f "${COMPOSE_DIR}/experiment-config.yaml"
    # Phase 12a-1g hotfix: substrate bind-mount subdirs (postgres,
    # forgejo, *-repo) contain files written by containers as uids the
    # host runner doesn't match (postgres=70, forgejo/eden=1000), inside
    # subdirectories the containers created with the container's
    # umask (mode 0755 — NOT world-writable). The host's `rm -rf` then
    # fails with EACCES on every file under those subdirs. Delete
    # via a sibling container running as root, where uid mismatches
    # don't matter; then rmdir the now-empty bind-mount root from the
    # host side. The `|| true` keeps a cleanup failure from masking
    # the script's actual exit code.
    # Defense-in-depth: explicit empty / "/" guard. `mktemp -d` on a
    # working system never returns either, but bind-mounting `/` into
    # a container that then runs `find -mindepth 1 -delete` would
    # delete the host filesystem. The cost of the check is one line.
    if [[ -n "${SMOKE_DATA_ROOT:-}" \
        && "$SMOKE_DATA_ROOT" != "/" \
        && -d "$SMOKE_DATA_ROOT" ]]; then
        docker run --rm -v "$SMOKE_DATA_ROOT:/cleanup" alpine:3.20 \
            sh -c 'find /cleanup -mindepth 1 -delete' >/dev/null 2>&1 || true
    fi
    # `|| true` so a cleanup-side failure can't mask the script's
    # real exit code (e.g., if the helper container above never ran
    # because the daemon was unavailable, the host rm would still
    # fail with EACCES on container-owned subdirs).
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

# Issue #133: the default ideation policy maintains a pending depth
# of 3 but is unbounded — without a max_total cap the orchestrator
# would top up the queue every iteration and never quiesce. Append
# a `fixed_total` policy to the copied experiment config so the loop
# quiesces after exactly 3 variants land.
# Issue #157: max_quiescent_iterations is now an experiment-config field
# (the single-experiment orchestrator no longer reads the
# EDEN_MAX_QUIESCENT_ITERATIONS env var). 30 reproduces the retired
# compose-level default — 30 iterations × 1s poll = 30s of stall tolerance.
EXPERIMENT_CONFIG="${REPO_ROOT}/reference/compose/experiment-config.yaml"
cat >>"$EXPERIMENT_CONFIG" <<'YAML'
ideation_policy:
  kind: fixed_total
  total: 3
max_quiescent_iterations: 30
YAML

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

echo "--- asserting seeded ref published to forgejo ---"
# Phase 10d follow-up B: the seed lives on Forgejo, not on a shared
# bare-repo volume. Probe via `git ls-remote` against the forgejo
# container (Forgejo was brought up by setup-experiment).
FORGEJO_REMOTE_PASSWORD="$(grep -E '^FORGEJO_REMOTE_PASSWORD=' "$ENV_FILE" | cut -d= -f2-)"
test -n "$FORGEJO_REMOTE_PASSWORD"
EDEN_EXPERIMENT_ID="$(grep -E '^EDEN_EXPERIMENT_ID=' "$ENV_FILE" | cut -d= -f2-)"
test -n "$EDEN_EXPERIMENT_ID"
SEEDED_SHA="$(
    docker compose -f compose.yaml --env-file "$ENV_FILE" \
        run --rm --no-deps \
        --entrypoint sh \
        eden-repo-init \
        -c "git ls-remote http://eden:${FORGEJO_REMOTE_PASSWORD}@forgejo:3000/eden/${EDEN_EXPERIMENT_ID}.git refs/heads/main | awk '{print \$1}'"
)"
SEEDED_SHA="$(echo "$SEEDED_SHA" | tr -d '[:space:]')"
test "$SEEDED_SHA" = "$EDEN_BASE_COMMIT_SHA" || {
    echo "forgejo seed mismatch: $SEEDED_SHA != $EDEN_BASE_COMMIT_SHA" >&2
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
for sub in postgres forgejo orchestrator-repo web-ui-repo executor-repo \
           evaluator-repo task-store-repo artifacts \
           credentials/orchestrator credentials/ideator \
           credentials/executor credentials/evaluator credentials/web-ui \
           logs/task-store-server logs/orchestrator logs/ideator-host \
           logs/executor-host logs/evaluator-host logs/web-ui; do
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

echo "--- asserting Forgejo API responds ---"
FORGEJO_PORT="$(grep -E '^FORGEJO_HOST_PORT=' "$ENV_FILE" | cut -d= -f2-)"
curl -fsS "http://localhost:${FORGEJO_PORT}/api/v1/version" | jq -e '.version' >/dev/null

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
# After wave-7 setup-experiment runs the reserved-group bootstrap, the
# initial admin worker (EDEN_ADMINS_INITIAL_MEMBER, default
# "operator") is in the registry too, so we now expect >= 5 workers:
# orchestrator + ideator-1 + executor-1 + evaluator-1 + initial admin.
# (web-ui-1 is also registered if the web-ui container has come up.)
test "$REGISTERED_WORKERS" -ge 5 || {
    echo "expected >= 5 registered workers (orchestrator + worker hosts + initial admin); got $REGISTERED_WORKERS" >&2
    echo "workers response: $WORKERS_JSON" >&2
    exit 1
}

# 12a-2 §3.5 / §5.7: the `admins` and `orchestrators` reserved groups
# MUST exist after setup-experiment runs the bootstrap. `admins`
# carries the initial admin member; `orchestrators` carries the
# auto-orchestrator (added by its own startup helper).
echo "--- asserting reserved groups + initial admin ---"
# #128: reserved groups (admins / orchestrators) are now addressed by
# their OPAQUE `grp_*` id, not by name-in-URL. setup-experiment minted
# both groups and wrote their ids to `.env`; read them back. Members
# are opaque `wkr_*` ids, so assert membership by the opaque worker ids
# setup also persisted (EDEN_ADMINS_INITIAL_MEMBER / the orchestrator's
# minted worker id), NOT by literal role labels.
ADMINS_INITIAL_MEMBER="$(grep -E '^EDEN_ADMINS_INITIAL_MEMBER=' "$ENV_FILE" | cut -d= -f2-)"
ORCH_WORKER_ID="$(grep -E '^EDEN_ORCHESTRATOR_WORKER_ID=' "$ENV_FILE" | cut -d= -f2-)"
ADMINS_GROUP_ID="$(grep -E '^EDEN_ADMINS_GROUP_ID=' "$ENV_FILE" | cut -d= -f2-)"
ORCHESTRATORS_GROUP_ID="$(grep -E '^EDEN_ORCHESTRATORS_GROUP_ID=' "$ENV_FILE" | cut -d= -f2-)"
test -n "$ADMINS_INITIAL_MEMBER"
test -n "$ORCH_WORKER_ID"
test -n "$ADMINS_GROUP_ID"
test -n "$ORCHESTRATORS_GROUP_ID"

ADMINS_JSON="$(
    docker compose -f compose.yaml --env-file "$ENV_FILE" \
        exec -T task-store-server \
        curl -fsS \
            -H "Authorization: Bearer admin:${EDEN_ADMIN_TOKEN}" \
            -H "X-Eden-Experiment-Id: ${EXPERIMENT_ID}" \
            "http://localhost:8080/v0/experiments/${EXPERIMENT_ID}/groups/${ADMINS_GROUP_ID}"
)"
echo "$ADMINS_JSON" \
    | jq -e --arg m "$ADMINS_INITIAL_MEMBER" '.members | index($m)' >/dev/null \
    || {
        echo "admins group (${ADMINS_GROUP_ID}) missing initial admin '${ADMINS_INITIAL_MEMBER}': $ADMINS_JSON" >&2
        exit 1
    }

ORCH_JSON="$(
    docker compose -f compose.yaml --env-file "$ENV_FILE" \
        exec -T task-store-server \
        curl -fsS \
            -H "Authorization: Bearer admin:${EDEN_ADMIN_TOKEN}" \
            -H "X-Eden-Experiment-Id: ${EXPERIMENT_ID}" \
            "http://localhost:8080/v0/experiments/${EXPERIMENT_ID}/groups/${ORCHESTRATORS_GROUP_ID}"
)"
echo "$ORCH_JSON" \
    | jq -e --arg w "$ORCH_WORKER_ID" '.members | index($w)' >/dev/null \
    || {
        echo "orchestrators group (${ORCHESTRATORS_GROUP_ID}) missing auto-orchestrator '${ORCH_WORKER_ID}': $ORCH_JSON" >&2
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

echo "--- asserting variant/* refs published to forgejo ---"
# Phase 10d follow-up B §D.6: variant/* refs must be visible on the
# Forgejo remote after integration. Each `variant.integrated` event
# corresponds to a published ref.
VARIANT_REMOTE_REFS="$(
    docker compose -f compose.yaml --env-file "$ENV_FILE" \
        run --rm --no-deps \
        --entrypoint sh \
        eden-repo-init \
        -c "git ls-remote http://eden:${FORGEJO_REMOTE_PASSWORD}@forgejo:3000/eden/${EDEN_EXPERIMENT_ID}.git 'refs/heads/variant/*' | wc -l" \
        | tr -d '[:space:]'
)"
test "$VARIANT_REMOTE_REFS" -ge 3 || {
    echo "expected >= 3 variant/* refs on forgejo; got $VARIANT_REMOTE_REFS" >&2
    exit 1
}

echo "--- asserting per-service log JSONL files were written ---"
# Issue #109: every long-running service writes /var/lib/eden/logs/
# <service>.jsonl alongside stdout. The bind-mount surfaces those
# under ${EDEN_EXPERIMENT_DATA_ROOT}/logs/<service>/ on the host.
# Each must exist and be non-empty after the full smoke run.
for svc in task-store-server orchestrator ideator-host \
           executor-host evaluator-host web-ui; do
    log_file="${SMOKE_DATA_ROOT}/logs/${svc}/${svc}.jsonl"
    test -s "$log_file" || {
        echo "expected non-empty ${log_file}; got:" >&2
        ls -la "${SMOKE_DATA_ROOT}/logs/${svc}/" >&2 || true
        exit 1
    }
    # Sanity: file should parse as JSON-lines (at least the first
    # line). `jq -e` exits non-zero on parse failure.
    head -n1 "$log_file" | jq -e . >/dev/null || {
        echo "first line of ${log_file} is not valid JSON" >&2
        head -n1 "$log_file" >&2
        exit 1
    }
done

echo "PASS"
