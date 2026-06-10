#!/usr/bin/env bash
set -euo pipefail

# helm-upgrade-smoke — issue #284 (Phase 13a §6.3 deferral): prove `helm
# upgrade` works in place against a live release.
#
# Installs the chart at the upgrade BASELINE — the merge-base with origin/main
# (on a PR that is main's chart; on a main push it is HEAD itself, degrading
# to a same-chart upgrade that still exercises the upgrade machinery) — drives
# the fixture experiment to a first variant.integrated, then `helm upgrade`s
# to the WORKTREE chart per the documented operator procedure
# (docs/deployment/helm.md §7, --reset-then-reuse-values) and asserts:
#
#   - the upgrade applies cleanly: no immutable-field errors (StatefulSet
#     volumeClaimTemplates, Service clusterIP, ...) and every workload rolls
#     to readiness;
#   - experiment state survives: event counts never regress across the
#     upgrade (a PVC reclaim mistake would reset Postgres and zero them);
#   - the stack still reaches the full helm-smoke end-state and passes the
#     helm test connection probe post-upgrade.
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
# Post-upgrade budget for the full helm-smoke end-state (same knob as
# ci-smoke.sh so CI tuning applies to both).
DEADLINE_SECONDS="${EDEN_HELM_SMOKE_DEADLINE:-240}"

# --- Resolve + extract the baseline chart tree ---
BASELINE_REF="${EDEN_UPGRADE_BASELINE_REF:-}"
if [[ -z "$BASELINE_REF" ]]; then
    git -C "$REPO_ROOT" fetch origin main --quiet 2>/dev/null || true
    BASELINE_REF="$(git -C "$REPO_ROOT" merge-base HEAD origin/main)" || {
        echo "cannot resolve merge-base(HEAD, origin/main); set EDEN_UPGRADE_BASELINE_REF" >&2
        exit 2
    }
fi
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

eden_create_kind_cluster
eden_build_and_load_image

# --- Install at the baseline (baseline chart + setup script + ci-values) ---
echo "--- running setup-experiment-helm.sh @ baseline ---" >&2
bash "${BASELINE_DIR}/reference/scripts/setup-experiment-helm.sh" \
    --namespace "$NAMESPACE" \
    --release "$RELEASE" \
    --chart "${BASELINE_DIR}/reference/helm/eden" \
    --values "${BASELINE_DIR}/reference/helm/eden/ci-values.yaml" \
    --experiment-config "${REPO_ROOT}/tests/fixtures/experiment/.eden/config.yaml" \
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
eden_wait_for_integrated 3 "$DEADLINE_SECONDS" || true
eden_assert_end_state

echo "--- running helm test (connection probe) ---" >&2
helm test "$RELEASE" -n "$NAMESPACE" --timeout 2m

echo "--- helm-upgrade-smoke PASSED ---" >&2
