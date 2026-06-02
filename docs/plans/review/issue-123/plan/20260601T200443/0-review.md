**Missing Context**

Assessment: Mostly strong. The problem is well-defined and the plan is grounded in actual repo surfaces. I buy the “zero spec/schema/conformance impact” claim in [§7](</Users/ericalt/Documents/eden-worktrees/plan-issue-123-variant-lineage-tree/docs/plans/issue-123-variant-lineage-tree.md:112>) as long as the evaluator stays web-ui-local and explicitly non-normative.

- The plan should call out the existing config-drift limitation more directly. The graph colors depend on `objective` and `evaluation_schema`, but the web-ui reads those from its startup YAML, not from a store-authoritative runtime source ([plan](</Users/ericalt/Documents/eden-worktrees/plan-issue-123-variant-lineage-tree/docs/plans/issue-123-variant-lineage-tree.md:17>), [cli help](</Users/ericalt/Documents/eden-worktrees/plan-issue-123-variant-lineage-tree/reference/services/web-ui/src/eden_web_ui/cli.py:62>)). If that file drifts from the task-store’s config, the visualization can be wrong while still “working.”
- “Zero contract impact” is too broad unless you narrow what “contract” means. This chunk introduces the first concrete reference-impl interpretation of `objective.expr`; that is not spec impact, but it is new operator-visible semantics and should be documented somewhere operator-facing.

**Feasibility**

Assessment: The feature is implementable, but two parts do not work as written.

- The browse-URL plan in [decision 6](</Users/ericalt/Documents/eden-worktrees/plan-issue-123-variant-lineage-tree/docs/plans/issue-123-variant-lineage-tree.md:27>) is wrong to reuse `FORGEJO_CLONE_URL_HOST`. That variable is explicitly a credential-bearing clone URL, not a browse base ([.env.example](</Users/ericalt/Documents/eden-worktrees/plan-issue-123-variant-lineage-tree/reference/compose/.env.example:29>), [compose.yaml](</Users/ericalt/Documents/eden-worktrees/plan-issue-123-variant-lineage-tree/reference/compose/compose.yaml:451>)). Reusing it for “View on Forgejo” either yields malformed browse links or leaks credentials into operator-facing hrefs. This needs a distinct `FORGEJO_WEB_URL`/browse-base surface.
- The JSON embedding sketch in [§4 D.1](</Users/ericalt/Documents/eden-worktrees/plan-issue-123-variant-lineage-tree/docs/plans/issue-123-variant-lineage-tree.md:57>) is internally contradictory: `graph_json|safe` and “use Jinja `|tojson`” are not the same thing. For a page that may embed text metrics, `|safe` is the wrong default. Pass a Python dict and render it with `|tojson` inside the `<script type="application/json">` block, then read `textContent` client-side.

**Alternatives**

Assessment: The current objective-fallback strategy is the main design problem.

- Falling back to “first metric in `evaluation_schema`” on any evaluator failure ([decision 2](</Users/ericalt/Documents/eden-worktrees/plan-issue-123-variant-lineage-tree/docs/plans/issue-123-variant-lineage-tree.md:19>), [§4 D.2](</Users/ericalt/Documents/eden-worktrees/plan-issue-123-variant-lineage-tree/docs/plans/issue-123-variant-lineage-tree.md:63>)) is the wrong tradeoff. It silently mixes incompatible semantics in one heat map: some nodes are objective scores, others are an arbitrary fallback metric. Parse errors and unknown names are experiment-global config bugs, not per-node data gaps. Better shape: compile/validate the objective once, show a loud banner and gray nodes if the expression is invalid, and reserve per-node gray only for missing/non-numeric metrics. That also aligns better with the spec’s “reject absent metric names” posture for conforming implementations ([spec](</Users/ericalt/Documents/eden-worktrees/plan-issue-123-variant-lineage-tree/spec/v0/02-data-model.md:62>)).
- The “minimal grammar” claim in [decision 2](</Users/ericalt/Documents/eden-worktrees/plan-issue-123-variant-lineage-tree/docs/plans/issue-123-variant-lineage-tree.md:19>) does not match the allowlist in [§4 D.2](</Users/ericalt/Documents/eden-worktrees/plan-issue-123-variant-lineage-tree/docs/plans/issue-123-variant-lineage-tree.md:63>), which adds `Pow` and `Mod`. Unless the issue explicitly needs them, I would keep v0 to `+ - * /` and parentheses.

**Completeness**

Assessment: The validation plan is not adequate yet for a JS-heavy page.

- The wave gates in [§8](</Users/ericalt/Documents/eden-worktrees/plan-issue-123-variant-lineage-tree/docs/plans/issue-123-variant-lineage-tree.md:116>) overclaim what the existing automation covers. `pytest` can validate the server-built graph and HTML shell, but it cannot catch broken `lineage-tree.js`, missing D3 asset wiring, broken export handlers, or a page that only fails after JS runs.
- The cited live-stack check does not currently exercise this page. [`e2e.sh`](</Users/ericalt/Documents/eden-worktrees/plan-issue-123-variant-lineage-tree/reference/compose/healthcheck/e2e.sh:8>) drives the ideator/admin flows, but it does not visit `/admin/lineage/` or execute browser JS ([driver invocation](</Users/ericalt/Documents/eden-worktrees/plan-issue-123-variant-lineage-tree/reference/compose/healthcheck/e2e.sh:133>)). I’d add at least one automated live-stack assertion that signs in, loads `/admin/lineage/`, and verifies the page serves the embedded JSON plus both JS assets. Manual browser verification is still needed for force layout/hover/export, but it should not be the only runtime check.
- The wording “gate each wave on the literal AGENTS.md Commands” conflicts with the actual wave gates, which use narrowed subsets in waves 1 and 2 ([plan](</Users/ericalt/Documents/eden-worktrees/plan-issue-123-variant-lineage-tree/docs/plans/issue-123-variant-lineage-tree.md:118>), [AGENTS](</Users/ericalt/Documents/eden-worktrees/plan-issue-123-variant-lineage-tree/AGENTS.md:174>)). Those are fine as interim checkpoints; they just should not be described as the repo’s literal gate.

**Edge Cases and Risks**

Assessment: A few concrete cases should be pinned down before implementation.

- The variant-detail “Diff against parent” link in [§4 D.5](</Users/ericalt/Documents/eden-worktrees/plan-issue-123-variant-lineage-tree/docs/plans/issue-123-variant-lineage-tree.md:85>) picks `parent_commits[0]`. For a multi-parent variant, that is arbitrary and can mislead. Either show it only for single-parent variants or label it explicitly as “diff against first parent.”
- The seed node id `__seed__` in [§4 D.1](</Users/ericalt/Documents/eden-worktrees/plan-issue-123-variant-lineage-tree/docs/plans/issue-123-variant-lineage-tree.md:52>) can collide with opaque `variant_id` values. Use a namespaced internal node id for every node, e.g. `seed:<sha>` and `variant:<variant_id>`.
- The client-side color scale needs an explicit rule for `score_min == score_max` and “only one scored node.” That case is not described in [§4 D.1](</Users/ericalt/Documents/eden-worktrees/plan-issue-123-variant-lineage-tree/docs/plans/issue-123-variant-lineage-tree.md:54>) or [§4 D.3](</Users/ericalt/Documents/eden-worktrees/plan-issue-123-variant-lineage-tree/docs/plans/issue-123-variant-lineage-tree.md:72>), and it is easy to end up with a degenerate or misleading heat map.

**Overall Assessment**

The plan has a solid foundation: the read-only/web-ui-local framing is right, the data-source analysis is good, and zero spec/schema/conformance impact is mostly defensible. I would revise it before implementation, though. The main fixes are: separate browse-base config from clone-URL config, make the JSON embedding explicitly `tojson`-safe, replace the current “fallback to first metric on any failure” strategy with a loud invalid-objective posture, and strengthen the validation story for the client-side JS.