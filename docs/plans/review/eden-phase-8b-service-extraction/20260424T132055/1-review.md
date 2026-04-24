**Missing Context**

Assessment: materially improved. The updated plan now explains the subprocess worker profile, the repo ownership model, and the ID-allocation story well enough to review the design itself. I don’t see a remaining blocking missing-context issue.

**Feasibility**

Assessment: significant concern remains here, so I would stop at this level and not evaluate alternatives, completeness, or later risks yet.

- `CRITICAL` The new orphan-commit / empty-parents design conflicts with the current protocol and models. The plan says the planner emits `parent_commits=()` and the implementer writes an orphan `work/*` commit with no parents ([plan lines 317-335](/Users/ericalt/Documents/eden/docs/plans/eden-phase-8b-service-extraction.md:317)). But the current spec requires `parent_commits` to contain at least one SHA ([02-data-model.md](/Users/ericalt/Documents/eden/spec/v0/02-data-model.md:159), [02-data-model.md](/Users/ericalt/Documents/eden/spec/v0/02-data-model.md:166)), the role contract requires implementer writes to descend from those parents ([03-roles.md](/Users/ericalt/Documents/eden/spec/v0/03-roles.md:89)), and the Pydantic bindings enforce `min_length=1` for both proposals and trials ([proposal.py](/Users/ericalt/Documents/eden/reference/packages/eden-contracts/src/eden_contracts/proposal.py:31), [trial.py](/Users/ericalt/Documents/eden/reference/packages/eden-contracts/src/eden_contracts/trial.py:32)). As written, this plan cannot pass against the current contracts.

- `CRITICAL` Relatedly, the planner still has no source of a valid parent commit. Its CLI only exposes `--proposals-per-plan` ([plan lines 242-243](/Users/ericalt/Documents/eden/docs/plans/eden-phase-8b-service-extraction.md:242)), and the current experiment config fixture carries no starting commit SHA ([config.yaml](/Users/ericalt/Documents/eden/tests/fixtures/experiment/.eden/config.yaml:1)). So even if the empty-list issue were removed, the plan still does not give the planner enough information to produce spec-valid `parent_commits`.

Suggestion: keep the “real commit in shared repo” fix, but restore a non-empty parent lineage. The cleanest path is to seed the repo with a starting commit and have proposals carry that SHA (or an already-completed trial SHA later); then implementer `work/*` commits use that SHA as parent. If you don’t want planner repo access, pass the starting SHA explicitly via config/CLI, but `parent_commits` cannot be empty under the current spec.

**Overall Assessment**

This revision closes the earlier subprocess-boundary concerns and is much closer. The remaining blocker is now narrow but fundamental: the proposed repo model violates the current `parent_commits` contract, so the plan is still not implementation-ready until that lineage model is corrected.