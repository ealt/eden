#!/usr/bin/env bash
# status.sh — one-screen project state aggregator.
#
# Combines:
#   - Critical-path chain: which Phase-12 chunks have shipped, which are in flight
#   - In-flight PRs: number, title, mergeable state, CI summary
#   - Active delegates: registry.sh sessions not in terminal states
#   - Recently merged: last 5 commits on origin/main
#
# Pure read-only. Requires: gh, jq. The delegate section is best-effort —
# if registry.sh is not on PATH (e.g. operator hasn't installed the toolchain),
# the section is skipped without erroring.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REGISTRY_SH="${EDEN_REGISTRY_SH:-$HOME/Documents/toolchain/skills/delegate-claude-session/scripts/registry.sh}"

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
hr() { printf -- '-%.0s' $(seq 1 72); printf '\n'; }

# --- Critical-path chain -----------------------------------------------------

bold "Critical-path chain (Phase 12)"
hr

# Derive shipped vs planned by grepping roadmap.md's status flips.
# This is intentionally textual — the roadmap is the source of truth.
if [[ -f "$REPO_ROOT/docs/roadmap.md" ]]; then
  awk '
    /^### Phase 12|^- \[12[abc]/ {
      sub(/ \(see \[CHANGELOG\]\([^)]*\)\)/, "")
      print
    }
  ' "$REPO_ROOT/docs/roadmap.md" | sed 's/^- /  /'
else
  echo "  (docs/roadmap.md not found — run from a repo checkout)"
fi
echo

# --- In-flight PRs -----------------------------------------------------------

bold "In-flight PRs"
hr

if command -v gh >/dev/null 2>&1; then
  # Pull open PRs against main with checks state
  prs_json="$(gh pr list --state open --base main --limit 30 \
    --json number,title,mergeable,headRefName,statusCheckRollup 2>/dev/null || echo '[]')"

  count="$(echo "$prs_json" | jq 'length')"
  if [[ "$count" == "0" ]]; then
    echo "  (no open PRs against main)"
  else
    echo "$prs_json" | jq -r '
      .[] |
      "  #\(.number) \(.title)\n      branch=\(.headRefName) mergeable=\(.mergeable // "?")" +
      "\n      checks: " + (
        if (.statusCheckRollup | length) == 0 then
          "(no checks)"
        else
          ((.statusCheckRollup | map(select(.conclusion == "SUCCESS")) | length) | tostring) + " pass / " +
          ((.statusCheckRollup | map(select(.conclusion == "FAILURE" or .conclusion == "TIMED_OUT" or .conclusion == "CANCELLED")) | length) | tostring) + " fail / " +
          ((.statusCheckRollup | map(select(.status == "IN_PROGRESS" or .status == "QUEUED" or .status == "PENDING")) | length) | tostring) + " run"
        end
      )
    '
  fi
else
  echo "  (gh CLI not on PATH — install or check $PATH)"
fi
echo

# --- Active delegates --------------------------------------------------------

bold "Active delegates"
hr

if [[ -x "$REGISTRY_SH" ]]; then
  # Filter to sessions whose status is not in {completed, abandoned, deleted}
  registry_json="$("$REGISTRY_SH" list 2>/dev/null || echo '[]')"
  active="$(echo "$registry_json" | jq '[.[] | select(.status as $s | (["completed","abandoned","deleted"] | index($s)) | not)]')"

  count="$(echo "$active" | jq 'length')"
  if [[ "$count" == "0" ]]; then
    echo "  (no active delegate sessions)"
  else
    echo "$active" | jq -r '
      .[] |
      "  \(.session_id[0:8]) \(.label // "(no label)")" +
      "\n      status=\(.status // "?") updated=\(.updated_at // "?")"
    '
  fi
else
  echo "  (registry.sh not at $REGISTRY_SH — set \$EDEN_REGISTRY_SH or install delegate-claude-session)"
fi
echo

# --- Recently merged ---------------------------------------------------------

bold "Recently merged (last 5 on origin/main)"
hr

if git -C "$REPO_ROOT" rev-parse --git-dir >/dev/null 2>&1; then
  git -C "$REPO_ROOT" log --oneline origin/main -5 2>/dev/null | sed 's/^/  /' || \
    echo "  (could not read git log — has origin/main been fetched?)"
else
  echo "  (not a git repo)"
fi
echo

exit 0
