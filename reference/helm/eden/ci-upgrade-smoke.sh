#!/usr/bin/env bash
set -euo pipefail

# helm-upgrade-smoke — issue #284 (Phase 13a §6.3 deferral): prove `helm
# upgrade` works in place against a live release.
#
# Installs the chart at the upgrade BASELINE — the merge-base with origin/main
# (on a PR that is main's chart; when the merge-base is HEAD itself — a main
# push or a local run on main — the previous commit, so the run still crosses
# a real chart diff) — drives a doubled-total (6-variant) derivative of the
# fixture experiment to a first variant.integrated, then `helm upgrade`s to
# the WORKTREE chart per the documented operator procedure
# (docs/deployment/helm.md §7, --reset-then-reuse-values) and asserts:
#
#   - the upgrade applies cleanly: no immutable-field errors (StatefulSet
#     volumeClaimTemplates, Service clusterIP, ...) and every workload rolls
#     to readiness;
#   - experiment state survives: event counts never regress across the
#     upgrade (a PVC reclaim mistake would reset Postgres and zero them);
#   - the upgraded stack still makes progress (the doubled total keeps work
#     in flight across the upgrade) and reaches the 6-variant end-state +
#     passes the helm test connection probe post-upgrade.
#
# The baseline install runs the WORKTREE image (the image under test) under
# the BASELINE chart + setup script. A PR that changes the chart and the
# image's CLI surface in lockstep can therefore fail the baseline bring-up;
# override the baseline with EDEN_UPGRADE_BASELINE_REF=<ref> (e.g. HEAD for a
# same-chart upgrade) and call out why in the PR.
#
# Mirrors the CI helm-upgrade-smoke job; runnable locally with kind + helm
# (>= 3.14 for --reset-then-reuse-values) + kubectl + docker + jq + python3 +
# git installed. Shared harness in ci-smoke-lib.sh.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CHART_DIR="$SCRIPT_DIR"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=ci-smoke-lib.sh
. "${SCRIPT_DIR}/ci-smoke-lib.sh"

eden_require_tools kind kubectl helm docker jq python3 git

CLUSTER="${EDEN_KIND_CLUSTER:-eden-helm-upgrade-smoke}"
NAMESPACE="eden-ci"
RELEASE="eden"
IMAGE="eden-reference:ci"
EXPERIMENT_ID="$(eden_mint_experiment_id)"
# Pre-upgrade budget: one integration proves the baseline release holds live
# experiment state worth preserving across the upgrade.
PRE_UPGRADE_DEADLINE="${EDEN_HELM_UPGRADE_SMOKE_PRE_DEADLINE:-240}"
# Post-upgrade budget for the doubled-total end-state (more headroom than
# ci-smoke.sh's 240s: up to 5 of the 6 variants may still be pending when
# the upgrade lands).
DEADLINE_SECONDS="${EDEN_HELM_UPGRADE_SMOKE_DEADLINE:-420}"

# --- Resolve + extract the baseline chart tree ---
BASELINE_REF="${EDEN_UPGRADE_BASELINE_REF:-}"
if [[ -z "$BASELINE_REF" ]]; then
    # Refresh origin/main explicitly (plain `git fetch origin main` only
    # updates FETCH_HEAD in some configs); tolerate fetch failure only when
    # origin/main already resolves locally.
    git -C "$REPO_ROOT" fetch --quiet origin "+refs/heads/main:refs/remotes/origin/main" 2>/dev/null \
        || git -C "$REPO_ROOT" rev-parse --verify --quiet refs/remotes/origin/main >/dev/null \
        || { echo "cannot resolve origin/main; set EDEN_UPGRADE_BASELINE_REF" >&2; exit 2; }
    BASELINE_REF="$(git -C "$REPO_ROOT" merge-base HEAD origin/main)" || {
        echo "cannot resolve merge-base(HEAD, origin/main); set EDEN_UPGRADE_BASELINE_REF" >&2
        exit 2
    }
    # On a push to main (or a local run on main) the merge-base IS HEAD; fall
    # back to the previous commit so the run still exercises a real
    # cross-chart upgrade rather than the degenerate same-chart one.
    if [[ "$(git -C "$REPO_ROOT" rev-parse "$BASELINE_REF")" == "$(git -C "$REPO_ROOT" rev-parse HEAD)" ]] \
        && git -C "$REPO_ROOT" rev-parse --verify --quiet HEAD~1 >/dev/null; then
        BASELINE_REF="HEAD~1"
    fi
fi
git -C "$REPO_ROOT" cat-file -e "${BASELINE_REF}:reference/helm/eden/Chart.yaml" 2>/dev/null || {
    echo "baseline '${BASELINE_REF}' has no chart at reference/helm/eden; set EDEN_UPGRADE_BASELINE_REF" >&2
    exit 2
}
echo "--- upgrade baseline: $(git -C "$REPO_ROOT" log -1 --oneline "$BASELINE_REF") ---" >&2

BASELINE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/eden-upgrade-baseline.XXXXXX")"
eden_extra_cleanup() {
    rm -rf "$BASELINE_DIR"
}
# The extracted tree preserves the repo layout so setup-experiment-helm.sh's
# REPO_ROOT-relative path resolution works inside it.
git -C "$REPO_ROOT" archive "$BASELINE_REF" \
    reference/helm/eden reference/scripts/setup-experiment-helm.sh \
    | tar -x -C "$BASELINE_DIR"

# --- Derive a doubled-total experiment config ---
# The base fixture's fixed_total(3) often completes during the setup script's
# own rollout waits, which would make every post-upgrade progress assertion
# vacuous. The upgrade smoke doubles the total so work is still in flight
# when the upgrade lands. 6 variants = 6 integrated + 18 completed tasks
# (ideation + execution + evaluation per variant) at the end state.
UPGRADE_TOTAL=6
UPGRADE_COMPLETED_TARGET=$((UPGRADE_TOTAL * 3))
UPGRADE_CONFIG="${BASELINE_DIR}/upgrade-config.yaml"
sed "s/^\\(  total:\\) 3\$/\\1 ${UPGRADE_TOTAL}/" \
    "${REPO_ROOT}/tests/fixtures/experiment/.eden/config.yaml" > "$UPGRADE_CONFIG"
grep -q "^  total: ${UPGRADE_TOTAL}\$" "$UPGRADE_CONFIG" || {
    echo "failed to derive the doubled-total config — fixture ideation_policy shape changed?" >&2
    exit 2
}

eden_create_kind_cluster
eden_build_and_load_image

# --- Install at the baseline (baseline chart + setup script + ci-values) ---
echo "--- running setup-experiment-helm.sh @ baseline ---" >&2
bash "${BASELINE_DIR}/reference/scripts/setup-experiment-helm.sh" \
    --namespace "$NAMESPACE" \
    --release "$RELEASE" \
    --chart "${BASELINE_DIR}/reference/helm/eden" \
    --values "${BASELINE_DIR}/reference/helm/eden/ci-values.yaml" \
    --experiment-config "$UPGRADE_CONFIG" \
    --experiment-id "$EXPERIMENT_ID" \
    --timeout 5m

eden_resolve_task_store
eden_wait_for_integrated 1 "$PRE_UPGRADE_DEADLINE" || {
    echo "baseline release never reached 1 variant.integrated within ${PRE_UPGRADE_DEADLINE}s" >&2
    kubectl get pods -n "$NAMESPACE" >&2 || true
    exit 1
}

events="$(eden_fetch_events)"
INTEGRATED_PRE="$(eden_count_type "$events" "variant.integrated")"
COMPLETED_PRE="$(eden_count_type "$events" "task.completed")"
echo "--- pre-upgrade counts: variant.integrated=${INTEGRATED_PRE} task.completed=${COMPLETED_PRE} ---" >&2

# --- Upgrade in place to the worktree chart (the documented §7 procedure).
# An immutable-field patch (StatefulSet volumeClaimTemplates, Service
# clusterIP, ...) fails the upgrade right here. ---
echo "--- helm upgrade to the worktree chart ---" >&2
helm upgrade "$RELEASE" "$CHART_DIR" -n "$NAMESPACE" \
    --reset-then-reuse-values --wait --timeout 5m

# Every workload must roll to readiness post-upgrade (crash loops against
# retained PVCs surface here when --wait alone is too lenient).
while IFS= read -r workload; do
    [[ -n "$workload" ]] || continue
    kubectl rollout status "$workload" -n "$NAMESPACE" --timeout=5m
done < <(kubectl get statefulset,deployment -n "$NAMESPACE" -o name)

# --- State survival: the event log is append-only, so counts must never
# regress across the upgrade. The upgrade may have rolled the
# task-store-server pod, so re-resolve it first. ---
eden_resolve_task_store
events="$(eden_fetch_events)"
INTEGRATED_POST="$(eden_count_type "$events" "variant.integrated")"
COMPLETED_POST="$(eden_count_type "$events" "task.completed")"
echo "--- post-upgrade counts: variant.integrated=${INTEGRATED_POST} task.completed=${COMPLETED_POST} ---" >&2
test "${INTEGRATED_POST:-0}" -ge "$INTEGRATED_PRE" || {
    echo "variant.integrated regressed across the upgrade (${INTEGRATED_PRE} -> ${INTEGRATED_POST}): experiment state was lost" >&2
    exit 1
}
test "${COMPLETED_POST:-0}" -ge "$COMPLETED_PRE" || {
    echo "task.completed regressed across the upgrade (${COMPLETED_PRE} -> ${COMPLETED_POST}): experiment state was lost" >&2
    exit 1
}

# --- The upgraded stack must still finish the experiment ---
eden_wait_for_integrated "$UPGRADE_TOTAL" "$DEADLINE_SECONDS" || true

# Strict post-upgrade progress, keyed on variant.integrated — the LAST stage
# of the task chain, so it is the slowest to saturate and the strongest
# post-upgrade signal (task.completed saturates while integrations are still
# in flight in-orchestrator). When integrations remained at the snapshot (the
# doubled total makes this the overwhelmingly common case), the final count
# must strictly exceed the pre-upgrade one — pods that come back Ready but
# never integrate another variant fail here. When every variant had already
# integrated pre-upgrade (slow pre-wait poll on a fast machine), progress is
# unprovable; the no-regress + end-state + helm-test assertions still hold,
# so note it rather than flake.
events="$(eden_fetch_events)"
INTEGRATED_FINAL="$(eden_count_type "$events" "variant.integrated")"
if [[ "$INTEGRATED_PRE" -lt "$UPGRADE_TOTAL" ]]; then
    test "${INTEGRATED_FINAL:-0}" -gt "$INTEGRATED_PRE" || {
        echo "no post-upgrade progress: variant.integrated stuck at ${INTEGRATED_PRE} (target ${UPGRADE_TOTAL})" >&2
        exit 1
    }
else
    echo "NOTE: all ${UPGRADE_TOTAL} variants already integrated pre-upgrade; strict progress assertion skipped" >&2
fi

eden_assert_end_state "$UPGRADE_TOTAL" "$UPGRADE_COMPLETED_TARGET" "$UPGRADE_TOTAL"

echo "--- running helm test (connection probe) ---" >&2
helm test "$RELEASE" -n "$NAMESPACE" --timeout 2m

echo "--- helm-upgrade-smoke PASSED ---" >&2
