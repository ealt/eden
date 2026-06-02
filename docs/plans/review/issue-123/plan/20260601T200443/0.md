# Issue #123 — Variant lineage tree visualization (objective-colored, hover-preview, click-to-detail)

GitHub issue: [#123](https://github.com/ealt/eden/issues/123). Labels: `enhancement`, `manual-ui`, `priority:2-planned`.

## 1. Context

Finding a "promising variant to build on" is today a three-tool loop: Adminer for ranked metrics, Forgejo for code state, the web-ui ideator for drafting. The experiment's evolutionary structure — which variants spawned which ideas, which lineages are productive, which dead-ended — is fully present in the task store but has no visual surface. This issue adds a read-only web-ui page that renders the experiment as a directed graph: nodes are variants (plus the seed root), edges are parent links, color heat-maps the objective score, hover previews, click navigates to the existing variant-detail page.

**This is a pure web-ui rendering chunk.** The data is all there and already wire-observable (`list_variants`, `read_variant`, `read_idea`, `read_experiment`); variants carry `parent_commits` directly ([`variant.py`](../../reference/packages/eden-contracts/src/eden_contracts/variant.py)), so the lineage DAG is reconstructable from a single `list_variants()` scan plus the seed SHA. The work lives entirely in the rendering layer (D3 integration, layout, hover/click, export, deep links) and one genuinely new component: the **first objective-expression evaluator** in the reference impl (§4 D.2). Two precedents establish the shape — [12a-1c task-transparency](eden-phase-12a-1c-task-transparency.md) (read-only admin extensions, one-hop lineage) and [#137 pending-task redesign](issue-137-pending-task-list-redesign.md) (web-ui-module-only, "no spec chapter, JSON schema, Pydantic model, wire binding, or store query changes").

The Forgejo deep links on the variant-detail page (issue's "bundled with this") ride along because they make the click→detail navigation actually useful, and they share the same operator-facing-URL plumbing the tree's export/share story wants.

## 2. Decisions captured before drafting

These shape scope and the naming map. They follow the issue's recommendations where it expresses a lean; where it leaves a fork open, the default selected is stated with its rationale and the alternative. **All are open to override at plan-PR review** — surfaced here so codex-review and the operator can challenge them rather than miss them in the diff.

1. **Server builds the graph + scores; D3 only lays out and colors.** A plain-Python `build_lineage_graph(...)` produces the node/edge list with each node's pre-computed objective score, status, label, and hrefs; the page embeds that as JSON and a vendored D3 glue script renders it. Rationale: keeps the objective evaluator and graph construction in **testable Python** (mirroring the [`_lineage.py`](../../reference/services/web-ui/src/eden_web_ui/routes/_lineage.py) "plain Python, no FastAPI coupling, unit-tested" discipline) and avoids reimplementing an expression evaluator in JavaScript. The only untested-by-pytest surface is the D3 layout glue (covered by manual verification + the smoke render check, §8). Alternative (ship raw variants, compute everything client-side) rejected: untestable, duplicates the evaluator in JS.

2. **Build a safe `ast`-based objective evaluator; this is net-new (§4 D.2).** Nothing in the reference impl evaluates `objective.expr` — it is opaque/implementation-defined ([`02-data-model.md`](../../spec/v0/02-data-model.md) §2.2: "The expression language is implementation-defined"), and even [`target_condition_policy`](../../reference/packages/eden-dispatch/src/eden_dispatch/termination.py) deliberately narrows to a single metric ("the legacy field accepted a full expression; the reference policy keeps the surface simple"). The evaluator supports the issue's "minimal grammar: arithmetic on metric names + numeric literals" via Python's `ast` module with a node-type allowlist — **never `eval()`**. On any failure (parse error, unknown metric, variant unevaluated, non-numeric, divide-by-zero) the node falls back to **the first metric in `evaluation_schema`** (insertion/YAML order — `evaluation_schema` is an ordered dict, [`evaluation.py`](../../reference/packages/eden-contracts/src/eden_contracts/evaluation.py)); if that too is unavailable the node is colorless/grey. `objective.direction` (`maximize`/`minimize`) sets heat polarity. **Scope decision: the evaluator lives in the web-ui** (tightest scope; the issue frames it as a viz concern). It is broadly useful — the orchestrator's target policy historically accepted a full expression — but wiring it into the orchestrator is explicitly out of scope; surface as a future-promotion candidate, do not build it shared now.

3. **The page is admin-gated at `/admin/lineage/`.** All `/admin/*` paths require `admins`-group membership ([`middleware.py`](../../reference/services/web-ui/src/eden_web_ui/middleware.py), issue #144 closed the read-side leak). The lineage view reads the same data class as admin observability (variants/ideas/experiment), so it belongs behind the same gate. **Tension the issue surfaces:** it asks for a link from the `/ideator/` page, but ideators may be non-admin. Default: render the ideator-page link unconditionally; a non-admin clicking it hits the standard 403 "ask to be added to `admins`" page. In the single-operator dogfooding norm the operator plays every role as an admin, so this is a non-issue in practice. **Alternative (surface for review):** a separate non-admin-gated `/lineage/` route readable by any authenticated worker, since the page is read-only. If review prefers ideators to see the tree without admin rights, that is the one-line route-prefix change — call it out before impl. *(Naming/scope note: this is a documented default, not a deadlock; if review deadlocks on it, escalate per the task's stop conditions.)*

4. **Force-directed layout (`d3-force`) for v0; defer the linearize-single-parent pre-pass.** Matches the issue's stated lean ("Lean force-directed for the generic case"). `d3-force` handles arbitrary DAGs including multi-parent merges with the least code. The "linearize single-parent paths" tidy-up the issue floats is deferred to a follow-up issue (filed at chunk completion per the deferral-tracking rule), not built in v0.

5. **SVG export in-scope; PNG export best-effort.** SVG export (serialize the rendered `<svg>` → download) is dependency-free and produces the self-contained shareable record the issue wants. PNG export (render SVG onto a `<canvas>` → `toBlob`) is fiddlier (CSS inlining, font tainting) — ship it best-effort; if it proves unreliable in the target browsers, defer with a filed issue rather than block the chunk. Both are client-side, no new server deps.

6. **Forgejo deep links use a new operator-supplied browse-base URL, no fictional default.** The web-ui has `app.state.clone_url` (a *git clone* URL, possibly credentialed / `.git`-suffixed) — not a browse base. Rather than fragile derivation, add an operator-facing browse-base config: a `--forgejo-web-url` CLI flag honoring the `FORGEJO_CLONE_URL_HOST` env var (the latter is already referenced in [`executor_claim.html`](../../reference/services/web-ui/src/eden_web_ui/templates/executor_claim.html) but unimplemented in [`cli.py`](../../reference/services/web-ui/src/eden_web_ui/cli.py)). Per the AGENTS.md substrate-values rule, **no fictional default**: when unset, the deep links simply don't render (and a variant with no `commit_sha` never gets a "View on Forgejo" link). Bundled into this chunk because the issue asks and it shares the operator-URL plumbing.

7. **Root node from `app.state.base_commit_sha`; no dependency on #122.** The web-ui already receives the seed SHA via `--base-commit-sha` ([`app.py`](../../reference/services/web-ui/src/eden_web_ui/app.py):124), used today for the ideator parent-commit hints. The tree's colorless root is sourced from there. When #122 (evaluatable baseline) lands, the seed becomes a real `kind="baseline"` variant and is colored like any other node with no change here. When `base_commit_sha` is absent, the root is omitted and any variant whose parent resolves to nothing renders as a dangling/orphan-parented node (handled gracefully, §4 D.3).

## 3. Background facts established by exploration

Pinned so the design is auditable against actual surfaces, not inference.

- **`objective`** = `{expr: string (minLength 1), direction: "maximize"|"minimize"}` ([`experiment-config.schema.json`](../../spec/v0/schemas/experiment-config.schema.json):20-33; [`config.py`](../../reference/packages/eden-contracts/src/eden_contracts/config.py) `ObjectiveSpec`). `expr` is a "scalar expression over fields of evaluation_schema"; the language is implementation-defined and **no evaluator exists anywhere in the reference impl** (verified across `eden-dispatch`; termination policies operate on single metric keys).
- **`evaluation_schema`** = ordered `dict[str, "integer"|"real"|"text"]` ([`evaluation.py`](../../reference/packages/eden-contracts/src/eden_contracts/evaluation.py) RootModel). "First metric" = first dict key in YAML/insertion order; there is no normative "first," so the plan defines it (insertion order) and states it.
- **`Variant`** ([`variant.py`](../../reference/packages/eden-contracts/src/eden_contracts/variant.py)) carries `parent_commits: list[CommitSha]` (≥1) **directly** — no need to read the idea to build edges. Other fields: `variant_id`, `idea_id` (required today; #122 would relax), `status` ∈ `{starting, success, error, evaluation_error}`, `commit_sha` (work-branch tip, nullable), `variant_commit_sha` (canonical-lineage SHA, set only by integrator, nullable), `evaluation: dict[str,Any]|None`, `branch`, `started_at`, `completed_at`, `executed_by`, `evaluated_by`. There is **no `slug` field on Variant** — the issue's "variant slug" label must derive from `branch` (the `work/<slug>` shape) or fall back to a truncated `variant_id`. Confirm during impl; the node label is `branch`-derived-slug + `variant_id[:8]`.
- **Parent resolution**: a parent SHA in `parent_commits` names either the seed (`base_commit_sha`) or another completed variant's `variant_commit_sha` ([`02-data-model.md`](../../spec/v0/02-data-model.md) §5.2). So the SHA→node index keys on `variant_commit_sha` (+ the seed); a non-integrated variant (no `variant_commit_sha`) cannot yet be anyone's parent. Issue's edge claim confirmed.
- **`status` enum has no "orphaned"**; "orphaned" is a *derived* display state — `status == "starting"` AND the variant's idea has a terminal execution task ([`observability.py`](../../reference/services/web-ui/src/eden_web_ui/routes/admin/observability.py):270). The tree reuses that derivation for visual consistency.
- **Experiment runtime object has no `base_commit_sha`** on `main` ([`experiment.schema.json`](../../spec/v0/schemas/experiment.schema.json), [`experiment.py`](../../reference/packages/eden-contracts/src/eden_contracts/experiment.py)) — #122 proposes it, not landed. The web-ui's `app.state.base_commit_sha` is the substitute source (decision 7), so this chunk does **not** depend on #122.
- **Web-ui plumbing**: `make_app` already threads `base_commit_sha` and `clone_url` onto `app.state` ([`app.py`](../../reference/services/web-ui/src/eden_web_ui/app.py):86,123-124); CLI flags `--clone-url` / `--forgejo-url` / `--base-commit-sha` exist ([`cli.py`](../../reference/services/web-ui/src/eden_web_ui/cli.py)). Admin router is assembled in [`routes/admin/__init__.py`](../../reference/services/web-ui/src/eden_web_ui/routes/admin/__init__.py) (`include_router` per sub-module). Handlers read `request.app.state.store` and render via `request.app.state.templates.TemplateResponse(request, name, ctx)`.
- **Static assets**: plain vendored files, no build step. `static/` has `htmx-1.9.12.min.js` + `style.css`. **D3 is not vendored.** No CSP middleware exists (inline scripts permitted), but the glue lives in a vendored file for cleanliness. Compose runs offline → **vendor D3, no CDN.** [`base.html`](../../reference/services/web-ui/src/eden_web_ui/templates/base.html) has only `title`/`main` blocks → add a `{% block scripts %}` before `</body>`.
- **Variant-detail template** ([`admin_variant_detail.html`](../../reference/services/web-ui/src/eden_web_ui/templates/admin_variant_detail.html)) has **no** Forgejo links today; the handler ([`observability.py`](../../reference/services/web-ui/src/eden_web_ui/routes/admin/observability.py) `variant_detail`) does not pass a clone/browse URL into the context.
- **Tests**: [`reference/services/web-ui/tests/`](../../reference/services/web-ui/tests/) has **no `__init__.py`**; tests use bare `from conftest import …`. Reusable fixtures: `seed_evaluate_task`, `seed_implement_task`, `BASE_SHA_FIXTURE`, `get_csrf`, in-memory `store`/`app`/`client`. Basenames `test_admin_variants_lineage.py`, `test_admin_tasks_lineage.py`, `test_lineage_helpers.py` already exist → new test basenames MUST be unique (see §5).

## 4. Design

### D.1 Graph model + builder (`lineage_graph.py`, plain Python)

A new top-level web-ui helper module `lineage_graph.py` (sibling of [`artifacts.py`](../../reference/services/web-ui/src/eden_web_ui/artifacts.py); distinct from the per-page one-hop [`_lineage.py`](../../reference/services/web-ui/src/eden_web_ui/routes/_lineage.py)), with frozen-dataclass view models and a single public builder:

- `LineageNode`: `id` (variant_id, or `"__seed__"` for root), `label` (slug + `variant_id[:8]`), `status` (`starting`/`success`/`error`/`evaluation_error`/`orphaned`/`seed`), `score: float | None`, `score_source` (`"objective"`/`"fallback_metric"`/`None`), `key_metrics: dict`, `href` (`/admin/variants/<id>/` or `None` for seed), `commit_sha`, `variant_commit_sha`.
- `LineageEdge`: `source` (parent node id), `target` (child node id).
- `LineageGraph`: `nodes`, `edges`, `score_min`/`score_max` (for client normalization), `direction`, `transport_errors`, `dangling_parents` (count of parent SHAs that resolved to no node — surfaced as a "lineage may be incomplete" banner, same posture as `_lineage.py`).
- `build_lineage_graph(store, *, base_commit_sha, config) -> LineageGraph`: one `list_variants()` scan; build a SHA→node index from `{variant_commit_sha: node}` plus the seed; for each variant add edges from each `parent_commits` entry's resolved node; compute each node's score via the objective evaluator (D.2); derive `orphaned` via the observability.py rule (sharing/replicating its `exec_terminal_by_idea` computation). Transport-shaped read failures are caught per-call and counted (not fatal), matching `_lineage.py`.

The builder emits a JSON-serializable dict (`graph.to_json()`); the route embeds it via `<script type="application/json" id="lineage-data">{{ graph_json|safe }}</script>` (Jinja `|tojson` on the dict → no manual escaping, XSS-safe) and the glue script `JSON.parse`s `#lineage-data`.

### D.2 Objective evaluator (`objective_eval.py`, net-new, safe)

`objective_eval.py` with:

- `evaluate_objective_expr(expr: str, metrics: Mapping[str, float]) -> float`: `ast.parse(expr, mode="eval")`, then a recursive evaluator over a **strict node allowlist** — `Expression`, `BinOp` (`Add`/`Sub`/`Mult`/`Div`/`Pow`/`Mod`), `UnaryOp` (`UAdd`/`USub`), `Constant` (numeric only), `Name` (resolved against `metrics`; unknown → raise), and parentheses (implicit in the AST). Any other node type → `ObjectiveEvalError`. **No `eval`, no `__builtins__`, no attribute access, no calls.** Divide-by-zero / non-finite → error.
- `objective_score_for_variant(variant, config) -> tuple[float | None, str | None]`: returns `(score, source)`. If `variant.evaluation` is present and numeric metrics extract cleanly, try `evaluate_objective_expr(config.objective.expr, metrics)` → `(score, "objective")`. On any failure, fall back to the **first key in `config.evaluation_schema`** present-and-numeric in `evaluation` → `(value, "fallback_metric")`. Else `(None, None)` (colorless).

This module is the highest-risk, highest-value unit and gets the densest unit tests (§8.2): valid arithmetic, precedence, unary minus, unknown-metric rejection, malformed-expr rejection, divide-by-zero, attribute/call/comprehension/lambda **rejection** (security), fallback paths, and `maximize`/`minimize` polarity downstream. Place it under the web-ui (decision 2); add a `# NOTE` that it is a promotion candidate for `eden-dispatch` if the orchestrator's target policy later wants full-expression support.

### D.3 Route + template + client

- **Route**: new `routes/admin/lineage.py` — `router = APIRouter(prefix="/admin")`, `@router.get("/lineage/")` → session check (defense-in-depth; middleware gates), read `store` + `base_commit_sha` + `config` + `forgejo_web_url` from `app.state`, `build_lineage_graph(...)`, render `admin_lineage.html` with `graph_json`, `direction`, `dangling_parents`, `transport_errors`, `empty` (no variants). Registered in [`routes/admin/__init__.py`](../../reference/services/web-ui/src/eden_web_ui/routes/admin/__init__.py).
- **Template** `admin_lineage.html` (extends `base.html`): a `<div id="lineage">` container, the embedded `#lineage-data` JSON, a legend (status shapes + heat scale + colorless = unevaluated), the incomplete-lineage banner when `dangling_parents`/`transport_errors` > 0, an empty-state message, Export-SVG / Export-PNG buttons, and a `{% block scripts %}` loading `static/d3.v7.min.js` + `static/lineage-tree.js`.
- **Client** `static/lineage-tree.js`: parse `#lineage-data`; `d3-force` layout; color via a sequential scale over `[score_min, score_max]` inverted when `direction == "minimize"`, colorless for `score == null`; node shape/border by status (incl. `orphaned`, `seed`); hover tooltip (slug, status, score, key metrics, "click for detail →"); click → `node.href`; SVG/PNG export handlers. No external network.
- **`base.html`**: add `{% block scripts %}{% endblock %}` before `</body>`.

### D.4 Cross-page links

- [`admin_index.html`](../../reference/services/web-ui/src/eden_web_ui/templates/admin_index.html): a "variant lineage tree »" link in the variants section.
- [`admin_variants.html`](../../reference/services/web-ui/src/eden_web_ui/templates/admin_variants.html): a link above the table.
- [`ideator_list.html`](../../reference/services/web-ui/src/eden_web_ui/templates/ideator_list.html) (or `_parent_commits_hints.html`): a "see the lineage tree to pick a parent" link near the parent-commit hints (admin-gated target; decision 3).

### D.5 Forgejo deep links on variant detail (bundled)

- **Config**: `--forgejo-web-url` CLI flag honoring `FORGEJO_CLONE_URL_HOST` env ([`cli.py`](../../reference/services/web-ui/src/eden_web_ui/cli.py)); threaded onto `app.state.forgejo_web_url` ([`app.py`](../../reference/services/web-ui/src/eden_web_ui/app.py) `make_app` gains a `forgejo_web_url: str | None = None` param). No fictional default (decision 6).
- **Handler**: `variant_detail` ([`observability.py`](../../reference/services/web-ui/src/eden_web_ui/routes/admin/observability.py)) passes `forgejo_web_url` + the experiment id into the context.
- **Template** ([`admin_variant_detail.html`](../../reference/services/web-ui/src/eden_web_ui/templates/admin_variant_detail.html)): when `forgejo_web_url` set AND a SHA is present, render "View on Forgejo" → `<base>/eden/<exp>/src/commit/<variant_commit_sha or commit_sha>` and "Diff against parent" → `<base>/eden/<exp>/compare/<parent_commits[0]>..<sha>`. Null-safe: a `starting`/`error` variant with no `commit_sha` shows no link; verify the exact Forgejo browse path shape during impl against the running Forgejo (`/src/commit/`, `/compare/A..B`).

### D.6 Docs

- [`docs/observability.md`](../observability.md) §2.1: add a `/admin/lineage/` row to the admin-routes table; in the variant-detail section, mention the new Forgejo deep links.
- [`.claude/skills/eden-manual-ideator/SKILL.md`](../../.claude/skills/eden-manual-ideator/SKILL.md) Phase 1 (context-gathering): mention the lineage page as a first-look surface. *(In-repo skill copy is authoritative here per the explored path; the global toolchain-vault sync discipline does not apply to this repo-local manual-UI skill.)*
- [`CHANGELOG.md`](../../CHANGELOG.md) `[Unreleased]` entry + [`docs/roadmap.md`](../roadmap.md) one-liner pointing at the plan (per the #137 web-ui-module-only precedent).

## 5. Naming map (old → new)

| Surface | Identifier | Disposition |
|---|---|---|
| Route | `GET /admin/lineage/` (user-facing title "Variant lineage tree") | **new** |
| Route module | `routes/admin/lineage.py` | **new** |
| Graph builder + view models | `lineage_graph.py`: `build_lineage_graph`, `LineageGraph`, `LineageNode`, `LineageEdge` | **new** |
| Objective evaluator | `objective_eval.py`: `evaluate_objective_expr`, `objective_score_for_variant`, `ObjectiveEvalError` | **new** |
| Template | `templates/admin_lineage.html` | **new** |
| Static | `static/d3.v7.min.js` (vendored), `static/lineage-tree.js` | **new** |
| Config | `--forgejo-web-url` flag / `FORGEJO_CLONE_URL_HOST` env → `app.state.forgejo_web_url` | **new** |
| Tests | `test_objective_eval.py`, `test_lineage_graph.py`, `test_admin_lineage_tree.py` | **new** (basenames verified unique) |

No identifiers renamed or retired; the `rename-discipline` CI job is unaffected. **Naming-discipline notes** (validate against [`docs/glossary.md`](../glossary.md) before introducing): "lineage" and "variant" are canonical vocabulary. The structure is a **DAG** (multi-parent merges), so code identifiers use `lineage_graph` (accurate) while user-facing copy keeps the issue's "lineage tree". Distinct from the pre-existing `_lineage.py` (per-page one-hop helper) — the new module is whole-experiment; the basename `lineage_graph.py` avoids collision. No new role/verb/task-kind/submission/artifact, so the verb-noun-coherence table doesn't apply.

## 6. Migration / cleanup

EDEN is pre-external-user (CLAUDE.md no-backwards-compat-shims posture). This is purely additive — a new page, helpers, static assets, one new optional config flag, and link additions. Nothing is retired or migrated; no schema/data migration. The `--forgejo-web-url` flag is optional with no default (links degrade off when unset). No interaction with existing experiments' stored state.

## 7. Conformance impact

**Zero.** Per [`09-conformance.md`](../../spec/v0/09-conformance.md) §6 the suite asserts only what is observable through the chapter-7 HTTP binding. This chunk adds **no wire op, no schema, no contract model, no spec prose** — it reads existing wire-observable data (`list_variants`/`read_variant`/`read_idea`/`read_experiment`) and renders it. Web-ui rendering is not part of the IUT contract. No `§`-reference updates, no `CONFORMANCE_GROUP`, no new scenario. The objective evaluator is non-normative (the spec leaves the expression language implementation-defined). Confirmed against the #137 / 12a-1c precedents, both of which shipped web-ui-only with zero conformance impact.

## 8. Chunked execution plan (waves + validation gates)

Single chunk, three waves. Gate each wave on the literal AGENTS.md Commands (not narrowed subsets).

**Wave 1 — pure-Python core (testable):** `objective_eval.py` + `lineage_graph.py` + their unit tests (`test_objective_eval.py`, `test_lineage_graph.py`).

- Gate: `uv run pytest -q reference/packages/eden-contracts reference/services/web-ui`, `uv run ruff check .`, `uv run pyright`, `python3 scripts/check-complexity.py`. The evaluator's recursion must stay under the complexity gate (a flat node-type dispatch dict keeps CC bounded; `# slop-allow:` only if genuinely warranted).

**Wave 2 — route + template + client + cross-links + Forgejo deep links:** `routes/admin/lineage.py`, `admin_lineage.html`, vendored `d3.v7.min.js` + `lineage-tree.js`, `base.html` scripts block, index/variants/ideator links, `--forgejo-web-url` plumbing (`cli.py`/`app.py`), `variant_detail` context + `admin_variant_detail.html` links. Tests: `test_admin_lineage_tree.py` (TestClient: 200, embedded `#lineage-data` JSON parses, node/edge counts for a seeded multi-variant + merge fixture, empty-state, admin-gate 403 for non-admin, dangling-parent banner) + variant-detail Forgejo-link tests (present when configured + SHA, absent otherwise).

- Gate: full `uv run pytest -q`, ruff, pyright, complexity-gate, markdownlint (templates aren't linted but touched docs are in wave 3).
- **Manual gate (irreducible):** spin up a stack (`eden-manual-experiment` skill or `bash reference/compose/healthcheck/e2e.sh`), load `/admin/lineage/`, verify color heat-map, hover tooltip, click→detail, multi-parent edges, SVG export, PNG export, Forgejo links. This is the only way to validate the D3/export client code.

**Wave 3 — docs + review record:** `docs/observability.md`, `eden-manual-ideator/SKILL.md`, `CHANGELOG.md` `[Unreleased]`, `docs/roadmap.md` one-liner; commit the impl-stage codex-review record under `docs/plans/review/issue-123/impl/<timestamp>/`. File deferral issues (linearize-single-parent pre-pass; PNG export if it proved unreliable) and reference them in the CHANGELOG entry.

- Gate: `npx --yes markdownlint-cli2@0.14.0 …`, re-run the full pytest + smoke quartet (`smoke.sh` / `smoke-subprocess.sh` / `e2e.sh`) since the page renders under the live stack and the new admin route must not regress quiescence/render.

## 9. Risks / tricky areas

- **9.1 Objective evaluator safety (load-bearing).** A naive `eval()` is a code-injection hole. The `ast`-allowlist approach is the catch; the rejection tests (call/attribute/lambda/comprehension/dunder) are mandatory, not optional. This is the single component most worth adversarial review.
- **9.2 Variant has no `slug` field.** The node label must derive from `branch` (`work/<slug>`) or fall back to `variant_id[:8]`. Confirm during impl; don't assume a `slug` attribute exists (it doesn't — §3).
- **9.3 Parent-SHA resolution edge cases.** Parents key on `variant_commit_sha` (integrated) + the seed. Non-integrated variants can't be parents; a parent SHA matching nothing is a *dangling parent* — surface it (banner + count), never crash. Self-parent / cycles shouldn't occur (lineage is a DAG by construction) but the builder must not infinite-loop if data is malformed; the builder is a single non-recursive pass over `list_variants`, so cycles can't hang it.
- **9.4 "orphaned" derivation drift.** Reuse observability.py's exact rule (`starting` + idea's exec task terminal) so the tree and the variants table agree; if the rule lives inline in observability.py, lift it to a shared helper to avoid two divergent copies.
- **9.5 Forgejo browse-path shape.** `/src/commit/<sha>` and `/compare/A..B` are Forgejo/Gitea conventions — verify against the running Forgejo in the manual gate; the clone URL is NOT the browse base (decision 6), so don't derive one from the other.
- **9.6 D3 offline + version pin.** Vendor a specific D3 build (`d3.v7.min.js`) into `static/`; no CDN (Compose is offline). Pin the version in the filename so upgrades are explicit.
- **9.7 Large experiments.** 100s of nodes embedded as JSON + force-directed layout is acceptable for v0 (filtering/search is out of scope per the issue). If a target experiment is much larger, that's a future perf issue, not a v0 blocker — note it, don't pre-optimize.
- **9.8 Admin-gate vs ideator link (decision 3).** If review flips to an open `/lineage/` route, the route prefix and the middleware exemption change; keep the route handler's logic prefix-agnostic so the flip is a one-line move.
- **9.9 Smoke/e2e blast radius.** A new admin GET route adds no orchestrator work and no new always-on poller, so the smoke count assertions (`≥` lower-bounds) are unaffected. The risk is only a render-time exception under the live stack (e.g. a null-safety miss on a real variant shape) — the wave-3 smoke re-run + manual gate is the catch.

## 10. Files to touch

**New (web-ui):**

- `reference/services/web-ui/src/eden_web_ui/objective_eval.py`
- `reference/services/web-ui/src/eden_web_ui/lineage_graph.py`
- `reference/services/web-ui/src/eden_web_ui/routes/admin/lineage.py`
- `reference/services/web-ui/src/eden_web_ui/templates/admin_lineage.html`
- `reference/services/web-ui/src/eden_web_ui/static/d3.v7.min.js` (vendored)
- `reference/services/web-ui/src/eden_web_ui/static/lineage-tree.js`
- `reference/services/web-ui/tests/test_objective_eval.py`
- `reference/services/web-ui/tests/test_lineage_graph.py`
- `reference/services/web-ui/tests/test_admin_lineage_tree.py`

**Modified (web-ui):**

- `routes/admin/__init__.py` — register the lineage router.
- `routes/admin/observability.py` — `variant_detail` passes `forgejo_web_url` + exp id; possibly lift the `orphaned` derivation to a shared helper (§9.4).
- `templates/base.html` — `{% block scripts %}`.
- `templates/admin_index.html`, `templates/admin_variants.html`, `templates/ideator_list.html` (or `_parent_commits_hints.html`) — links.
- `templates/admin_variant_detail.html` — Forgejo deep links.
- `cli.py`, `app.py` — `--forgejo-web-url` / `FORGEJO_CLONE_URL_HOST` → `app.state.forgejo_web_url`.

**Docs:**

- `docs/observability.md` (§2.1 + variant-detail Forgejo mention), `.claude/skills/eden-manual-ideator/SKILL.md`, `CHANGELOG.md`, `docs/roadmap.md`.

**Spec / schemas / contracts / conformance:** none (§7).

Approximate diff: ~120 lines `objective_eval.py` + tests, ~140 `lineage_graph.py` + tests, ~80 route + template + route tests, ~250 vendored D3 glue (`lineage-tree.js`; D3 itself is vendored verbatim), ~60 Forgejo-link plumbing + tests, ~40 docs.
