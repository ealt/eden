**Findings**

- No findings.

**Overall Assessment**

Round 2 closes the remaining blocker. The strip-set logic is now shaped correctly for the problem: a static superset of Git 2.44’s declared local env vars, unioned with the local `git rev-parse --local-env-vars` output and cached, is the right defense against future drift. The new regressions are the ones that mattered: one asserts strip-set coverage against Git’s own declaration, and one proves ambient `GIT_GRAFT_FILE` cannot spoof `commit_parents()` or `is_ancestor()`.

I reran `uv run pytest -q reference/packages/eden-git/tests` and `uv run pyright reference/packages/eden-git`; both passed (`50 passed`, `0 errors`). I also reran the prior graft-file repro manually, and the current code now correctly reports `is_ancestor False` and `parents []`. Plan adherence still looks good, the 7a surface is sufficient for 7b’s §3.2/§3.4 needs, and I don’t see any remaining correctness, integration, robustness, or code-quality issues worth blocking on.