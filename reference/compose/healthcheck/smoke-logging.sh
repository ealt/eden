#!/usr/bin/env bash
set -euo pipefail

# Smoke test for the issue-#110 log-search overlay (Loki + Alloy +
# Grafana). Mirrors smoke-checkpoint.sh's preflight / mktemp data-root /
# trap-cleanup / sibling-container root-owned-file-delete shape.
#
# Flow:
#   1. setup-experiment + append a fixed_total ideation policy (so the
#      orchestrator quiesces) — same as smoke.sh.
#   2. Static merge gate for the privileged infra overlay
#      (compose.logging-infra.yaml) — proves the three-file merge
#      resolves and the EDEN_LOGGING_DOCKER_GID :? guard is wired. Runs
#      even where no docker socket is reachable (a dummy gid is fine for
#      `config`, which starts nothing).
#   3. Bring up base + subprocess + logging overlays (subprocess mode so
#      real per-task log volume exists), --wait.
#   4. Wait until EDEN log lines exist, then assert:
#        - loki /ready
#        - grafana /api/health == ok
#        - the Loki datasource is provisioned (NOT just /api/health,
#          which stays ok even when provisioning silently fails)
#        - the EDEN-explore dashboard is provisioned
#        - a LogQL query {service="orchestrator"} returns >= 1 line
#          (this is also the label-regression backstop for the implicit
#          logging.py field-name contract — see alloy-config.alloy)
#   5. When a docker socket is reachable: layer compose.logging-infra.yaml
#      (probing the real socket gid from inside a container) and assert a
#      postgres stdout line reaches Loki. No-ops cleanly where the socket
#      isn't available.
#
# All assertions reach Loki/Grafana by exec-ing curl THROUGH the grafana
# container (which ships curl and can reach loki:3100 on the compose
# network), so the smoke depends on no host ports — loki + alloy are
# internal-only and grafana's host port would otherwise risk a 3000
# collision on a developer laptop.
#
# bash 3.2-safe (macOS default): no mapfile / declare -A.

# Tooling preflight.
for tool in docker jq python3; do
    command -v "$tool" >/dev/null || {
        echo "smoke-logging.sh requires '$tool' on PATH" >&2
        exit 2
    }
done
docker compose version >/dev/null || {
    echo "smoke-logging.sh requires the 'docker compose' v2 plugin" >&2
    exit 2
}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${COMPOSE_DIR}/../.." && pwd)"

cd "$COMPOSE_DIR"

ENV_FILE="$(mktemp)"
SMOKE_DATA_ROOT="$(mktemp -d -t eden-smoke-logging-XXXXXX)"
EXPERIMENT_ID="smoke-logging-exp"

# The overlay set is reused across up / down / exec; keep it in one
# place. The infra overlay is layered later (conditionally), so the
# teardown set is the widest one we ever bring up.
BASE_OVERLAYS=(-f compose.yaml -f compose.subprocess.yaml -f compose.logging.yaml)
INFRA_OVERLAYS=(-f compose.yaml -f compose.subprocess.yaml -f compose.logging.yaml -f compose.logging-infra.yaml)
# Default teardown uses the base set; bumped to INFRA if we bring it up.
DOWN_OVERLAYS=("${BASE_OVERLAYS[@]}")

cleanup() {
    local rc=$?
    docker compose "${DOWN_OVERLAYS[@]}" --env-file "$ENV_FILE" down -v >/dev/null 2>&1 || true
    rm -f "$ENV_FILE" "${ENV_FILE}.bak"
    rm -f "${COMPOSE_DIR}/experiment-config.yaml"
    # Substrate + observability dirs contain files written by container
    # uids the host runner doesn't match (postgres=70, forgejo/eden=1000,
    # loki=10001). Delete via a root sibling container, then rmdir from
    # the host. Defense-in-depth empty / "/" guard.
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

echo "=== Phase 1: setup-experiment ==="
bash "${REPO_ROOT}/reference/scripts/setup-experiment/setup-experiment.sh" \
    "${REPO_ROOT}/tests/fixtures/experiment/.eden/config.yaml" \
    --experiment-id "$EXPERIMENT_ID" \
    --env-file "$ENV_FILE" \
    --data-root "$SMOKE_DATA_ROOT"
rm -f "${ENV_FILE}.bak"

# Bound the experiment so the orchestrator quiesces (same as smoke.sh).
# Issue #157: max_quiescent_iterations is now an experiment-config field
# (the EDEN_MAX_QUIESCENT_ITERATIONS env var + compose default were
# retired), so inject it here the same way the other smokes do — the
# compose default is otherwise the low schema default.
EXPERIMENT_CONFIG="${COMPOSE_DIR}/experiment-config.yaml"
cat >>"$EXPERIMENT_CONFIG" <<'YAML'
ideation_policy:
  kind: fixed_total
  total: 3
max_quiescent_iterations: 30
YAML

GRAFANA_ADMIN_PASSWORD="$(grep -E '^EDEN_GRAFANA_ADMIN_PASSWORD=' "$ENV_FILE" | cut -d= -f2-)"
test -n "$GRAFANA_ADMIN_PASSWORD" || {
    echo "setup-experiment did not write EDEN_GRAFANA_ADMIN_PASSWORD" >&2
    exit 1
}

echo "=== Phase 2: static merge gate for the privileged infra overlay ==="
# A dummy gid is fine for `config` (it starts nothing); this proves the
# three-file merge resolves and the EDEN_LOGGING_DOCKER_GID :? guard is
# wired. Runs even on a socket-less host.
EDEN_LOGGING_DOCKER_GID=0 docker compose \
    -f compose.yaml -f compose.logging.yaml -f compose.logging-infra.yaml \
    --env-file "$ENV_FILE" config >/dev/null
echo "infra-overlay merge OK"

echo "=== Phase 3: bring up base + subprocess + logging ==="
docker compose "${BASE_OVERLAYS[@]}" --env-file "$ENV_FILE" up -d --wait --wait-timeout 300

# Helper: exec curl through the grafana container (ships curl; reaches
# loki:3100 + localhost:3000 on the compose network). No host ports.
gcurl() {
    docker compose "${DOWN_OVERLAYS[@]}" --env-file "$ENV_FILE" \
        exec -T grafana curl "$@"
}

echo "=== Phase 4: wait for loki readiness + EDEN log ingestion ==="
# Loki's /ready returns 503 for ~15s after startup; poll it.
deadline=$((SECONDS + 60))
until gcurl -fsS http://loki:3100/ready >/dev/null 2>&1; do
    [[ $SECONDS -lt $deadline ]] || {
        echo "loki did not become ready within 60s" >&2
        exit 1
    }
    sleep 2
done
echo "loki ready"

# Poll until the orchestrator's lines have been tailed + shipped. Alloy
# tails on a short interval; give it a generous window.
query_count() {
    local selector="$1" start end
    end="$(date +%s)000000000"
    start="$(( $(date +%s) - 3600 ))000000000"
    gcurl -fsS --get "http://loki:3100/loki/api/v1/query_range" \
        --data-urlencode "query=${selector}" \
        --data-urlencode "start=${start}" \
        --data-urlencode "end=${end}" \
        --data-urlencode "limit=5" 2>/dev/null \
        | jq '[.data.result[].values[]] | length' 2>/dev/null || echo 0
}

deadline=$((SECONDS + 120))
orch_lines=0
while [[ $SECONDS -lt $deadline ]]; do
    orch_lines="$(query_count '{service="orchestrator"}')"
    [[ "${orch_lines:-0}" -ge 1 ]] && break
    sleep 3
done
test "${orch_lines:-0}" -ge 1 || {
    echo "expected >= 1 orchestrator log line in Loki; got ${orch_lines:-0}" >&2
    echo "--- alloy logs (tail) ---" >&2
    docker compose "${DOWN_OVERLAYS[@]}" --env-file "$ENV_FILE" logs --tail 40 alloy >&2 || true
    exit 1
}
echo "orchestrator lines in Loki: ${orch_lines}"

echo "=== Phase 5: assert grafana health + provisioning ==="
gcurl -fsS http://localhost:3000/api/health | jq -e '.database == "ok"' >/dev/null || {
    echo "grafana /api/health not ok" >&2
    exit 1
}

# Datasource present (NOT just /api/health — provisioning can fail
# silently while health stays ok; plan §10).
gcurl -fsS -u "admin:${GRAFANA_ADMIN_PASSWORD}" http://localhost:3000/api/datasources \
    | jq -e 'any(.[]; .type == "loki" and .uid == "eden-loki")' >/dev/null || {
    echo "Loki datasource not provisioned in Grafana" >&2
    exit 1
}
echo "Loki datasource provisioned"

# Dashboard present.
gcurl -fsS -u "admin:${GRAFANA_ADMIN_PASSWORD}" \
    "http://localhost:3000/api/dashboards/uid/eden-explore" \
    | jq -e '.dashboard.uid == "eden-explore"' >/dev/null || {
    echo "EDEN-explore dashboard not provisioned in Grafana" >&2
    exit 1
}
echo "EDEN-explore dashboard provisioned"

# Faithfulness guard: the hardcoded DASH_DEFAULT_SELECTOR below is a
# proxy for the dashboard panel's interpolated default-view query, so it
# is only meaningful if the provisioned dashboard actually carries the
# allValue settings it assumes. Assert them directly from the provisioned
# dashboard: experiment_id/level use `.*` (so label-less infra streams
# aren't excluded), service uses `.+` (the non-empty anchor Loki requires).
DASH_ALLVALUES="$(gcurl -fsS -u "admin:${GRAFANA_ADMIN_PASSWORD}" \
    "http://localhost:3000/api/dashboards/uid/eden-explore" \
    | jq -r '.dashboard.templating.list[] | "\(.name)=\(.allValue)"' | sort | tr '\n' ' ')"
echo "dashboard template allValues: ${DASH_ALLVALUES}"
for kv in 'experiment_id=.*' 'level=.*' 'service=.+'; do
    echo "$DASH_ALLVALUES" | grep -qF "$kv" || {
        echo "dashboard template var allValue mismatch: expected '${kv}' in: ${DASH_ALLVALUES}" >&2
        echo "(the default-view query would exclude label-less infra streams — see finding-1 fix)" >&2
        exit 1
    }
done

# Sanity: every EDEN service that logs should appear as a `service`
# label value — confirms the JSON label-mapping pipeline, not just
# the orchestrator path.
SERVICES_SEEN="$(gcurl -fsS "http://loki:3100/loki/api/v1/label/service/values" \
    | jq -r '.data | sort | join(",")')"
echo "service label values: ${SERVICES_SEEN}"
for svc in orchestrator task-store-server; do
    echo "$SERVICES_SEEN" | tr ',' '\n' | grep -qx "$svc" || {
        echo "expected service label '${svc}' missing from Loki" >&2
        exit 1
    }
done

# Regression guard for the starter dashboard's default-view query. The
# eden-explore panel runs {experiment_id=~"$experiment_id",
# service=~"$service", level=~"$level"}; with all template vars at
# "All" those interpolate to the variables' allValue (.* / .+ / .*).
# `service=~".+"` is the load-bearing non-empty anchor (Loki rejects an
# all-`.*` selector); experiment_id/level use `.*` so streams missing
# those labels (the infra-overlay stdout streams) are NOT excluded from
# the default view. If someone drops the allValue settings back to the
# value-list default, infra streams silently vanish from the dashboard
# — this assertion catches that by requiring the exact default selector
# to return EDEN lines here, and (in Phase 6) infra lines too.
DASH_DEFAULT_SELECTOR='{experiment_id=~".*", service=~".+", level=~".*"}'
test "$(query_count "$DASH_DEFAULT_SELECTOR")" -ge 1 || {
    echo "dashboard default selector returned no lines: $DASH_DEFAULT_SELECTOR" >&2
    exit 1
}
echo "dashboard default selector returns EDEN lines"

echo "=== Phase 6: optional infra-stdout overlay (docker socket) ==="
if docker info >/dev/null 2>&1 && [[ -S /var/run/docker.sock ]]; then
    echo "docker socket reachable — exercising compose.logging-infra.yaml"
    # Probe the in-container gid of the socket (probe-from-inside, NOT a
    # host-side stat — the in-VM gid differs on Docker Desktop / Colima).
    PROBED_GID="$(docker run --rm \
        -v /var/run/docker.sock:/var/run/docker.sock \
        alpine:3.20 stat -c '%g' /var/run/docker.sock 2>/dev/null || true)"
    if [[ -z "$PROBED_GID" ]]; then
        echo "could not probe docker socket gid; skipping infra-overlay live tail" >&2
    else
        DOWN_OVERLAYS=("${INFRA_OVERLAYS[@]}")
        # Export (not inline) so every subsequent compose command —
        # query_count's exec, the teardown in cleanup() — also satisfies
        # the infra overlay's EDEN_LOGGING_DOCKER_GID :? guard, not just
        # this one `up`.
        export EDEN_LOGGING_DOCKER_GID="$PROBED_GID"
        docker compose "${INFRA_OVERLAYS[@]}" \
            --env-file "$ENV_FILE" up -d --wait --wait-timeout 120 alloy
        # postgres logs plenty at startup; loki.source.docker reads the
        # container's full log history, so the lines should arrive.
        deadline=$((SECONDS + 60))
        pg_lines=0
        while [[ $SECONDS -lt $deadline ]]; do
            pg_lines="$(query_count '{service="postgres"}')"
            [[ "${pg_lines:-0}" -ge 1 ]] && break
            sleep 3
        done
        test "${pg_lines:-0}" -ge 1 || {
            echo "expected >= 1 postgres stdout line in Loki via infra overlay; got ${pg_lines:-0}" >&2
            docker compose "${INFRA_OVERLAYS[@]}" --env-file "$ENV_FILE" logs --tail 40 alloy >&2 || true
            exit 1
        }
        echo "postgres stdout lines in Loki via infra overlay: ${pg_lines}"

        # The overlay's contract is "Postgres + Forgejo stdout" — assert
        # forgejo too, so a relabel-rule / docker-source regression that
        # drops forgejo can't stay green (forgejo emits startup stdout
        # even at log level Warn). Give it a window (forgejo is quieter
        # than postgres).
        deadline=$((SECONDS + 60))
        fj_lines=0
        while [[ $SECONDS -lt $deadline ]]; do
            fj_lines="$(query_count '{service="forgejo"}')"
            [[ "${fj_lines:-0}" -ge 1 ]] && break
            sleep 3
        done
        test "${fj_lines:-0}" -ge 1 || {
            echo "expected >= 1 forgejo stdout line in Loki via infra overlay; got ${fj_lines:-0}" >&2
            docker compose "${INFRA_OVERLAYS[@]}" --env-file "$ENV_FILE" logs --tail 40 alloy >&2 || true
            exit 1
        }
        echo "forgejo stdout lines in Loki via infra overlay: ${fj_lines}"

        # alloy-config-infra.alloy carries its OWN copy of the EDEN
        # file-tail pipeline (the two .alloy configs duplicate it, guarded
        # only by a "keep in sync" comment). Re-assert an EDEN-service
        # label still flows under the infra config so drift in that
        # duplicated half can't stay green.
        test "$(query_count '{service="orchestrator"}')" -ge 1 || {
            echo "EDEN file-tail pipeline broke under the infra config (no orchestrator lines)" >&2
            exit 1
        }
        echo "EDEN file-tail still flows under the infra config"

        # The default dashboard view must surface infra stdout too (the
        # finding-1 allValue fix). A bare `$DASH_DEFAULT_SELECTOR >= 1`
        # would be satisfied by EDEN lines alone, so it can't prove infra
        # is included. Instead pin service=postgres (a label-less stream:
        # no experiment_id, no level) and keep the experiment_id/level
        # matchers at the dashboard's `.*` allValue shape: this returns
        # lines ONLY IF those `.*` matchers do not exclude streams missing
        # the label. If allValue reverted to a value-list (which excludes
        # label-less streams), this drops to 0 and fails — exactly the
        # regression finding 1 fixed.
        test "$(query_count '{service="postgres", experiment_id=~".*", level=~".*"}')" -ge 1 || {
            echo "default-view label shape ({...,experiment_id=~\".*\",level=~\".*\"}) excludes label-less infra streams" >&2
            exit 1
        }
        echo "dashboard default-view label shape surfaces label-less infra streams"
    fi
else
    echo "docker socket not reachable — skipping infra-overlay live tail (static merge gate in Phase 2 still ran)"
fi

echo "PASS"
