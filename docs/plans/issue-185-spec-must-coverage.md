# Issue #185 — Automated spec MUST-coverage instrumentation

## 1. Context

[Issue #185](https://github.com/ealt/eden/issues/185) asks for **automated spec-coverage tooling** that walks `spec/v0/*.md`, extracts every normative MUST, joins each against the conformance scenarios that cite its section (or an ancestor), reports which MUSTs are covered vs. uncovered, and wires the result into CI so that **a new spec MUST landed without a covering scenario fails CI**. The headline value is a *self-maintaining* coverage audit that replaces the manual snapshot ([#99](https://github.com/ealt/eden/issues/99)).

### 1.1 Reconciliation against the current tree (verified at plan-authoring time)

The issue was filed against the 2026-05-23 demo-session snapshot and its framing is **partly stale**. Mapping each of its asks against the current codebase surfaced four corrections this plan bakes in — consistent with the "verify before claiming" discipline:

1. **The forward-direction generator already exists.** The issue estimates "build the MUST-extraction + scenario-citation-join logic (~200 lines of Python)." That logic is *already shipped* as [`scripts/conformance-coverage.py`](../../scripts/conformance-coverage.py) (landed in PR #112 alongside #99). It walks every RFC-2119 keyword line in `spec/v0/*.md`, classifies by keyword, parses scenario docstring citations, does the same ancestor-walk join as [`check_citations.py`](../../conformance/src/conformance/tools/check_citations.py), and emits the coverage matrix to [`docs/conformance-coverage.md`](../../docs/conformance-coverage.md). **The build-the-tool work is done.** What is missing is *enforcement* — the generator runs only when a human remembers to run it.

2. **#99 is CLOSED but did NOT zero the gap.** The issue says "after #99 lands (the existing gap drops to zero or near-zero), tighten the CI to fail on any uncovered MUST." #99 landed (PR #112) as a *partial* per-claim audit; the gap is nowhere near zero. A fresh generator run today reports **191 / 579 MUST/MUST-NOT lines covered (33%), 216 uncovered**. The "tighten to zero" trigger therefore **cannot fire in this chunk**. This plan reframes the endgame (§2.4): the ratchet *baselines* the current gap and enforces monotonic shrink; "fail on any uncovered MUST" becomes the terminal state when the baseline is finally emptied, not a step this chunk performs.

3. **The committed coverage doc is already drifted.** The committed [`docs/conformance-coverage.md`](../../docs/conformance-coverage.md) reports `186 / 569` in its `## Summary`; a fresh regen reports `191 / 579`. The spec grew (chapters 10/11 control-plane + checkpoint MUSTs) since the doc was last regenerated, and nothing forced a refresh. This is the exact failure mode #185 exists to kill — and it means this chunk MUST regenerate-and-commit the doc *before* turning on the drift gate, or CI goes red on merge.

4. **There is no CI step for coverage at all.** The `conformance` job runs [`check_citations.py`](../../conformance/src/conformance/tools/check_citations.py) (the *inverse* direction: every scenario cites a real MUST) but never runs `conformance-coverage.py`. The forward direction has zero CI enforcement today.

**Net:** #185 is not "build a tool." It is **"add the enforcement layer (drift gate + growth ratchet + CI wiring) around the generator that already exists, and make the generator's output trustworthy enough to gate on."**

### 1.2 What "the deliverable" is

A small, stdlib-only enforcement layer on the existing generator, plus CI wiring and a baseline:

- A `--check` mode on `conformance-coverage.py` that regenerates in-memory and fails if the committed doc is stale (drift gate).
- A `--check-ratchet` mode + a committed baseline of *allowed-uncovered* MUSTs (with per-entry classification + justification) that fails if a **new** uncovered MUST appears beyond the baseline (growth ratchet).
- A CI job that runs both gates, triggered on `spec/**` **and** `conformance/**` changes.
- The regenerated, current `docs/conformance-coverage.md`.
- Docs: the AGENTS.md Commands-table entry, a CONTRIBUTING.md pointer refresh, and the gate's self-description in the generated doc's intro.

## 2. Decisions captured before drafting

Listed so codex-review and the operator can see what was deliberate vs. proposable. Per BASE.md these default to the **agentic / strongest-mechanism** framing; if the cautious version is right, course-correct at plan review.

### 2.1 Extend the existing generator, do not build a parallel tool

The generator already does the extraction + join correctly and is the source of `docs/conformance-coverage.md`. Building a second tool (e.g. under `conformance/src/conformance/tools/`) would fork the join logic and create two sources of truth for "which MUSTs are covered." The enforcement layer goes **into `scripts/conformance-coverage.py` as new modes** (`--check`, `--check-ratchet`, `--write-baseline`), mirroring how [`check-rename-discipline.py`](../../scripts/check-rename-discipline.py) carries `--write-baseline` and `check-complexity.py` carries `--json` / `--list`. *Proposable:* split the gate into a sibling `scripts/check-coverage.py` that imports the generator's join functions, if codex prefers gate-logic separated from doc-rendering. The default keeps them co-located because they share the exact same parse/join and divergence between them is the risk.

### 2.2 Set/allowlist ratchet, not a bare count

Two models for "fail if the gap GROWS":

- **(A) Count ratchet** — store a baseline integer (max allowed uncovered count); fail if `len(uncovered) > baseline`. Trivial, but gameable (add a new uncovered MUST while a refactor happens to drop another below threshold → net-zero, gate stays green) and it pins *no identity* (you can't tell *which* MUST regressed).
- **(B) Set/allowlist ratchet** — store the exact set of allowed-uncovered MUST keys; fail if any *currently-uncovered* MUST is **not** in the baseline (i.e. a genuinely new uncovered MUST appeared). Newly-covered MUSTs are removed from the baseline (monotonic shrink). Surfaces exactly which MUST regressed.

**Decision: Model B.** It matches the two established repo ratchets ([`check-rename-discipline.py`](../../scripts/check-rename-discipline.py)'s allowlist and [`check-complexity.py`](../../scripts/check-complexity.py)'s `# slop-allow` disposition) and the operator already reasons in those terms. *Proposable:* Model A if the operator wants the lowest-maintenance gate and accepts the gaming/identity weaknesses.

### 2.3 Baseline entries carry a classification + justification (not just a key)

The generator flags **every** uncovered MUST, including ones that are *permanently un-coverable by a wire-only suite* — black-box-impossible atomicity invariants (chapter 9 §3 explicitly: the §1.3 atomicity-of-state-change MUST is asserted only via testable consequences) and off-wire git artifacts (chapter 9 §4: squash shape / eval-manifest / `work/*` discipline are part of chapter 06 but not assertable through the chapter-7 binding per §6). A bare allowlist would create perverse pressure to write impossible scenarios to "close" these.

**Decision:** each baseline entry carries a `reason` tag drawn from #99's five-tag taxonomy — `consequence` (black-box-impossible, asserted via proxy), `off-wire` (not exposed through the chapter-7 IUT contract per §6), `restatement` (duplicate of a MUST covered elsewhere), or `todo` (a real, coverable gap awaiting a scenario). This is the direct analogue of `# slop-allow: <justification>`: an allowed-uncovered entry is an explicit, operator-reviewable exception, not an invisible pass. The gate treats `consequence` / `off-wire` / `restatement` as *permanent* (they will never leave the baseline) and `todo` as *debt* (every closed scenario removes a `todo` entry). See §7 for why this is the load-bearing tie to chapter 9 §6.

### 2.4 Endgame is "baseline emptied," not "tighten to zero now"

Because #99 closed without zeroing the gap (§1.1.2), the issue's "after #99, fail on any uncovered MUST" is reframed: the terminal state is *the `todo` set in the baseline reaches zero* (only `consequence`/`off-wire`/`restatement` permanent entries remain). At that point the gate already enforces "no new uncovered coverable MUST" with no further tightening needed — the permanent entries are the irreducible floor that a wire-only suite can never cover. There is no separate "flip to strict" step; the ratchet *is* the strict gate, parameterized by a baseline that shrinks to its permanent floor.

### 2.5 Standalone lightweight CI job, no `uv sync`

`conformance-coverage.py` imports only the stdlib (`ast`, `re`, `sys`, `collections`, `pathlib`) — it never imports a workspace package. So the gate can run in a **standalone job needing only `python3`**, mirroring [`rename-discipline`](../../.github/workflows/ci.yml) and `complexity-gate` (both python-only, no `uv sync`), rather than as a step inside the heavy `conformance` job (which runs the full `pytest -n auto` and is still behind the eden#38 2-week-clean branch-protection TODO). *Proposable:* fold it in as a step after the existing `check_citations.py` step in the `conformance` job — that job already has the right path filter (`spec/**` + `conformance/**`) — if the operator prefers one fewer job over faster/independent feedback. The default is the standalone job because (a) it's faster (no venv sync), (b) it's independent of conformance-suite flakiness, and (c) it parallels the existing discipline-gate jobs the operator already recognizes.

These five are NOT up for re-litigation in codex-review unless review surfaces a load-bearing contradiction with the issue's stated scope.

## 3. Scope

**In scope:**

- `--check` (drift gate): regenerate in-memory, diff against committed `docs/conformance-coverage.md`, exit non-zero on mismatch with a "run `scripts/conformance-coverage.py` and commit" hint.
- `--check-ratchet` (growth gate): compute the current uncovered-MUST set, compare against the committed baseline, fail on any uncovered MUST not in the baseline; warn (don't fail) on baseline entries that are now covered (so they can be pruned).
- `--write-baseline`: dump the current uncovered set as the baseline file, with `reason` defaulting to `todo` for new entries (operator re-classifies the permanent ones), preserving the classification of entries already present.
- A **stable identity key** for uncovered MUSTs (§8.2) — line numbers are not stable across spec edits; the baseline must key on `(chapter, section, normalized-claim-text)`.
- The committed baseline file (`scripts/conformance-coverage-baseline.json`) seeded from the current 216-gap set, with the permanent entries (`consequence`/`off-wire`/`restatement`) classified during this chunk by reconciling against chapter 9 §3/§4/§6 and the existing manual per-claim block in `docs/conformance-coverage.md`.
- Regenerate + commit the current `docs/conformance-coverage.md` (it is drifted; §1.1.3).
- A new `coverage-gate` CI job (§2.5) on the `spec` + `conformance` path filters.
- Docs: AGENTS.md Commands-table row for `conformance-coverage.py` (currently absent); CONTRIBUTING.md pointer (already links the doc — add the "gate fails if you add a MUST without a scenario or forget to regen" note); the generated doc's intro gains a short "this matrix is CI-gated" paragraph.

**Out of scope (per issue §"Out of scope" + reconciliation):**

- **Authoring scenarios to close the existing 216-MUST gap.** The tool flags gaps; closing them is #99-style follow-up work, each its own chunk. This chunk *baselines* the gap, it does not close it.
- **SHOULD / MAY coverage gating.** Per chapter 9 §3 only MUSTs are interop contracts. The generated matrix continues to *display* SHOULD/MAY for visibility but the gate keys only on MUST / MUST NOT.
- **Automating the per-claim five-tag taxonomy / multi-MUST-line splitting.** The hand-written per-claim audit block in `docs/conformance-coverage.md` (the "manual prose block" the generator preserves verbatim) stays manual. The baseline's `reason` tags reuse the taxonomy's vocabulary but are applied by a human during baseline seeding, not auto-derived. (If codex judges multi-MUST-per-line under-counting material to the gate's correctness, §8.3 notes the conservative posture.)
- **Cross-spec-version migration tracking** (e.g. `spec/v1/`).
- **Changing the join semantics** (ancestor-walk, citation grammar). The existing join is the contract; the gate enforces it, it does not redefine it.

## 4. Spec / contract impact

**None normative.** This chunk touches no `spec/v0/*.md` chapter, no JSON Schema, no Pydantic model, no wire binding. The gate *reads* the spec to count MUSTs; it changes nothing in it.

One **non-normative** spec-adjacent note: chapter 9 §3 already states "When the spec evolves, the suite evolves with it… A change that removes a MUST or downgrades it to SHOULD MUST be accompanied by a matching scenario removal or rewrite." This chunk's gate is the *mechanical enforcement* of the forward half of that prose ("a change that *adds* a MUST must be accompanied by a covering scenario or a baselined exception"). No spec edit is required to add the gate, but the plan flags this as the conceptual hook — if codex thinks chapter 9 should *name* the gate (the way AGENTS.md names `check_citations.py`), that is a one-line informative addition to §3, surfaced here as **proposable, not default** (the gate is impl tooling, not a normative contract; AGENTS.md is the more appropriate home — §6).

## 5. Naming map

No EDEN-vocabulary identifiers (roles / verbs / task kinds / submission classes / artifacts) are introduced or renamed — the gate is pure tooling. The new tooling-surface names, validated against the established sibling-gate conventions:

| New identifier | Kind | Rationale / precedent |
|---|---|---|
| `--check` | CLI flag on `conformance-coverage.py` | drift gate; "check, don't write" is the conventional generator-verification verb (mirrors `check-jsonschema`, the `check-*` script family) |
| `--check-ratchet` | CLI flag | growth gate; named for the ratchet model (§2.2). *Alt considered:* `--gate` — rejected as less descriptive of the monotonic-shrink semantics |
| `--write-baseline` | CLI flag | **exact reuse** of [`check-rename-discipline.py`](../../scripts/check-rename-discipline.py)'s flag name — same semantics (dump current hit set as the new baseline) |
| `scripts/conformance-coverage-baseline.json` | committed baseline file | `<tool-name>-baseline.json`; sibling to the generator. JSON (not txt) because entries are structured (key + reason + justification). *Alt:* embed in the generator as a literal — rejected; a separate file keeps the diff reviewable and matches the rename-discipline allowlist-as-data posture |
| `coverage-gate` | CI job name | mirrors `rename-discipline` / `complexity-gate` — the `<concern>-gate` / `<concern>-discipline` naming family for python-only discipline jobs |
| `reason` values: `consequence` / `off-wire` / `restatement` / `todo` | baseline-entry classification enum | reuses #99's five-tag taxonomy vocabulary (the doc's manual block already uses `(consequence)` / `(restatement)` / `(uncovered)`); `off-wire` is the chapter-9-§6 IUT-contract-boundary tag, `todo` replaces `(uncovered)` to read as actionable debt |

No glossary (`docs/glossary.md`) entry is needed — these are tooling identifiers, not protocol vocabulary. The naming map is surfaced here for operator review per the rename-discipline.

## 6. Migration / cleanup

Pre-external-user posture (CLAUDE.md): no compat shims, no deprecation period.

- **Regenerate-and-replace** `docs/conformance-coverage.md` in this chunk (it is drifted; §1.1.3). The manual prose block is preserved verbatim by the generator's existing block-preservation logic — confirm byte-identity after regen so the drift gate doesn't immediately trip on its own first commit.
- **AGENTS.md Commands table**: add the `conformance-coverage.py` row (currently only `check_citations.py` and `spec-xref-check.py` are listed). This is additive, not a migration.
- **Nothing is retired.** `check_citations.py` (inverse direction) and `conformance-coverage.py` (forward direction) are complementary and both stay. The gate does not subsume the citation check.
- **No data migration.** The baseline file is born in this chunk; there is no prior baseline to migrate.

## 7. Conformance impact

**No scenarios are added, edited, or removed**, and **no `§`-reference changes.** The gate measures coverage; it does not assert anything against an IUT.

The load-bearing tie to conformance is the **chapter 9 §6 IUT-contract boundary** (the recurring AGENTS.md pitfall). The generator flags as "uncovered" a set of MUSTs that *cannot* be covered by a wire-only conformance suite:

- **Black-box-impossible (chapter 9 §3):** the [`04-task-protocol.md`](../../spec/v0/04-task-protocol.md) §1.3 atomicity-of-(state-change, event-emission) invariant — §3 says the suite asserts only its testable *consequences*. These are `consequence`-tagged baseline entries.
- **Off-wire (chapter 9 §4 + §6):** the [`06-integrator.md`](../../spec/v0/06-integrator.md) git-side artifacts (squash shape, evaluation-manifest shape, `work/*` access discipline, reachability) — §4 explicitly says these "are **not** asserted by a wire-only suite" because §6 makes the chapter-7 HTTP binding the only IUT contract. These are `off-wire`-tagged baseline entries.

**The gate MUST NOT pressure anyone to write scenarios for these.** That is the entire purpose of the §2.3 `reason` classification: `consequence` / `off-wire` entries are *permanent* baseline residents, not debt. Mis-modeling them as `todo` would re-create the exact "conformance plan over-promises what §6 allows" failure the AGENTS.md pitfall warns against. **Seeding the baseline therefore requires walking each of the 216 uncovered MUSTs through the chapter-9-§6 pipeline** (`is this MUST observable through the chapter-7 binding?`) and tagging accordingly — this classification pass, reconciled against the existing manual per-claim block, is the substantive judgment work of this chunk (the code is mechanical; the tagging is not).

A secondary subtlety: the generator's join is **section-granular** (a scenario citing §3.2 covers §3 and §3.2). A section can contain several MUSTs of which only one is actually exercised — the matrix's own intro already disclaims this ("a scenario citing a section is not proof that every MUST in that section is exercised"). The gate inherits this looseness: it gates on *section citation*, not per-MUST assertion. This is acceptable for a *growth* ratchet (it still catches a brand-new MUST in a never-cited section) but is **not** a per-MUST coverage proof. §8.3 records this as a deliberate conservatism with a tracked follow-up rather than scope creep into per-MUST body analysis.

## 8. Design notes

### 8.1 Two gates, one tool

`conformance-coverage.py` gains three non-default modes (default = regenerate-and-write, unchanged):

- `--check`: build the matrix in-memory; compare the full rendered output against the committed file; exit 1 + a unified-diff snippet + "run `python3 scripts/conformance-coverage.py` and commit the result" on mismatch. Because the generator already re-inserts the manual prose block verbatim, a full-file compare is correct *provided* the committed manual block is byte-stable — confirm during §9 Wave 1.
- `--check-ratchet`: compute the current uncovered-MUST key set; load the baseline; `new_uncovered = current_keys - baseline_keys`; if non-empty, exit 1 listing each new uncovered MUST with chapter/§/excerpt and the instruction to either add a covering scenario or (if off-wire/consequence) add a justified baseline entry. Separately, `stale_baseline = baseline_keys - current_keys` (entries now covered or whose text changed) is printed as a non-fatal "prune these" advisory — keeping the baseline honest without blocking on it.
- `--write-baseline`: regenerate the baseline from the current uncovered set; preserve `reason`/justification for keys already present; default new keys to `reason: todo`. Used to re-seed after a deliberate spec change.

`--check` and `--check-ratchet` run together in CI as one job (§2.5).

### 8.2 Stable identity key (load-bearing)

The current generator keys uncovered rows by **line number**, which shifts on every spec edit — unusable as a baseline key (every edit would look like the whole gap moved). The baseline key is `(chapter_filename, section_number, sha1(normalized_claim_text))` where normalization collapses whitespace and strips markdown table-pipe / list-bullet / anchor-link noise (the generator's existing `_trim_paragraph` already does most of this — reuse it).

**Tradeoff surfaced for codex/operator:**

- **Text-hash key (recommended).** Stable across line moves; *changes* when the MUST's wording changes. That sensitivity is arguably *correct* — rewording a MUST is exactly when you should re-confirm its coverage — but a cosmetic edit (fix a typo in a MUST sentence) spuriously trips the gate, forcing a `--write-baseline` re-seed. Acceptable: the re-seed is one command and the diff makes the wording change reviewable.
- **Section-granular key (fallback).** `(chapter, section)` with ≥1 uncovered MUST → one key. Stable across both line *and* wording edits, but coarse: adding a new uncovered MUST to a section that *already* has a baselined uncovered MUST would not trip the gate. That hole defeats the issue's core ask ("a new MUST without a scenario fails CI"), so text-hash is the default.

**Decision: text-hash key**, with the cosmetic-edit-noise cost accepted. If codex-review judges the noise unacceptable, the documented fallback is section-granular + a separate per-section MUST *count* in the key (`(chapter, section, n_uncovered_must)`) — stable across wording, trips when a section's uncovered-MUST count rises. This is recorded as the round-1 decision point.

### 8.3 Multi-MUST-per-line + section-granular conservatism

The generator counts per *line*; a line "X MUST … and MUST NOT …" is one row with two keywords. The join is per *section*. Neither is per-MUST-claim. For a *growth ratchet* this is conservative-safe (it never *misses* a new MUST in an uncovered section; it can only *under-resolve within* an already-baselined section — see §8.2 section-key hole, which the text-hash key avoids at line granularity). Per-MUST-claim splitting + per-MUST body-exercise proof is explicitly deferred (it requires reading each test body, flagged as a follow-up in the existing doc and in #99). **A deferral issue is filed at chunk-completion** per the AGENTS.md deferral-tracking rule, cross-referencing #99.

### 8.4 CI wiring specifics

New `coverage-gate` job in [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml):

- `runs-on: ubuntu-latest`, `needs: changes`, `if:` gated on `run_all` OR the `conformance` path-filter output (which already covers `spec/**` + `conformance/**` — verified §1.1.4; a new MUST is a `spec/**` change and MUST trigger the gate, so reusing the `conformance` filter is correct and no new filter bucket is needed).
- Steps: `actions/checkout@v4`; `actions/setup-python` (per AGENTS.md "Python-using jobs must declare an interpreter"); `python3 scripts/conformance-coverage.py --check` then `python3 scripts/conformance-coverage.py --check-ratchet`.
- No `uv sync` (stdlib-only; §2.5).

## 9. Chunked execution plan

A small single-PR chunk; "waves" here are sequential commits within the one impl PR, each with its own validation gate. The whole thing is one cohesive change (pre-external-user; CLAUDE.md no-partial-migration posture).

### Wave 1 — Make the committed doc current + add `--check`

- Regenerate `docs/conformance-coverage.md`; confirm the manual prose block round-trips byte-identical.
- Implement `--check` (drift gate).
- **Gate:** `python3 scripts/conformance-coverage.py --check` exits 0 against the freshly-committed doc; deliberately stale the doc by one line and confirm it exits 1 with a useful diff; markdownlint clean on the regenerated doc.

### Wave 2 — Stable key + baseline seed + classification pass

- Implement the `(chapter, section, claim-hash)` key (§8.2), reusing `_trim_paragraph` normalization.
- `--write-baseline` to seed `scripts/conformance-coverage-baseline.json` from the current 216-gap set.
- **The classification pass (the substantive judgment work):** walk each uncovered MUST through the chapter-9-§6 pipeline (§7), tagging `consequence` / `off-wire` / `restatement` / `todo`, reconciled against the doc's manual per-claim block. Each non-`todo` entry carries a one-line justification (the `# slop-allow` analogue).
- **Gate:** baseline round-trips (`--write-baseline` is idempotent on a clean tree); every entry has a `reason` + (for non-`todo`) a justification; the count of `todo` entries is recorded in the PR description as the starting debt figure.

### Wave 3 — `--check-ratchet` + CI job + docs

- Implement `--check-ratchet` (growth gate) + the stale-baseline advisory.
- Add the `coverage-gate` CI job (§8.4).
- Docs: AGENTS.md Commands row; CONTRIBUTING.md note; generated-doc intro "CI-gated" paragraph.
- File the §8.3 per-MUST-resolution deferral issue (cross-ref #99) and the §4 "name the gate in chapter 9 §3?" proposable (if codex wants it tracked).
- **Gate:** add a throwaway MUST to a scratch spec line locally → `--check-ratchet` exits 1 naming it; remove it → exits 0. Run the full pre-push quartet per AGENTS.md ("the Commands section is the literal pre-push gate"): markdownlint, the two new gate invocations, `check_citations.py`, and `conformance-coverage.py` (default mode) leaving a clean tree. CHANGELOG `[Unreleased]` entry + roadmap one-liner (planless-chunk shape: roadmap points at the merged PR).

## 10. Risks / things to watch

- **The drift gate trips on its own first commit if the manual block isn't byte-stable.** The generator re-inserts the manual prose block; if regeneration normalizes whitespace differently than the committed block, `--check` fails immediately. Mitigation: Wave 1 confirms byte-identity before committing; if the block isn't stable, fix the preservation logic (not the block) so regen is idempotent.
- **Text-hash key noise (§8.2).** Cosmetic MUST edits force a `--write-baseline` re-seed. Accepted; the fallback (section+count key) is documented. Watch for codex pushing back here — it's the most likely round-1 design challenge.
- **Mis-classifying an off-wire/consequence MUST as `todo`** re-creates the chapter-9-§6 over-promise failure (§7). This is the highest-judgment, lowest-mechanical part; the classification pass MUST be reviewed against §3/§4/§6, not rubber-stamped. A wrong `todo` tag silently pressures a future contributor to write an impossible scenario.
- **CI path-filter miss.** If the gate is wired to the wrong filter and does NOT trigger on `spec/**`, the entire premise fails silently (a new MUST merges without the gate running). Mitigation: §8.4 reuses the verified `conformance` filter (already includes `spec/**`); Wave 3's gate test must confirm the job *runs* on a spec-only change (inspect the PR's checks list, not just local invocation) — this is the "narrowed local subset is not the CI gate" AGENTS.md pitfall applied to CI wiring itself.
- **Baseline rot.** A baseline that only grows is as bad as no audit. Mitigation: the stale-baseline advisory (§8.1) prints now-covered entries every CI run so they get pruned; the `todo` count in PR descriptions makes the debt trend visible.
- **Section-granular looseness (§8.3).** The gate proves "the section is cited," not "this MUST is exercised." A scenario could cite a section and assert something unrelated to a co-located new MUST. Accepted conservatism for a growth ratchet; per-MUST body analysis is the filed follow-up.
- **Two gates double the "I forgot to regenerate" friction.** A contributor who edits the spec now must regenerate the doc *and* possibly re-seed the baseline. Mitigation: both failures emit the exact command to run; the CONTRIBUTING.md note sets the expectation up front.

## 11. Estimated effort

| Activity | Estimate |
|---|---|
| Wave 1 (regen doc + `--check` drift gate) | ~0.25 day |
| Wave 2 (stable key + baseline seed + **classification pass**) | ~0.5 day (classification is the judgment-heavy part) |
| Wave 3 (`--check-ratchet` + CI job + docs + deferral issues) | ~0.25 day |
| **Total** | **~1 day** |

Lower than the issue's "~1 week" because the generator — the issue's headline build item — already exists. The remaining work is enforcement plumbing plus the one genuinely-substantive task: classifying the 216 existing gaps against the chapter-9-§6 boundary.
