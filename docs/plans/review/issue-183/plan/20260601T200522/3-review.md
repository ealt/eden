**1. Missing Context**

Assessment: resolved. The plan now gives a reader the load-bearing context up front: `#147` provides the multi-experiment base, `#183` adds the missing cross-mode cells, and exp-2 needs its own overlays rather than a magical overlay composition shortcut ([issue-183-cross-mode-ci-smoke-matrix.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-183-cross-mode-ci-smoke-matrix/docs/plans/issue-183-cross-mode-ci-smoke-matrix.md:64)).

**2. Feasibility**

Assessment: resolved. The dedicated `compose.multi-experiment.subprocess.yaml` and `compose.multi-experiment.docker.yaml` design matches the repo’s real substrate model and preserves the existing DooD privilege boundary ([issue-183-cross-mode-ci-smoke-matrix.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-183-cross-mode-ci-smoke-matrix/docs/plans/issue-183-cross-mode-ci-smoke-matrix.md:123), [issue-183-cross-mode-ci-smoke-matrix.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-183-cross-mode-ci-smoke-matrix/docs/plans/issue-183-cross-mode-ci-smoke-matrix.md:157)).

**3. Alternatives**

Assessment: the chosen approach now looks like the right one. Overlay symmetry for exp-1 and exp-2 is simpler and more robust than inventing a separate mode-selection mechanism for only half the stack.

**4. Completeness**

Assessment: almost there. I see one remaining clarification worth making.

Suggestion:
- Be more explicit about the full exp-2 env surface the new overlays need, not just “`_2`-suffixed cidfiles dir etc. for docker mode” ([issue-183-cross-mode-ci-smoke-matrix.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-183-cross-mode-ci-smoke-matrix/docs/plans/issue-183-cross-mode-ci-smoke-matrix.md:130), [issue-183-cross-mode-ci-smoke-matrix.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-183-cross-mode-ci-smoke-matrix/docs/plans/issue-183-cross-mode-ci-smoke-matrix.md:253)). `compose.subprocess.yaml` for exp-1 depends on more than docker-only knobs: e.g. `EDEN_EXPERIMENT_DIR_HOST`, `EDEN_READONLY_STORE_URL`, `FORGEJO_REMOTE_URL`, and `EDEN_FORGEJO_CREDS_DIR_HOST` in addition to the repo/artifact paths ([compose.subprocess.yaml](/Users/ericalt/Documents/eden-worktrees/plan-issue-183-cross-mode-ci-smoke-matrix/reference/compose/compose.subprocess.yaml:31), [compose.subprocess.yaml](/Users/ericalt/Documents/eden-worktrees/plan-issue-183-cross-mode-ci-smoke-matrix/reference/compose/compose.subprocess.yaml:58), [compose.subprocess.yaml](/Users/ericalt/Documents/eden-worktrees/plan-issue-183-cross-mode-ci-smoke-matrix/reference/compose/compose.subprocess.yaml:89), [compose.subprocess.yaml](/Users/ericalt/Documents/eden-worktrees/plan-issue-183-cross-mode-ci-smoke-matrix/reference/compose/compose.subprocess.yaml:149)). I would add one sentence saying the `-2` overlays require the full exp-2 analog of every env var the exp-1 subprocess/docker overlays consume, not only the cidfile/docker-specific subset.

**5. Edge Cases and Risks**

Assessment: strong. Risk 6 and risk 9 are exactly the right additions; they cover the two most likely false-confidence failure modes: silent scripted fallback and bad Compose merge behavior ([issue-183-cross-mode-ci-smoke-matrix.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-183-cross-mode-ci-smoke-matrix/docs/plans/issue-183-cross-mode-ci-smoke-matrix.md:305), [issue-183-cross-mode-ci-smoke-matrix.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-183-cross-mode-ci-smoke-matrix/docs/plans/issue-183-cross-mode-ci-smoke-matrix.md:311)).

**Overall Assessment**

This is now in good shape and no longer has an architectural blocker. The overlay design is coherent, the positive controls are concrete, and the plan is mostly implementation-ready. I would make the one completeness clarification about the full exp-2 env surface, then I’d consider the plan ready for plan-stage approval.