**1. Missing context**

Assessment: Mostly solid. I verified the core reconciliation claims against the tree: [`scripts/conformance-coverage.py`](/Users/ericalt/Documents/eden-worktrees/plan-issue-185-spec-must-coverage/scripts/conformance-coverage.py:1) already exists, [`check_citations.py`](/Users/ericalt/Documents/eden-worktrees/plan-issue-185-spec-must-coverage/conformance/src/conformance/tools/check_citations.py:1) is inverse-direction only, CI has no forward-direction coverage step today, and a fresh generator run does produce the plan’s `191 / 579` and `216 uncovered`.

Issue:
- The baseline taxonomy is underspecified for the currently uncovered lines in `00-overview.md`, `01-concepts.md`, and `09-conformance.md`. The plan narrows reasons to `consequence` / `off-wire` / `restatement` / `todo` ([issue-185-spec-must-coverage.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-185-spec-must-coverage/docs/plans/issue-185-spec-must-coverage.md:52)), but the current manual audit explicitly calls out structurally coverage-immune chapters and uses a separate `meta` tag ([conformance-coverage.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-185-spec-must-coverage/docs/conformance-coverage.md:44), [conformance-coverage.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-185-spec-must-coverage/docs/conformance-coverage.md:60)). The plan should say whether those collapse into `restatement` or need their own baseline reason.

**2. Feasibility**

Assessment: The approach is feasible, but two details need correction before implementation.

Issues:
- The proposed stable key should not reuse `_trim_paragraph` as its normalization base. That helper is display-oriented and truncates at 220 chars (`line = line[: max_len - 1] + "…"`), so hashing its output can collide distinct long MUST lines or miss edits beyond the truncation point ([issue-185-spec-must-coverage.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-185-spec-must-coverage/docs/plans/issue-185-spec-must-coverage.md:142), [conformance-coverage.py](/Users/ericalt/Documents/eden-worktrees/plan-issue-185-spec-must-coverage/scripts/conformance-coverage.py:93)). Use a separate non-truncating normalization helper for identity.
- Reusing the existing `conformance` path filter is not enough for `coverage-gate`. The plan says no new bucket is needed ([issue-185-spec-must-coverage.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-185-spec-must-coverage/docs/plans/issue-185-spec-must-coverage.md:159)), but the current `conformance` filter does not include `scripts/**`; that lives under the `python` bucket ([ci.yml](/Users/ericalt/Documents/eden-worktrees/plan-issue-185-spec-must-coverage/.github/workflows/ci.yml:94), [ci.yml](/Users/ericalt/Documents/eden-worktrees/plan-issue-185-spec-must-coverage/.github/workflows/ci.yml:110)). A PR that changes only `scripts/conformance-coverage.py` or `scripts/conformance-coverage-baseline.json` would skip the new job. The gate needs `spec || conformance || python`, or a dedicated coverage filter.

**3. Alternatives**

Assessment: High-level choice looks right. Extending the existing generator and using a set-based ratchet is the right approach.

Suggestion:
- The main alternative worth adopting is a dedicated normalization helper for keying, not a separate tool.

**4. Completeness**

Assessment: Close, but not complete.

Issues:
- The close-out instructions conflict with repo policy. Wave 3 says to use the “planless-chunk shape” for the roadmap entry ([issue-185-spec-must-coverage.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-185-spec-must-coverage/docs/plans/issue-185-spec-must-coverage.md:186)), but this chunk has a plan file. [`AGENTS.md`](/Users/ericalt/Documents/eden-worktrees/plan-issue-185-spec-must-coverage/AGENTS.md:25) says planned chunks must point roadmap at the plan path; PR-link shape is only for planless work.
- The plan leans on reconciling the new baseline against the existing manual audit block ([issue-185-spec-must-coverage.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-185-spec-must-coverage/docs/plans/issue-185-spec-must-coverage.md:72)), but that block currently audits chapters 02-09 only, while the live uncovered set also includes 00, 01, 10, and 11. The plan should state explicitly that those chapters need fresh manual classification, not imply the current audit already covers them.

**5. Edge cases and risks**

Assessment: Good overall risk section.

Suggestion:
- The tool output for text-only spec edits should explicitly tell authors when `--write-baseline` is the expected fix. Otherwise the text-hash ratchet will be noisier than the plan implies.

Overall assessment: The plan’s core framing is correct, and its main claims about the current tree check out. I would not treat it as implementation-ready yet: fix the stable-key design, widen the CI trigger scope, and correct the chunk-closeout instructions first.