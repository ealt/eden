# Codex-review record — issues #154 + #155 (impl)

- **PR:** <https://github.com/ealt/eden/pull/201>
- **Commit reviewed:** `b679fa4` (initial impl)
- **Fix commit:** `8e25a40` (round 1 fix)
- **Round:** 1
- **Reviewer:** `code-review` skill (high effort — 3 angles × up to 6 candidates → recall-biased verify)

## Scope

Planless chunk bundling two Phase 12a-1f deferrals:

- **#154** — executor-host substrate access (mirror ideator/evaluator)
- **#155** — DooD `--exec-network` un-suppression

## Findings

### Blocking (fixed in `8e25a40`)

**Executor WARN gating asymmetric with ideator/evaluator.** Original trigger checked `args.artifact_url is not None or args.readonly_store_url is not None`; missing `args.repo_path is not None`. Since `--repo-path` is required in executor subprocess mode, the omission meant `EDEN_REPO_DIR` was silently suppressed under `--exec-mode docker` without `--exec-network` whenever URL substrates weren't separately configured — no WARN. Fix: include `args.repo_path is not None` in the OR-list. Two type-narrowing `assert` lines added because the new is-not-None clause teaches pyright args.repo_path could be None (argparse-required invariant is not visible to the type checker).

### Deferred (accepted limitations / future enhancements)

1. **No `--exec-network` validation.** A typo'd network silently bypasses suppression; the spawned sibling fails late inside the per-task spawn. **Mitigation:** WARN hint points at `--exec-network`; spec §9.3 documents the requirement. **Future:** runtime `docker network inspect` check at host startup.
2. **`EDEN_REPO_DIR` forwarded without bind-mount validation.** Operator must separately wire `--exec-bind` for the repo path; reference compose stack does this for executor + evaluator. **Future:** host CLI check that EDEN_REPO_DIR's path is reachable via the configured exec_binds.
3. **Smoke assertion is absence-only.** Greps that the suppression WARN did not log; does not positively probe a sibling for env values or substrate reachability. **Future:** sibling-side `printenv` + URL `curl` probe inside the spawned image.
4. **Default network `eden-reference_default` brittle to `-p` overrides.** Operators with custom project names set `$EDEN_EXEC_NETWORK`. **Mitigation:** documented in CLI help, spec §9.3, CHANGELOG. **Future:** auto-derive from `COMPOSE_PROJECT_NAME`.

## Validation post-fix

- `uv run ruff check .` clean
- `uv run pyright` 0 errors
- `uv run pytest -q reference/services/executor/tests/` 12 passed
- Full `uv run pytest -q` ran cleanly pre-fix (1796 passed, 217 skipped); the round-1 fix only added type-narrowing asserts + a single OR-clause, so the green pytest carries.

## Outcome

Round 1 sufficient. Remaining findings classified as deferred per the operator-discipline / scope analysis above; no further rounds needed before merge.
