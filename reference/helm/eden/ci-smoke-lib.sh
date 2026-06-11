# shellcheck shell=bash
# ci-smoke-lib.sh — shared harness for the Helm smoke scripts (ci-smoke.sh,
# ci-upgrade-smoke.sh). Sourced, not executed.
#
# Callers must set CLUSTER / NAMESPACE / RELEASE / IMAGE / EXPERIMENT_ID /
# REPO_ROOT before calling the functions below. Must stay bash-3.2-clean
# (macOS default bash) — no mapfile/readarray, no associative arrays.

eden_require_tools() {
    local tool
    for tool in "$@"; do
        command -v "$tool" >/dev/null || {
            echo "$(basename "$0") requires '$tool' on PATH" >&2
            exit 2
        }
    done
}

# Mint a valid opaque exp_* id (#128 grammar ^exp_[0-9a-hjkmnp-tv-z]{26}$). An
# operator-typed mnemonic like "exp-1" is rejected by the event model at
# runtime, so the smokes must drive a real opaque id end-to-end.
eden_mint_experiment_id() {
    python3 -c '
import secrets, time
alphabet = "0123456789abcdefghjkmnpqrstvwxyz"
value = ((int(time.time() * 1000) & ((1 << 48) - 1)) << 80) | secrets.randbits(80)
print("exp_" + "".join(alphabet[(value >> (5 * i)) & 31] for i in range(26))[::-1])
'
}

# Only tear down a cluster THIS script created — never delete a pre-existing
# local cluster that happens to share the name.
EDEN_CREATED_CLUSTER=0
eden_cleanup() {
    local rc=$?
    # Optional per-script hook (e.g. temp-dir removal) defined by the caller.
    if type eden_extra_cleanup >/dev/null 2>&1; then
        eden_extra_cleanup || true
    fi
    if [[ "$EDEN_CREATED_CLUSTER" -eq 1 ]]; then
        echo "--- tearing down kind cluster ${CLUSTER} ---" >&2
        kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true
    fi
    exit "$rc"
}

eden_create_kind_cluster() {
    trap eden_cleanup EXIT
    if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
        echo "kind cluster '${CLUSTER}' already exists; refusing to reuse/delete it." >&2
        echo "Delete it yourself or set EDEN_KIND_CLUSTER to a fresh name." >&2
        exit 2
    fi
    echo "--- creating kind cluster ${CLUSTER} ---" >&2
    kind create cluster --name "$CLUSTER" --wait 120s
    EDEN_CREATED_CLUSTER=1
}

eden_build_and_load_image() {
    echo "--- building + loading ${IMAGE} ---" >&2
    docker build -t "$IMAGE" -f "${REPO_ROOT}/reference/compose/Dockerfile" "$REPO_ROOT"
    kind load docker-image "$IMAGE" --name "$CLUSTER"
}

# Resolve the task-store-server pod + admin bearer used by eden_fetch_events.
# Re-call after any operation that may roll the pod (e.g. helm upgrade) — the
# old pod name dangles after a rollout.
eden_resolve_task_store() {
    TASK_STORE_POD="$(kubectl get pod -n "$NAMESPACE" \
        -l app.kubernetes.io/component=task-store-server \
        --field-selector status.phase=Running -o name | head -n1)"
    ADMIN_TOKEN="$(kubectl get secret "${RELEASE}-secrets" -n "$NAMESPACE" \
        -o 'jsonpath={.data.EDEN_ADMIN_TOKEN}' | base64 -d)"
}

eden_fetch_events() {
    kubectl exec -n "$NAMESPACE" "$TASK_STORE_POD" -- curl -fsS \
        -H "Authorization: Bearer admin:${ADMIN_TOKEN}" \
        -H "X-Eden-Experiment-Id: ${EXPERIMENT_ID}" \
        "http://localhost:8080/v0/experiments/${EXPERIMENT_ID}/events"
}

eden_count_type() {
    # eden_count_type <events-json> <type> [task-id-prefix]
    if [[ -n "${3:-}" ]]; then
        echo "$1" | jq --arg p "$3" '(.events // .) | [.[] | select(
            .type == "task.completed" and (.data.task_id | startswith($p)))] | length'
    else
        echo "$1" | jq --arg t "$2" '(.events // .) | [.[] | select(.type == $t)] | length'
    fi
}

eden_wait_for_integrated() {
    # eden_wait_for_integrated <min-count> <budget-seconds> — poll the event
    # feed until >= <min-count> variant.integrated events are seen. Returns 1
    # on budget exhaustion (callers decide whether that is fatal).
    local min="$1" budget="$2" elapsed=0 events integrated=0
    echo "--- waiting for >= ${min} variant.integrated (budget ${budget}s) ---" >&2
    while [[ "$elapsed" -lt "$budget" ]]; do
        events="$(eden_fetch_events 2>/dev/null || echo '{}')"
        integrated="$(eden_count_type "$events" "variant.integrated")"
        if [[ "${integrated:-0}" -ge "$min" ]]; then
            return 0
        fi
        sleep 5
        elapsed=$((elapsed + 5))
    done
    return 1
}

eden_assert_end_state() {
    # eden_assert_end_state [min-integrated] [min-completed] [min-ideation] —
    # defaults are the compose-smoke-equivalent end-state for the 3-variant
    # fixture: >= 3 variant.integrated, >= 9 task.completed, >= 3
    # ideation-task task.completed events.
    local min_integrated="${1:-3}" min_completed="${2:-9}" min_ideation="${3:-3}"
    local events integrated completed ideation_completed
    events="$(eden_fetch_events)"
    integrated="$(eden_count_type "$events" "variant.integrated")"
    completed="$(eden_count_type "$events" "task.completed")"
    ideation_completed="$(eden_count_type "$events" "task.completed" "ideation-")"

    echo "--- final counts: variant.integrated=${integrated} task.completed=${completed} ideation=${ideation_completed} ---" >&2
    test "${integrated:-0}" -ge "$min_integrated" || { echo "expected >= ${min_integrated} variant.integrated; got ${integrated}" >&2; exit 1; }
    test "${completed:-0}" -ge "$min_completed" || { echo "expected >= ${min_completed} task.completed; got ${completed}" >&2; exit 1; }
    test "${ideation_completed:-0}" -ge "$min_ideation" || { echo "expected >= ${min_ideation} ideation task.completed; got ${ideation_completed}" >&2; exit 1; }
}
