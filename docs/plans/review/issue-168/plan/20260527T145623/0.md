# Issue #168 — Hierarchical artifacts substrate layout (by entity)

## 1. Context

Today's artifact substrate is a **flat directory** under
`${EDEN_EXPERIMENT_DATA_ROOT}/artifacts/`, surfaced as
`/var/lib/eden/artifacts/` inside containers:

```text
artifacts/
├── 47191ac5521e4a6586c9aa39acc24366.md
├── 6da6b33279ee48b5a7008734ce1da05b.md
├── aff32a9425a748d19ba2cd38af7f9cba.tar.gz
└── eval.md
```

Filenames are entity-ids (an `idea_id` or `variant_id`) with no grouping.
Operator browsing requires already knowing the entity-id and what kind of
artifact each file is. Discovered during the 2026-05-22 manual demo session;
the operator's intuition was "subdirectories would group naturally."

Issue #168 picks **Option B-refined** (entity-hierarchical layout):

```text
artifacts/
  ideas/
    <idea_id>/
      content.md                 # ideator's text content
      <bundle-or-uploads>        # multi-file ideas (issue #120)
  variants/
    <variant_id>/
      executor/                  # executor-produced artifacts (issue #164)
        <files>
      evaluator/                 # evaluator-produced artifacts
        <files>
```

The layout choice settled in the issue thread; this plan does **not**
re-litigate it (see §3 for the one filename-convention decision left open).

### 1.1 The layout is already half-built — and drifting

The single most important grounding fact for this chunk: the
**ideator subprocess host already writes the hierarchical idea path**, and
the spec binding already documents it, but the web-UI and CLI writers do
**not** follow it. The current state is an inconsistency, not a greenfield:

| Writer | Path produced today | Text filename |
|---|---|---|
| Ideator **subprocess** host ([`subprocess_mode.py:304`](../../reference/services/ideator/src/eden_ideator_host/subprocess_mode.py)) | `artifacts/ideas/<idea_id>/content.md` ✅ hierarchical | `content.md` |
| Web-UI **ideator** route ([`ideator.py:414`](../../reference/services/web-ui/src/eden_web_ui/routes/ideator.py)) | `artifacts/<idea_id>.md` ❌ flat | `idea.md` |
| Web-UI **evaluator** route ([`evaluator.py:47`](../../reference/services/web-ui/src/eden_web_ui/routes/evaluator.py)) | `artifacts/<artifact_id>.<ext>` ❌ flat | `evaluation.md` |
| `eden-manual` CLI ([`eden-manual:312`](../../reference/scripts/manual-ui/eden-manual)) | `artifacts/<artifact_id>.<ext>` ❌ flat | `idea.md` / `evaluation.md` |

The spec binding [`worker-host-subprocess.md`](../../spec/v0/reference-bindings/worker-host-subprocess.md)
§2.3 already states the subprocess host writes
`<artifacts_dir>/ideas/<idea_id>/content.md`. So one writer and the binding
prose are already hierarchical; the chunk's real work is making the **other
three writers consistent** and **extending the convention to variants**, then
syncing the binding prose.

There are two latent drifts to fix while here:

1. **Layout drift** — web-UI / CLI write flat; subprocess writes hierarchical.
2. **Text-filename drift** — web-UI ideator writes `idea.md`; subprocess host
   and the binding say `content.md`. Unify on `content.md` (the binding is
   authoritative).

### 1.2 The substrate is self-describing — no migration shim required

`artifacts_uri` on an `Idea` / `Variant` stores the **full** URI
(`file:///var/lib/eden/artifacts/<path>`). The web-UI serve route
([`routes/artifacts.py:149-165`](../../reference/services/web-ui/src/eden_web_ui/routes/artifacts.py))
resolves any path under the artifacts-dir jail (`is_relative_to(base)`),
and the admin listing ([`admin_artifacts.py:57-101`](../../reference/services/web-ui/src/eden_web_ui/routes/admin_artifacts.py))
`os.walk`s recursively. Neither cares whether a URI points at a flat file or a
nested one.

**Consequence:** changing the *writer's* layout does not break any
already-written artifact — its stored URI still resolves. New writes go
hierarchical; pre-existing flat URIs keep working untouched. This is the
no-backwards-compat-shim posture from CLAUDE.md done right: we add **no**
dual-read code path, no aliasing, no migration scaffolding. We change the
writers and the reading side is already layout-agnostic. (See §8.)

### 1.3 The layout is non-normative; this is a reference-binding change

[`08-storage.md`](../../spec/v0/08-storage.md) §5.1 states the artifact
store's naming scheme is **"implementation-defined."**
[`02-data-model.md`](../../spec/v0/02-data-model.md) §1.5 states `artifacts_uri`
values are deployment-local opaque URIs. So the **core protocol** says nothing
about physical layout — the only spec surface that names a concrete path is the
**reference binding** `worker-host-subprocess.md`. This keeps the spec impact
small (one binding doc) and the conformance impact near-zero (§9).

## 2. Decisions captured before drafting

Listed so codex-review and future maintainers see what was deliberate:

1. **Option B-refined is settled.** The entity-hierarchical layout
   (`ideas/<idea_id>/`, `variants/<variant_id>/{executor,evaluator}/`) was
   chosen in the issue thread. Not up for re-litigation absent a load-bearing
   contradiction with a spec MUST.

2. **No migration tooling, no dual-read shim.** Per §1.2 the substrate is
   self-describing; existing flat artifacts keep resolving. We ship the new
   layout for newly-created artifacts only. No `migrate-artifacts.sh`, no
   compat reader. EDEN is pre-external-deployment-base; the cost of a
   migration shim exceeds its value.

3. **This is a reference-impl + reference-binding change, not a core-protocol
   change.** No new normative MUST about physical layout; `08-storage.md`
   §5.1's "implementation-defined" stays. Only the reference binding's
   concrete-path prose changes.

4. **Executor `executor/` subdir is forward-looking.** No current code writes
   executor artifact *bytes* — the web-UI executor form accepts a user-supplied
   `artifacts_uri` string ([`executor.py:680`](../../reference/services/web-ui/src/eden_web_ui/routes/executor.py)),
   and the subprocess `outcome.json` carries no artifacts field
   ([`worker-host-subprocess.md`](../../spec/v0/reference-bindings/worker-host-subprocess.md) §3
   step 7). We establish the `executor/` path convention (path-builder + binding
   prose) so any future executor-upload writer targets it, but we do **not**
   add an executor-byte-upload writer in this chunk (that is #120-adjacent and
   out of scope — see §4).

5. **Filename-within-entity-dir convention preserves no-overwrite (§5.4).**
   See §3 — the open "decide during impl" question from the issue is resolved
   here, with the tradeoff documented for plan-review.

## 3. Design

### D.1 A single shared path-builder

The four writers currently each compute their own path. The chunk introduces
one path-builder that all writers call, so the layout lives in exactly one
place per language surface.

There are two language surfaces:

- **Python** — `eden_web_ui.artifacts` (web-UI ideator + evaluator routes) and
  the ideator subprocess host. Add an `entity_artifact_dir()` helper to
  [`eden_web_ui/artifacts.py`](../../reference/services/web-ui/src/eden_web_ui/artifacts.py)
  and have `write_artifact_bundle` / `predict_artifact_uri` take an
  entity-scoped base dir instead of the flat artifacts root.
- **Standalone CLI** — [`eden-manual`](../../reference/scripts/manual-ui/eden-manual)
  ships as a system-`python3` script and **cannot import** `eden_web_ui`
  (documented at [`scripted.py`-adjacent comment, `eden-manual:296-300`](../../reference/scripts/manual-ui/eden-manual)).
  Its `_write_artifact_for_role` already mirrors `write_artifact_bundle`
  by hand; the mirror gets the same path change. Keep the two in lockstep
  (the existing in-file comment already warns about this).

The path-builder maps `(role, entity_id)` → directory:

| Producing role | Entity | Directory under `artifacts/` |
|---|---|---|
| ideator | idea | `ideas/<idea_id>/` |
| executor | variant | `variants/<variant_id>/executor/` |
| evaluator | variant | `variants/<variant_id>/evaluator/` |

### D.2 Filename-within-entity-dir convention (the one open decision)

The issue left "collision policy within a single entity dir" as "decide during
impl." This is load-bearing because of the **no-overwrite durability rule**
([`08-storage.md`](../../spec/v0/08-storage.md) §5.4: once a protocol object
references an artifact URI, the deployment MUST NOT overwrite the content).

Two candidate conventions:

- **(A) Fixed role-coherent filenames** — `ideas/<idea_id>/content.md`,
  `variants/<variant_id>/evaluator/evaluation.md`, bundles as `bundle.tar.gz`.
  Cleanest browsing. **But** the evaluator can resubmit (chapter 11c:
  evaluator-resubmit equivalence is `variant_id`+`status`+`metrics`, and
  `artifacts_uri` is explicitly **excluded** — so a corrected resubmit can carry
  a *different* artifact). Two evaluator submissions for the same `variant_id`
  would both target `variants/<variant_id>/evaluator/evaluation.md` → the second
  overwrites the first. If the first URI was already stamped onto the variant's
  evaluation manifest, that is a §5.4 violation.

- **(B) Unique stem within the entity dir** — keep today's unique
  `artifact_id` (a fresh `uuid4().hex` per submission, see
  [`ideator.py:386`](../../reference/services/web-ui/src/eden_web_ui/routes/ideator.py))
  as the *filename stem*, just nested under the entity dir:
  `variants/<variant_id>/evaluator/<artifact_id>.md`. No overwrite is ever
  possible; the directory still provides the entity grouping the issue wants
  ("what does variant X have?" → `ls variants/<variant_id>/`).

**Recommendation: a hybrid.** Ideas are created exactly once, so an idea's dir
is already unique — use the clean fixed name `ideas/<idea_id>/content.md`
(matching the subprocess host + binding; no resubmit path exists for an idea).
For the **evaluator** (the one resubmittable producer), keep a unique stem to
preserve §5.4: `variants/<variant_id>/evaluator/<artifact_id>.<ext>` (or
`<artifact_id>.tar.gz` for bundles). The executor subdir, when a byte-writer
eventually exists, follows the evaluator's unique-stem rule for symmetry.

This is the one decision in this plan that deviates from the issue's literal
sketch (which showed `evaluator/<filename>`). The deviation is the
§5.4-preserving refinement; **flagged here for plan-review and codex** — if the
reviewer prefers fixed filenames everywhere, the alternative is to make the
writer refuse to overwrite (raise rather than clobber) and let the form surface
a "artifact already exists for this submission" error. Either is defensible;
the unique-stem hybrid is the lower-risk default because it cannot violate §5.4
even if a future caller forgets the guard.

> **Surface to operator at plan-review:** confirm the hybrid
> (idea → `content.md`; evaluator/executor → `<artifact_id>.<ext>` under the
> source subdir) vs. fixed-names-everywhere-with-no-overwrite-guard.

### D.3 Text-filename unification

Unify the ideator text filename on **`content.md`** (the binding §2.3 value and
the subprocess host's value). The web-UI ideator route currently passes
`text_filename="idea.md"` ([`ideator.py:418`](../../reference/services/web-ui/src/eden_web_ui/routes/ideator.py));
change to `content.md`. The evaluator's text entry stays `evaluation.md`
(role-coherent; no competing convention to reconcile).

This only affects the *entry name inside a bundle* and the single-text-file
name; it does not change any wire field.

### D.4 URI shape after the change

`eden-manual` stamps the container-internal path
(`CONTAINER_ARTIFACTS_DIR / name`, [`eden-manual:351`](../../reference/scripts/manual-ui/eden-manual)).
After the change a stamped idea URI becomes
`file:///var/lib/eden/artifacts/ideas/<idea_id>/content.md`. The host-translate
helper `_translate_artifacts_uri_to_host`
([`eden-manual:157-178`](../../reference/scripts/manual-ui/eden-manual)) uses
`relative_to(CONTAINER_ARTIFACTS_DIR)` then re-joins onto the host dir — it
handles nested paths transparently (it walks the whole relative path, not just
the basename). No change needed there; verify with a nested-path unit case.

### D.5 Spec binding update

[`worker-host-subprocess.md`](../../spec/v0/reference-bindings/worker-host-subprocess.md)
§2.3 already documents `<artifacts_dir>/ideas/<idea_id>/content.md`. Two edits:

1. Add the variant-side convention to the binding prose (executor/evaluator
   subdirs), even though the subprocess executor/evaluator don't write bytes
   today — the binding is where the reference layout is documented, and a future
   byte-writer should find the convention there.
2. Add a one-line note in [`08-storage.md`](../../spec/v0/08-storage.md) §5.1
   pointing at the reference binding as the reference deployment's concrete
   naming scheme (keeping §5.1's "implementation-defined" intact — the note is
   informational, naming where the reference impl pins its choice).

No JSON-schema, Pydantic-model, or wire-binding field changes — `artifacts_uri`
remains an opaque URI string on every surface (§5).

## 4. Scope

**In scope:**

- One shared Python path-builder in
  [`eden_web_ui/artifacts.py`](../../reference/services/web-ui/src/eden_web_ui/artifacts.py);
  web-UI ideator + evaluator routes write hierarchical.
- The hand-mirrored path change in
  [`eden-manual`](../../reference/scripts/manual-ui/eden-manual)
  `_write_artifact_for_role`.
- Ideator subprocess host: already hierarchical for ideas; audit for the
  `content.md` filename + variant convention consistency (likely a no-op for
  the write path, plus the binding-prose sync).
- Text-filename unification on `content.md` (web-UI ideator).
- Spec binding `worker-host-subprocess.md` §2.3 + new variant-subdir prose;
  one informational note in `08-storage.md` §5.1.
- Tests: update fixtures/assertions that hard-code flat paths; add a
  path-builder unit test asserting the three entity→dir mappings and the
  no-overwrite stem for the evaluator; nested-path round-trip test for the
  CLI host-translate helper.
- `docs/glossary.md` audit (§5 naming map) — confirm the subdir vocabulary.

**Out of scope (deferred — file as issues per CLAUDE.md deferral rule if a
deferral phrase appears in the CHANGELOG entry at impl time):**

- **Executor artifact-byte upload writer.** No current code writes executor
  bytes; the `executor/` subdir is a documented convention only. A web-UI
  executor multi-file upload form (the writer that would populate it) is
  #120-adjacent and out of scope here.
- **Migration of legacy flat artifacts.** Per §2 decision 2 — leave in place;
  no shipped tooling.
- **The #166 wire-level opaque-URI endpoint.** Once #166 lands, physical layout
  becomes invisible to clients; this chunk's on-disk layout becomes a pure
  server-side detail. Independent of this chunk; informs #166's design.
- **API-side lineage join** (variant artifact list unioning idea + executor +
  evaluator artifacts). That is #166's wire-endpoint behavior, not on-disk
  layout — see the issue's mechanism-2 comment.
- **Path-traversal hardening.** The existing jail-check is layout-agnostic and
  unchanged; the new layout introduces no new attack surface (§9 confirms the
  serve route + admin walk already handle nested paths safely).

## 5. Naming map

Per CLAUDE.md naming discipline, validated against
[`docs/glossary.md`](../../docs/glossary.md):

| Concept | Identifier | Glossary basis |
|---|---|---|
| Idea group dir | `ideas/<idea_id>/` | artifact noun `idea`, plural dir |
| Variant group dir | `variants/<variant_id>/` | artifact noun `variant`, plural dir |
| Executor-produced subdir | `variants/<variant_id>/executor/` | role noun `executor` (-or form) |
| Evaluator-produced subdir | `variants/<variant_id>/evaluator/` | role noun `evaluator` (-or form) |
| Idea text file | `content.md` | matches binding §2.3 + subprocess host |
| Evaluator text file | `evaluation.md` | artifact/role-coherent |

**Naming note for review:** the top-level dirs use the **artifact noun**
(`ideas`/`variants`), while the variant sub-dirs use the **role noun**
(`executor`/`evaluator`). This is a deliberate mix and it is glossary-coherent:
the variant aggregates artifacts from two *sources* (the executor and the
evaluator roles), so naming the sub-dirs by their producing role is the natural
disambiguator. The alternative — task-kind gerunds (`execution/`/`evaluation/`)
— was considered and rejected: artifacts trace to durable entities and their
producing role, not to the transient task. The data model already uses
`executor_artifacts_uri` (role-prefixed) for exactly this distinction
([`02-data-model.md`](../../spec/v0/02-data-model.md) §9.1), so `executor/`
aligns. No new wire/enum/CLI identifiers are introduced, so the
`rename-discipline` gate is unaffected.

No identifiers are renamed (no old→new code-symbol map); this is a
directory-layout change, not a vocabulary change.

## 6. Files to touch

**Reference impl (Python):**

- [`reference/services/web-ui/src/eden_web_ui/artifacts.py`](../../reference/services/web-ui/src/eden_web_ui/artifacts.py)
  — add `entity_artifact_dir()`; thread an entity-scoped base into
  `write_artifact_bundle` + `predict_artifact_uri`; unify text filename.
- [`reference/services/web-ui/src/eden_web_ui/routes/ideator.py`](../../reference/services/web-ui/src/eden_web_ui/routes/ideator.py)
  — call the path-builder for `ideas/<idea_id>/`; `text_filename="content.md"`.
- [`reference/services/web-ui/src/eden_web_ui/routes/evaluator.py`](../../reference/services/web-ui/src/eden_web_ui/routes/evaluator.py)
  — call the path-builder for `variants/<variant_id>/evaluator/`.
- [`reference/services/ideator/src/eden_ideator_host/subprocess_mode.py`](../../reference/services/ideator/src/eden_ideator_host/subprocess_mode.py)
  — already writes `ideas/<idea_id>/content.md`; audit for consistency with the
  shared helper (route through it if feasible without an import cycle, else
  leave + add a lockstep comment).

**Standalone CLI:**

- [`reference/scripts/manual-ui/eden-manual`](../../reference/scripts/manual-ui/eden-manual)
  — `_write_artifact_for_role` path change (hand-mirror of the Python helper);
  verify `_translate_artifacts_uri_to_host` handles nested relative paths.

**Spec (binding only):**

- [`spec/v0/reference-bindings/worker-host-subprocess.md`](../../spec/v0/reference-bindings/worker-host-subprocess.md)
  — §2.3 confirm `ideas/<idea_id>/content.md`; add variant-subdir convention.
- [`spec/v0/08-storage.md`](../../spec/v0/08-storage.md) — one informational
  note in §5.1 pointing at the binding's concrete scheme.

**Tests:**

- [`reference/services/web-ui/tests/test_artifact_bundle.py`](../../reference/services/web-ui/tests/test_artifact_bundle.py),
  `test_ideator_*`, `test_evaluator_*`, `test_admin_artifacts_routes.py` —
  update path assertions; add a path-builder unit test (three mappings +
  no-overwrite stem).
- [`reference/services/ideator/tests/test_ideator_subprocess.py`](../../reference/services/ideator/tests/test_ideator_subprocess.py)
  — likely already asserts `ideas/<idea_id>/content.md`; confirm.
- Any test hard-coding `artifacts/<id>.md` flat paths — grep and update.

**Docs:**

- [`docs/glossary.md`](../../docs/glossary.md) — add the artifact-layout
  vocabulary if not already covered (per §5).
- `CHANGELOG.md` `[Unreleased]` + `docs/roadmap.md` one-liner (planless-style:
  roadmap points at the merged PR, since #168 is issue-driven not a phase
  chunk) at impl-completion time.

**Note on scripted fixtures:** the scripted hosts
([`scripted.py:64,161,169`](../../reference/services/_common/src/eden_service_common/scripted.py))
emit *fictional* URIs (`file:///tmp/artifacts/...`) — they never write bytes
(the admin-artifacts banner already explains scripted mode produces an empty
listing). These are opaque round-trip values, not real paths. Updating them to
match the new layout is **cosmetic** (keeps demos legible) and optional; doing
so is harmless because nothing resolves them. Decide during impl whether to
touch them; not load-bearing.

## 7. Conformance impact

**Near-zero.** Per chapter 9 §6, the only IUT contract a conformance harness
can rely on is the chapter-7 HTTP binding; physical artifact layout is **not
wire-observable** (`artifacts_uri` is an opaque deployment-local URI per
[`02-data-model.md`](../../spec/v0/02-data-model.md) §1.5). The conformance
suite asserts nothing about on-disk paths.

The conformance evaluator scenarios hard-code opaque artifact URIs
(`file:///tmp/eden-conformance-success-artifacts`, `…-error-artifacts` at
[`test_evaluator_submission.py`](../../conformance/scenarios/test_evaluator_submission.py)
~L143/L184). These are round-trip values the IUT stores and echoes back —
they do **not** need to follow the layout and **must not** be changed to do so
(that would falsely couple the suite to a reference-impl path shape, the
anti-pattern CLAUDE.md's conformance-traceability pitfall warns about). No
`§`-reference updates, no new/changed assertions, no `check_citations` changes.

## 8. Migration / cleanup

**No migration code, no compat shim** (CLAUDE.md no-backwards-compat-shims
posture + §1.2 self-describing-substrate property):

- **Existing flat artifacts keep resolving.** Their `artifacts_uri` values are
  absolute and still point at real files under the jail; the serve route and
  admin walk are layout-agnostic. Nothing breaks.
- **New writes go hierarchical.** From the merge, every new idea/evaluator
  artifact lands in the entity tree.
- **Operators see a transient mix** of legacy flat files + new nested dirs in
  the admin listing during the tail of any in-flight demo experiment. Acceptable
  per the issue; operators clean up flat files manually if they care.
- **What to retire:** nothing in code (no shim existed). The only "retirement"
  is the flat-write code path inside the writers, replaced by the hierarchical
  path-builder in the same edit.

If a future external-deployment-base emerges and a one-shot migration becomes
worth it, the recipe is: for each flat file, look up the entity-id it
references in the task store, `mkdir -p` the entity dir, move the file, and
rewrite the stored `artifacts_uri`. Explicitly deferred (§4); file as an issue
only if a real deployment needs it.

## 9. Risks / things to watch

- **No-overwrite (§5.4) on evaluator resubmit.** The load-bearing correctness
  risk. The §D.2 unique-stem hybrid neutralizes it by construction; if
  plan-review chooses fixed filenames instead, the writer **must** gain a
  refuse-to-overwrite guard. Either way, add a test that submits an evaluator
  artifact twice for the same `variant_id` and asserts the first is not
  clobbered.
- **CLI / web-UI lockstep.** `eden-manual` hand-mirrors the bundle logic
  because it can't import `eden_web_ui` (system-`python3` script). The path
  change must land in both, identically. The existing in-file comment
  ([`eden-manual:296-300`](../../reference/scripts/manual-ui/eden-manual))
  already flags this; the path-builder test should run against both code paths
  (or at least assert the CLI produces the same relative path the helper does).
- **Import-cycle temptation.** Routing the subprocess ideator host through
  `eden_web_ui.artifacts` would couple a worker-host package to the web-UI
  package. If that import is undesirable, duplicate the tiny path-builder into
  a shared `eden_service_common` location instead, and have all three Python
  surfaces import from there. Decide during impl; surface the chosen home in
  the PR.
- **Nested-path host translation.** `_translate_artifacts_uri_to_host` uses
  `relative_to(CONTAINER_ARTIFACTS_DIR)` — verified to walk the full relative
  path, so `ideas/<id>/content.md` round-trips. Add the nested-path unit test
  to lock this in; a regression here silently breaks the CLI's artifact
  read-back.
- **`content.md` vs `idea.md` rename affects bundle entry names.** Any test or
  template that reads the bundle's headline entry by the literal name `idea.md`
  must switch to `content.md`. Grep templates + helpers
  ([`_helpers.py` read-bundle paths](../../reference/services/web-ui/src/eden_web_ui/routes/_helpers.py))
  before merging.
- **Scope creep toward #166 / executor uploads.** The `executor/` subdir invites
  "while we're here, let's add the executor upload form." Resist — that's a
  separate writer with its own form, validation, and tests (#120-adjacent). This
  chunk establishes the convention only.
- **Spec binding drift re-opening.** Editing `worker-host-subprocess.md` §2.3
  risks re-touching adjacent prose; keep the edit surgical (idea-path
  confirmation + one variant-subdir paragraph) so the binding's other contracts
  (outcome.json shape, failure modes) are untouched.

## 10. Chunked execution plan + validation gates

Single impl PR (scope is small-to-medium, ~3–5 days per the issue; no
multi-wave split needed). Internal sequence:

**Wave 1 — path-builder + Python writers.**

- Add `entity_artifact_dir()` + thread entity-scoped base through
  `write_artifact_bundle` / `predict_artifact_uri`.
- Update web-UI ideator + evaluator routes; unify text filename to
  `content.md`.
- Audit/route the subprocess ideator host; decide the shared-helper home
  (avoid the import cycle — §9).
- Path-builder unit test (three mappings + evaluator no-overwrite).

*Gate:* `uv run ruff check . && uv run pyright && uv run pytest -q`
(web-UI + ideator package suites green).

**Wave 2 — CLI mirror.**

- Mirror the path change in `eden-manual` `_write_artifact_for_role`; verify
  nested-path host-translate.

*Gate:* `uv run pytest -q` (CLI / manual-ui tests if present) + a manual
`eden-manual` ideation-submit smoke against a local stack confirming the URI
stamps `…/ideas/<id>/content.md` and the web-UI serves it.

**Wave 3 — spec binding + docs.**

- `worker-host-subprocess.md` §2.3 + variant-subdir prose; `08-storage.md`
  §5.1 informational note; `docs/glossary.md` layout vocabulary.

*Gate:* `python3 scripts/spec-xref-check.py`, `markdownlint-cli2`,
`python3 scripts/check-rename-discipline.py`.

**Wave 4 — full validation + compose smokes.**

- Run the literal CLAUDE.md "Commands" gate (not a narrowed subset — the
  smokes are the layout's real end-to-end check, since they drive
  `setup-experiment` + a live stack that actually writes artifacts to the
  bind-mount):

```text
uv sync
uv run ruff check .
uv run pyright
uv run pytest -q
uv run pytest -q conformance/ -n auto
uv run python conformance/src/conformance/tools/check_citations.py
npx --yes markdownlint-cli2@0.14.0 "**/*.md" "#node_modules" "#.venv" "#docs/archive/**" "#docs/plans/review/**"
pipx run 'check-jsonschema==0.29.4' --check-metaschema spec/v0/schemas/*.schema.json
python3 scripts/spec-xref-check.py
python3 scripts/check-rename-discipline.py
bash reference/compose/healthcheck/smoke.sh
bash reference/compose/healthcheck/smoke-subprocess.sh
bash reference/compose/healthcheck/e2e.sh
```

The `smoke-subprocess.sh` run is the load-bearing surface for this chunk: it
exercises the subprocess ideator host writing real bytes to the bind-mount, so
it proves the `ideas/<idea_id>/content.md` path actually materializes on disk
and the web-UI / admin listing resolve it. `e2e.sh` drives the Web-UI ideator
walkthrough, proving the web-UI writer's hierarchical path is served correctly.

- CHANGELOG `[Unreleased]` entry + roadmap one-liner (planless shape, points
  at the PR).

## 11. Estimated effort

| Activity | Estimate |
|---|---|
| Path-builder + web-UI writers + unit tests | ~1 day |
| CLI mirror + nested-path translate test | ~0.5 day |
| Subprocess-host audit + shared-helper home decision | ~0.5 day |
| Spec binding + glossary + `08-storage` note | ~0.5 day |
| Test-fixture updates (grep flat paths) + full validation incl. smokes | ~1 day |
| Codex-review iterations (plan + impl) | ~1 day |
| **Total** | **~4 days** |

Matches the issue's "small to medium, ~3–5 days." The dominant variable is the
breadth of test fixtures that hard-code flat paths; a thorough grep up front
(`grep -rn 'artifacts/.*\.md\|artifacts_dir.*\.tar' reference/`) bounds it.
