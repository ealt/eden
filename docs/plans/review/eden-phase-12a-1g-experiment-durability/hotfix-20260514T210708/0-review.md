**Findings**

- Risk — cleanup can still hide the script’s real exit code if the helper container cannot run. In all four scripts, the new `docker run ... || true` is followed by an unguarded host `rm -rf "$SMOKE_DATA_ROOT"`, so if the helper pull/run fails and the bind-mount tree still contains container-owned `0755` subdirs, `rm -rf` will fail under `set -e` and replace the original status. Refs: [smoke.sh](/Users/ericalt/Documents/eden-worktrees/phase-12a-1g-experiment-durability/reference/compose/healthcheck/smoke.sh:53), [smoke-subprocess.sh](/Users/ericalt/Documents/eden-worktrees/phase-12a-1g-experiment-durability/reference/compose/healthcheck/smoke-subprocess.sh:44), [smoke-subprocess-docker.sh](/Users/ericalt/Documents/eden-worktrees/phase-12a-1g-experiment-durability/reference/compose/healthcheck/smoke-subprocess-docker.sh:54), [e2e.sh](/Users/ericalt/Documents/eden-worktrees/phase-12a-1g-experiment-durability/reference/compose/healthcheck/e2e.sh:73). Fix: keep the helper delete best-effort, but make the final host removal best-effort too, ideally with a warning if the helper fails, for example `rm -rf "$SMOKE_DATA_ROOT" >/dev/null 2>&1 || true` or `rmdir ... || true`.

**Assessment**

The container-side delete is the right minimal shape for the actual Linux CI bug. It directly addresses the cross-UID bind-mount case; `sudo` would add a host-specific assumption, `chown-and-rm` is more invasive than needed, and running the smoke as a non-default uid would still not match both `postgres=70` and `eden/gitea=1000`.

The `EXIT` trap coverage is otherwise good: it will run on success, normal `set -e` failures, and catchable shell-termination paths. The `[[ -d "$SMOKE_DATA_ROOT" ]]` guard is sufficient for avoiding a second mount attempt on an already-removed path. Symmetry is good across the four scripts, and [smoke-subprocess-docker.sh](/Users/ericalt/Documents/eden-worktrees/phase-12a-1g-experiment-durability/reference/compose/healthcheck/smoke-subprocess-docker.sh:45) appropriately keeps its cidfile cleanup separate.

No CI pre-pull is strictly required, but three of the four scripts may now need to pull `alpine:3.20` during cleanup if it is not already cached. That is acceptable only if cleanup failure stays non-fatal; in the current form it does not.

**Overall**

Don’t ship until the exit-code-masking issue is fixed. The core cleanup approach is correct, but this one robustness hole is enough to keep the hotfix from being reliably diagnostic.