**Findings**

No new findings.

**Assessment**

The round-1 fixes look correct and complete against the round-0 issues:

- `parse_list_view()` now ignores `dir` unless `sort` itself is valid, so `?dir=asc` and `?sort=garbage&dir=asc` fall back to `priority DESC` as planned.
- `EligibilityResolver` now memoizes the full tri-state group outcome, including transport-unknown, so repeated failing probes for the same group cost one walk and one warning increment.
- Disabled-button copy now comes from registration-first eligibility classification rather than inferring from `row.target`, so unregistered workers no longer get the wrong tooltip.
- Parent-commit cross-reference text is now populated by both row builders and rendered by the shared partial.

I also re-ran the targeted suite:
`uv run pytest -q reference/services/web-ui/tests/test_executor_list.py reference/services/web-ui/tests/test_evaluator_list.py reference/services/web-ui/tests/test_executor_routes.py reference/services/web-ui/tests/test_evaluator_routes.py`
Result: `90 passed in 43.55s`.

Residual note: the new regression coverage was added on the executor side; evaluator symmetry for these specific round-1 fixes is mostly validated by shared-template/shared-helper inspection rather than fresh evaluator-specific assertions.