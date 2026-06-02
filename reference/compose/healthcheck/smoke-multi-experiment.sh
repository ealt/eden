#!/usr/bin/env bash
set -euo pipefail

# #147 — control-plane + lease-handoff smoke for the EDEN reference
# Compose stack.
#
# RE-SCOPED (see docs/plans/issue-147-compose-smoke-multi-experiment.md
# §0): the reference impl cannot host more than one experiment per
# deployment (single-experiment task-store-server), so the original
# "two experiments + cross-experiment isolation" smoke is deferred to
# issue #254. This smoke exercises the genuinely-new chapter-11
# substrate surface that IS shippable today:
#
#   - The control-plane-server running as a first-class Compose service
#     (Postgres-backed; /healthz reachable).
#   - ONE registered experiment driven through the control plane.
#   - TWO orchestrator replicas (orchestrator, orchestrator-2) in
#     lease-driven mode contending for that experiment's single lease.
#   - The lease-singleton invariant: at most one replica holds the
#     lease at any observed instant.
#   - The lease-handoff chaos drill: kill the lease holder; the standby
#     replica acquires the lease and the experiment still makes
#     progress (>= 2 variant.integrated).
#   - State-sync convergence: after an operator-driven termination, the
#     control-plane's last_known_state for the experiment converges to
#     "terminated".
#
# NOTE on termination: this smoke uses the OPERATOR-DRIVEN
# `terminate_experiment` wire op (an `admins` worker), NOT
# `dispatch_mode.termination = "auto"`. The orchestrator's auto-
# termination decision currently 403s under wire auth (terminate is
# admins-gated, the orchestrator is in `orchestrators`) — a pre-existing
# spec inter-chapter drift surfaced by this smoke and tracked in #256.
# Operator-driven termination is the supported path per 03-roles.md
# §6.2 ("Termination MAY occur via the operator-driven wire op
# regardless of dispatch_mode").
#
# bash 3.2 compatible (no mapfile / readarray / associative arrays).

for tool in docker jq curl python3; do
    command -v "$tool" >/dev/null || {
        echo "smoke-multi-experiment.sh requires '$tool' on PATH" >&2
        exit 2
    }
done
docker compose version >/dev/null || {
    echo "smoke-multi-experiment.sh requires the 'docker compose' v2 plugin" >&2
    exit 2
}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${COMPOSE_DIR}/../.." && pwd)"

cd "$COMPOSE_DIR"

ENV_FILE="$(mktemp)"
# Per-run data root so re-runs don't trip the rotate-password trap
# (setup-experiment generates a fresh POSTGRES_PASSWORD each run, but a
# leftover bind-mounted postgres data dir bakes in the old one). See
# AGENTS.md "Compose smoke tests need explicit volume cleanup".
SMOKE_DATA_ROOT="$(mktemp -d -t eden-smoke-multi-XXXXXX)"
EXPERIMENT_ID="smoke-multi-exp"
COMPOSE_FILES=(-f compose.yaml -f compose.multi-experiment.yaml)

cleanup() {
    local rc=$?
    if [[ $rc -ne 0 ]]; then
        echo "--- cleanup: dumping compose state for diagnostics ---" >&2
        docker compose "${COMPOSE_FILES[@]}" --env-file "$ENV_FILE" \
            ps -a >&2 2>&1 || true
        docker compose "${COMPOSE_FILES[@]}" --env-file "$ENV_FILE" \
            logs --tail 80 control-plane orchestrator orchestrator-2 >&2 2>&1 || true
    fi
    docker compose "${COMPOSE_FILES[@]}" --env-file "$ENV_FILE" \
        down -v >/dev/null 2>&1 || true
    rm -f "$ENV_FILE"
    rm -f "${COMPOSE_DIR}/experiment-config.yaml"
    # Substrate bind-mount subdirs contain files owned by container
    # uids the host doesn't match; delete via a root sibling container,
    # then rmdir from the host. Defense-in-depth empty/`/` guard.
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

# ---------------------------------------------------------------------
# Phase 1 — provision the single experiment + pin lease-mode env
# ---------------------------------------------------------------------
echo "--- running setup-experiment ---"
bash "${REPO_ROOT}/reference/scripts/setup-experiment/setup-experiment.sh" \
    "${REPO_ROOT}/tests/fixtures/experiment/.eden/config.yaml" \
    --experiment-id "$EXPERIMENT_ID" \
    --env-file "$ENV_FILE" \
    --data-root "$SMOKE_DATA_ROOT"

# Flip the orchestrators + web-ui into chapter-11 lease-driven mode and
# pick a fast lease for a snappy chaos drill. setup-experiment writes
# EDEN_CONTROL_PLANE_URL= (empty); replace it. The lease/state-sync
# knobs are not written by setup-experiment (compose defaults apply),
# so appending them is unambiguous.
sed -i.bak \
    "s|^EDEN_CONTROL_PLANE_URL=.*$|EDEN_CONTROL_PLANE_URL=http://control-plane:8081|" \
    "$ENV_FILE"
rm -f "${ENV_FILE}.bak"
cat >>"$ENV_FILE" <<'EOF'
EDEN_LEASE_DURATION_SECONDS=10
EDEN_STATE_SYNC_INTERVAL_SECONDS=5
EOF

# Bound ideation so the run is finite (mirrors smoke-multi-orchestrator;
# edit the experiment-config YAML, not env, per issue #133). Termination
# is operator-driven below, so a small fixed budget is enough.
EXPERIMENT_CONFIG="${COMPOSE_DIR}/experiment-config.yaml"
cat >>"$EXPERIMENT_CONFIG" <<'YAML'
ideation_policy:
  kind: fixed_total
  total: 3
YAML

EDEN_ADMIN_TOKEN="$(grep -E '^EDEN_ADMIN_TOKEN=' "$ENV_FILE" | cut -d= -f2-)"
test -n "$EDEN_ADMIN_TOKEN"
EXP_BASE="/v0/experiments/${EXPERIMENT_ID}"

# Admin-authenticated wire call against the task-store-server, issued
# from inside its container (no host port-guessing; works whether or
# not the host has curl).
call_ts() {
    local method="$1" path="$2" body="${3:-}"
    local args=(
        -fsS -X "$method"
        -H "Authorization: Bearer admin:${EDEN_ADMIN_TOKEN}"
        -H "X-Eden-Experiment-Id: ${EXPERIMENT_ID}"
        -H "Content-Type: application/json"
    )
    [[ -n "$body" ]] && args+=(-d "$body")
    args+=("http://localhost:8080${path}")
    docker compose "${COMPOSE_FILES[@]}" --env-file "$ENV_FILE" \
        exec -T task-store-server curl "${args[@]}"
}

# Worker-bearer wire call (for the §3.7 admins-gated terminate_experiment
# op, which rejects the literal `admin` bearer).
call_ts_worker() {
    local method="$1" path="$2" bearer="$3" body="${4:-}"
    local args=(
        -fsS -X "$method"
        -H "Authorization: Bearer ${bearer}"
        -H "X-Eden-Experiment-Id: ${EXPERIMENT_ID}"
        -H "Content-Type: application/json"
    )
    [[ -n "$body" ]] && args+=(-d "$body")
    args+=("http://localhost:8080${path}")
    docker compose "${COMPOSE_FILES[@]}" --env-file "$ENV_FILE" \
        exec -T task-store-server curl "${args[@]}"
}

# Admin-authenticated call against the control-plane, from inside its
# container (it listens on 8081).
call_cp() {
    local method="$1" path="$2" body="${3:-}"
    local args=(
        -fsS -X "$method"
        -H "Authorization: Bearer admin:${EDEN_ADMIN_TOKEN}"
        -H "Content-Type: application/json"
    )
    [[ -n "$body" ]] && args+=(-d "$body")
    args+=("http://localhost:8081${path}")
    docker compose "${COMPOSE_FILES[@]}" --env-file "$ENV_FILE" \
        exec -T control-plane curl "${args[@]}"
}

# ---------------------------------------------------------------------
# Phase 2 — bring up the stack with the lease overlay
# ---------------------------------------------------------------------
echo "--- bringing up the full stack with the lease overlay ---"
docker compose "${COMPOSE_FILES[@]}" --env-file "$ENV_FILE" \
    up -d --wait --wait-timeout 300

echo "--- asserting control-plane /healthz ---"
docker compose "${COMPOSE_FILES[@]}" --env-file "$ENV_FILE" \
    exec -T control-plane curl -fsS http://localhost:8081/healthz >/dev/null || {
        echo "control-plane /healthz did not return 200" >&2
        exit 1
    }

# Register the experiment with the control plane so the lease-driven
# orchestrators pick it up (admin-gated; 201 first / 200 idempotent).
echo "--- registering experiment with the control plane ---"
call_cp POST /v0/control/experiments \
    "{\"experiment_id\":\"${EXPERIMENT_ID}\",\"config_uri\":\"file:///etc/eden/experiment-config.yaml\"}" \
    >/dev/null
call_cp GET "/v0/control/experiments/${EXPERIMENT_ID}" \
    | jq -e --arg id "$EXPERIMENT_ID" '.experiment_id == $id' >/dev/null || {
        echo "control-plane registry does not list ${EXPERIMENT_ID}" >&2
        exit 1
    }

# Pre-seed the task-store `orchestrators` group with both replica
# worker_ids. In single-experiment mode the orchestrator self-joins
# this group at startup; the chapter-11 lease-driven path only joins
# the CONTROL-PLANE orchestrators group, NOT the task-store one, so
# without this seeding the lease holder's §3.7-gated dispatch/integrate
# calls would 403. Tracked for a proper in-orchestrator fix under #254
# (multi-experiment hosting). add_to_group is admin-gated + idempotent.
echo "--- seeding task-store orchestrators group with both replicas ---"
for wid in orchestrator orchestrator-2; do
    call_ts POST "${EXP_BASE}/workers" "{\"worker_id\":\"${wid}\"}" >/dev/null
    call_ts POST "${EXP_BASE}/groups/orchestrators/members" \
        "{\"member_id\":\"${wid}\"}" >/dev/null
done

# Return the single lease holder for the experiment (empty if none).
lease_holder() {
    call_cp GET "/v0/control/experiments/${EXPERIMENT_ID}" 2>/dev/null \
        | jq -r '.lease.holder // empty'
}

# Count active leases for $EXPERIMENT_ID held by $1 (0 or 1).
leases_for_holder() {
    call_cp GET "/v0/control/leases?holder=$1" 2>/dev/null \
        | jq --arg id "$EXPERIMENT_ID" \
            '[.leases[]? | select(.experiment_id == $id)] | length'
}

echo "--- waiting for a single lease holder (lease-singleton invariant) ---"
HOLDER=""
deadline=$((SECONDS + 60))
while [[ $SECONDS -lt $deadline ]]; do
    HOLDER="$(lease_holder || true)"
    if [[ "$HOLDER" = "orchestrator" || "$HOLDER" = "orchestrator-2" ]]; then
        break
    fi
    sleep 2
done
case "$HOLDER" in
    orchestrator|orchestrator-2) ;;
    *)
        echo "no replica acquired the lease within 60s (holder='${HOLDER}')" >&2
        exit 1
        ;;
esac
n_total=$(( $(leases_for_holder orchestrator) + $(leases_for_holder orchestrator-2) ))
test "$n_total" -eq 1 || {
    echo "lease-singleton violated: ${n_total} active leases across both replicas" >&2
    exit 1
}
echo "    lease held by: ${HOLDER}"

# ---------------------------------------------------------------------
# Phase 3 — lease-handoff chaos drill
# ---------------------------------------------------------------------
# Kill AND remove the lease holder so the on-failure restart policy has
# nothing to restart; the standby MUST acquire the lease within
# lease_duration*2 + poll slack.
OTHER="orchestrator-2"
[[ "$HOLDER" = "orchestrator-2" ]] && OTHER="orchestrator"
echo "--- chaos: killing lease holder eden-${HOLDER}; expecting ${OTHER} to take over ---"
docker rm -f "eden-${HOLDER}" >/dev/null

deadline=$((SECONDS + 45))
NEW_HOLDER=""
while [[ $SECONDS -lt $deadline ]]; do
    NEW_HOLDER="$(lease_holder || true)"
    if [[ "$NEW_HOLDER" = "$OTHER" ]]; then
        break
    fi
    sleep 2
done
test "$NEW_HOLDER" = "$OTHER" || {
    echo "lease did not hand off to ${OTHER} within 45s (holder='${NEW_HOLDER}')" >&2
    exit 1
}
n_total=$(( $(leases_for_holder orchestrator) + $(leases_for_holder orchestrator-2) ))
test "$n_total" -eq 1 || {
    echo "post-handoff lease-singleton violated: ${n_total} active leases" >&2
    exit 1
}
echo "    lease handed off to: ${NEW_HOLDER}"

# ---------------------------------------------------------------------
# Phase 4 — the surviving replica drives the pipeline to >= 2 integrated
# ---------------------------------------------------------------------
echo "--- waiting for the surviving replica to integrate >= 2 variants ---"
deadline=$((SECONDS + 240))
integrated=0
while [[ $SECONDS -lt $deadline ]]; do
    events="$(call_ts GET "${EXP_BASE}/events" || true)"
    if [[ -n "$events" ]]; then
        integrated="$(echo "$events" \
            | jq '[.events[]? | select(.type == "variant.integrated")] | length')"
        if [[ "${integrated:-0}" -ge 2 ]]; then
            break
        fi
    fi
    sleep 3
done
events="$(call_ts GET "${EXP_BASE}/events")"
integrated="$(echo "$events" \
    | jq '[.events[]? | select(.type == "variant.integrated")] | length')"
exec_completed="$(echo "$events" | jq '[.events[]? | select(
    .type == "task.completed" and (.data.task_id | startswith("execution-"))
  )] | length')"
eval_completed="$(echo "$events" | jq '[.events[]? | select(
    .type == "task.completed" and (.data.task_id | startswith("evaluate-"))
  )] | length')"
for name in integrated exec_completed eval_completed; do
    count="${!name}"
    if (( count < 2 )); then
        echo "expected >= 2 ${name}; got ${count}" >&2
        exit 1
    fi
done
echo "    integrated=${integrated} exec_completed=${exec_completed} eval_completed=${eval_completed}"

# ---------------------------------------------------------------------
# Phase 5 — operator-driven termination + state-sync convergence
# ---------------------------------------------------------------------
# terminate_experiment is admins-group-gated and rejects the literal
# admin bearer, so register a throwaway worker, add it to `admins`, and
# call terminate with its worker bearer. (The orchestrator's OWN auto-
# termination decision can't do this under wire auth — see #256; the
# operator-driven op is the supported path.)
echo "--- operator-driven terminate (admins worker) ---"
TERM_ADMIN="smoke-term-admin"
REG_JSON="$(call_ts POST "${EXP_BASE}/workers" "{\"worker_id\":\"${TERM_ADMIN}\"}")"
TERM_TOKEN="$(echo "$REG_JSON" | jq -r '.registration_token // empty')"
if [[ -z "$TERM_TOKEN" ]]; then
    REG_JSON="$(call_ts POST "${EXP_BASE}/workers/${TERM_ADMIN}/reissue-credential" "")"
    TERM_TOKEN="$(echo "$REG_JSON" | jq -r '.registration_token // empty')"
fi
test -n "$TERM_TOKEN"
call_ts POST "${EXP_BASE}/groups/admins/members" \
    "{\"member_id\":\"${TERM_ADMIN}\"}" >/dev/null
call_ts_worker POST "${EXP_BASE}/terminate" \
    "${TERM_ADMIN}:${TERM_TOKEN}" '{"reason":"smoke-multi-experiment"}' >/dev/null

echo "--- asserting experiment.terminated ---"
deadline=$((SECONDS + 60))
terminated=0
while [[ $SECONDS -lt $deadline ]]; do
    events="$(call_ts GET "${EXP_BASE}/events" || true)"
    if [[ -n "$events" ]]; then
        n="$(echo "$events" \
            | jq '[.events[]? | select(.type == "experiment.terminated")] | length')"
        if [[ "${n:-0}" -ge 1 ]]; then
            terminated=1
            break
        fi
    fi
    sleep 2
done
test "$terminated" -eq 1 || {
    echo "experiment did not reach experiment.terminated within 60s" >&2
    exit 1
}

echo "--- asserting control-plane state-sync convergence (last_known_state) ---"
deadline=$((SECONDS + 30))
last_state=""
while [[ $SECONDS -lt $deadline ]]; do
    last_state="$(call_cp GET "/v0/control/experiments/${EXPERIMENT_ID}" 2>/dev/null \
        | jq -r '.last_known_state // empty')"
    if [[ "$last_state" = "terminated" ]]; then
        break
    fi
    sleep 2
done
test "$last_state" = "terminated" || {
    echo "control-plane last_known_state did not converge to terminated (got '${last_state}')" >&2
    exit 1
}

echo "PASS"
