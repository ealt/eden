# Hotfix Review: 12a-1g smoke-script teardown bind-mount ownership

## Plan reference

This is a hotfix for Phase 12a-1g (docs/plans/eden-phase-12a-1g-experiment-durability.md).
After 12a-1g merged to main, all four compose smokes started failing on
teardown with "rm: cannot remove ...: Permission denied" against
container-created subdirectories inside the bind-mount tree.

## Symptom

```text
rm: cannot remove '/tmp/eden-smoke-XXXXXX/orchestrator-repo/hooks/update.sample': Permission denied
rm: cannot remove '/tmp/eden-smoke-XXXXXX/web-ui-repo/refs/tags': Permission denied
(repeated for many .sample files and dirs)
```

Branch protection on main requires all four smoke jobs; current main is
blocking PR #90.

## Root cause

12a-1g made `${EDEN_EXPERIMENT_DATA_ROOT}/<subdir>/` host bind-mounts.
Containers write into them as their own uid (postgres=70, gitea/eden=1000)
and create subdirectories (`hooks/`, `refs/`, `objects/`) with mode 0755
(container's umask). The host runner's `rm -rf "$SMOKE_DATA_ROOT"` cannot
delete files inside those container-created subdirectories.

Local-on-macOS doesn't reproduce because Docker Desktop's VFS uid-translation
layer hides the issue. Linux CI runners hit it directly because uids pass
through.

## Fix

Before the host `rm -rf "$SMOKE_DATA_ROOT"` in each script's cleanup()
function, run a sibling `alpine:3.20` container as root with the bind-mount
at `/cleanup` and `find /cleanup -mindepth 1 -delete`. Root inside the
container can delete files of any uid. The host then only needs to rmdir
the now-empty bind-mount root.

## Files changed (4)

| File | Change |
|---|---|
| reference/compose/healthcheck/smoke.sh | Added container-side rm before host rm in cleanup() |
| reference/compose/healthcheck/smoke-subprocess.sh | Same |
| reference/compose/healthcheck/smoke-subprocess-docker.sh | Same |
| reference/compose/healthcheck/e2e.sh | Same |

## Test plan

| Gate | Result |
|---|---|
| uv run ruff check . | clean |
| uv run pyright | 0 errors |
| uv run pytest -q (not e2e/docker) | 1016 passed, 128 skipped |
| uv run pytest -q conformance/ | 158 passed, 1 skipped |
| markdownlint, check-jsonschema, spec-xref, rename-discipline | clean |
| smoke.sh | PASS, no leftover /tmp/eden-smoke-* |
| smoke-subprocess.sh | PASS, no leftover dir |
| e2e.sh | PASS, no leftover dir |
| smoke-subprocess-docker.sh | not run locally (slow + reliant on docker-exec wrap); fix is symmetric |

Codex-review starting now.
