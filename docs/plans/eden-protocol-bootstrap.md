# EDEN: Protocol-First Repo Bootstrap

## Context

`/Users/ericalt/Documents/eden` is the successor to `/Users/ericalt/Documents/direvo`.
The predecessor has a working monolith at `direvo/src/eden/` and a design
doc at `eden/docs/plans/eden-microservices-refactor.md`.

**The reframe (critical):** EDEN is not a product-with-microservices. EDEN is
a **protocol** for orchestrating directed code evolution — a specification
that defines components, their responsibilities, the messages between them,
and the invariants they must honor. This repo provides:

1. **The normative protocol specification** — versioned, formal, with
   machine-readable schemas for every wire format and state machine. This
   is the authoritative artifact; semantics live here, not in code.
2. **A reference implementation** — one complete, working stack that
   conforms to the spec. Explicitly labeled as *one* valid implementation,
   not *the* implementation.
3. **A conformance suite** (roadmapped) — black-box tests that any
   independently-built component can run against itself to prove it
   conforms. This is what makes swappability real rather than aspirational.

Analogues: Language Server Protocol (spec + reference servers + clients),
Model Context Protocol (spec repo + reference SDKs), OpenTelemetry (spec

- reference SDKs per language), Kubernetes CRI/CNI/CSI (wire APIs + many
interoperable implementations).

Anyone should be able to implement their own orchestrator, planner host,
evaluator, storage backend, git host, or UI in any language, and have it
interoperate with other conforming components.

User decisions already captured:

- GitHub: **personal account, private**, name `eden`.
- Existing microservices plan: **move to archive**; new spec + roadmap
  become authoritative.
- GitHub setup: repo create + push + CI (**markdownlint only at
  bootstrap**; Python and JSON Schema gates phase in as those artifacts
  land) + branch protection on `main` requiring the one CI check.

## Core Design Decisions

- **Spec-first, impl-second.** Every cross-component boundary is defined
  first in `spec/` (human-readable normative text + JSON Schema + state
  machine) before any code for it is written in `reference/`. When they
  disagree, the spec wins and the impl gets a bug.
- **Wire protocols as the contract**, not Python types. Components
  communicate over documented message formats (JSON over HTTP for control
  calls, SQL or JSON over some transport for tasks/events — to be pinned
  in Phase 1). Python bindings in `reference/packages/eden-contracts/`
  are generated from / aligned with the JSON Schemas and exist as a
  convenience for Python implementors, **not** as the source of truth.
- **Single repo, clear boundaries.** `spec/`, `reference/`, `conformance/`,
  `docs/` at top level. Can split into separate repos later if needed
  (MCP/LSP pattern), but day 0 discipline is structural, not geographical.
- **Versioned spec.** `spec/v0/…` from day 0. Breaking changes go to `v1/`,
  not mutated in place. Reference impl declares which spec version it
  targets.
- **No Python toolchain at bootstrap.** Phase 0 creates zero `.py`
  files, so no `pyproject.toml`, no `.python-version`, no uv
  workspace, no ruff/pyright config. These land in **Phase 3** when
  the first reference package (`eden-contracts`) gets actual code.
  This avoids CI gates that can't meaningfully run yet (pytest on
  no tests exits non-zero; ruff/pyright on an empty tree is a
  no-signal green that creates false confidence).
- **Target Python version and tooling are pinned for later phases**:
  Python 3.12+, ruff + pyright, uv workspace with members under
  `reference/services/*` and `reference/packages/*`. Config will be
  ported from direvo's `pyproject.toml` when Phase 3 introduces it.
- **`CLAUDE.md` → `AGENTS.md` symlink** (user convention).

## What Gets Created

### A. Top-level layout

Two trees below. The **first** is exactly what exists at the end of
Phase 0. The **second** is the eventual target shape (filled in over
Phases 1–13), included so the bootstrap directory structure makes
sense to readers — but none of the italicized future files are written
at bootstrap.

#### A.1 Phase 0 output (what *this* task creates)

```text
eden/
├── spec/
│   ├── README.md                  # "What is the EDEN protocol"; versioning policy
│   └── v0/
│       ├── README.md              # Table of planned chapters; "populated in Phases 1,2,4"
│       └── schemas/
│           └── .gitkeep           # Empty; first schema lands in Phase 1
├── reference/
│   ├── README.md                  # "This is a reference impl; not THE impl"
│   ├── services/
│   │   ├── control-plane/.gitkeep
│   │   ├── orchestrator/.gitkeep
│   │   ├── planner/.gitkeep
│   │   ├── implementer/.gitkeep
│   │   ├── evaluator/.gitkeep
│   │   └── web-ui/.gitkeep
│   ├── packages/
│   │   ├── eden-contracts/.gitkeep
│   │   ├── eden-storage/.gitkeep
│   │   ├── eden-git/.gitkeep
│   │   └── eden-blob/.gitkeep
│   ├── scripts/
│   │   └── setup-experiment/.gitkeep
│   └── compose/
│       └── .gitkeep
├── conformance/
│   └── README.md                  # Stub: "suite lands Phase 11"
├── docs/
│   ├── naming.md                  # (already exists, untouched)
│   ├── roadmap.md                 # Phase 0–13 (written at bootstrap)
│   ├── plans/
│   │   ├── eden-protocol-bootstrap.md  # (this doc)
│   │   └── review/                # /codex-review run artifacts
│   │       └── eden-protocol-bootstrap/<timestamp>/...
│   └── archive/
│       └── microservices-refactor-plan.md   # historical reference
├── tests/
│   ├── unit/.gitkeep
│   └── integration/.gitkeep
├── .github/workflows/ci.yml       # docs-lint job only
├── .gitignore
├── .markdownlint.json
├── README.md                      # "EDEN is a protocol…"
├── AGENTS.md                      # Coding-agent guide
├── CLAUDE.md                      # → AGENTS.md (symlink)
├── CONTRIBUTING.md                # Two paths: contributing to spec vs. impl
├── STYLE_GUIDE.md
└── LICENSE                        # (already exists, untouched)
```

Everything above is in scope for Phase 0. Nothing else. In particular:
**no numbered spec chapter files, no `*.schema.json` files, no
`pyproject.toml`, no Python, no code of any kind.**

#### A.2 Eventual target layout *(not created by this bootstrap)*

This is the eventual shape once the spec and reference impl are
written. Shown here for *orientation only*; Phase 0 does not create
any of these files.

```text
spec/v0/
  00-overview.md            # Phase 1
  01-concepts.md            # Phase 1
  02-data-model.md          # Phase 1
  03-roles.md               # Phase 2
  04-task-protocol.md       # Phase 2
  05-event-protocol.md      # Phase 4
  06-integrator.md          # Phase 4
  07-control-plane.md       # Phase 12
  08-storage.md             # Phase 4
  09-conformance.md         # Phase 11
  schemas/
    experiment-config.schema.json   # Phase 1
    task.schema.json                # Phase 1
    event.schema.json               # Phase 1
    proposal.schema.json            # Phase 1
    trial.schema.json               # Phase 1
    metrics-schema.schema.json      # Phase 1 (meta-schema for per-experiment metrics)

reference/packages/eden-contracts/pyproject.toml   # Phase 3
reference/packages/eden-contracts/...              # Phase 3
reference/packages/eden-storage/...                # Phase 6
reference/packages/eden-git/...                    # Phase 7
reference/services/orchestrator/...                # Phase 8
... (see docs/roadmap.md for the full mapping)

pyproject.toml           # Phase 3 (uv workspace root lands with first member)
.python-version          # Phase 3
```

### B. The spec itself (`spec/v0/`)

**Phase 0 writes only `spec/README.md` and `spec/v0/README.md`.** Those
READMEs describe the versioning policy and enumerate the planned
chapters (with a "filled in Phase N" note beside each), but no
chapter content or schema files are written at bootstrap. The
conventions below apply to those later-phase writes.

When chapters are eventually written (Phases 1, 2, 4, 11, 12):
**normative language** (MUST / SHOULD / MAY per RFC 2119), numbered
sections, explicit cross-references, and *no* references to specific
technology choices. Example boundaries:

- `04-task-protocol.md` (Phase 2) will define the task state machine,
  the claim-token rule, the submit-idempotency rule, and the wire
  format for task objects. It will **not** say "Postgres `tasks` table"
  — that's a reference-impl detail. It may say "a conforming task
  store MUST provide atomic claim with linearizable semantics" and
  leave the mechanism to the implementor.
- `05-event-protocol.md` (Phase 4) will define the event object shape,
  the transactional invariant (event insert MUST be atomic with the
  state change it describes), and the delivery guarantees subscribers
  can rely on. Whether that's LISTEN/NOTIFY, Kafka, or a WebSocket is
  out of scope.
- `06-integrator.md` (Phase 4) will pin git topology invariants:
  namespaces (`work/*`, `trial/*`, `main`), sole-integrator rule,
  squash rule, eval-manifest shape. Applies regardless of git host.

Every wire-format object will eventually have a JSON Schema under
`spec/v0/schemas/` (first ones land in Phase 1). The Markdown docs
link to them. CI validates schemas from Phase 1 onward and enforces
reference-impl Pydantic-model parity from Phase 3 onward.

### C. `reference/` at bootstrap

- `reference/README.md` — "What's implemented, what's stubbed, what
  spec version is targeted." At bootstrap: declares `targets:
  eden-protocol/v0 (draft — chapters not yet written)` and lists all
  services/packages as "empty; lands in Phase N".
- Each `reference/services/*/` and `reference/packages/*/` — a
  `.gitkeep` only. Root `pyproject.toml` and per-member
  `pyproject.toml` files **land in Phase 3** with the first real
  package (`eden-contracts`), not at bootstrap.
- **No code yet.** Bootstrap is structure + section-level READMEs.
  Phase 1 starts writing the spec.

### D. `conformance/` at bootstrap

- `conformance/README.md` only. Describes the intended shape: scenarios
  drive an implementation-under-test via its advertised protocol
  endpoints; assertions verify invariants. Actual suite is a later
  roadmap phase once enough protocol surface is stable to test.

### E. `docs/roadmap.md`

Two orthogonal concepts:

- **Units** — the fine-grained decomposition. Each unit is a distinct,
  named piece of work with its own exit criterion. Units are the
  progress-tracking granularity: you can cross one off even when it
  lands in a shared commit with others.
- **Chunks** — execution grouping. Units that must be designed and
  implemented together (cross-referencing spec chapters, protocol +
  first consumer, shared commit-sized work) travel as one chunk.
  Chunks are the review/commit granularity.

**Rule:** a chunk can span multiple units; no unit spans chunks. If
implementation runs over context mid-chunk, stop at the next unit
boundary — never mid-unit. This keeps handoffs, resumes, and partial
progress legible.

Roadmap uses phases (the high-level arc, 0–13) containing numbered
units (1a, 1b, …) with per-phase chunk annotations.

- **Phase 0 — Bootstrap.** *(This task.)* Single unit: 0a repo +
  scaffold + docs shell + GitHub.
- **Phase 1 — Spec v0 core concepts + schemas + fixture migration.**
  - 1a `00-overview.md` + `01-concepts.md` + `02-data-model.md` prose.
  - 1b JSON Schemas for config, task, event, proposal, trial,
    metrics-meta.
  - 1c Migrate `tests/fixtures/experiment/.eden/config.yaml` from
    direvo; assert schemas validate it.
  - *Chunk:* 1a+1b+1c one chunk — the prose, schemas, and fixture
    cross-constrain each other; splitting would produce inconsistency.
- **Phase 2 — Spec v0: role contracts + task protocol.**
  - 2a `03-roles.md` (planner / implementer / evaluator contracts).
  - 2b `04-task-protocol.md` (state machine, claim token,
    idempotency, wire format).
  - *Chunk:* 2a+2b one chunk — roles reference task lifecycle and
    vice versa.
- **Phase 3 — Reference contracts package (`eden-contracts`).**
  - 3a Pydantic models for the 6 schemas.
  - 3b CI parity check (models ↔ schemas).
  - *Chunk:* 3a+3b one chunk.
- **Phase 4 — Spec v0: events + integrator + storage.**
  - 4a `05-event-protocol.md` + event schema.
  - 4b `06-integrator.md` (git topology invariants).
  - 4c `08-storage.md` (repository interface, durability).
  - *Chunk:* 4a+4b+4c one chunk — heavy cross-referencing
    (integrator emits events; storage persists them).
- **Phase 5 — In-memory reference dispatch loop.**
  - 5a In-memory task queue + event log.
  - 5b Scripted workers for all three roles, state machine driver.
  - 5c First conformance scenarios against the state machine.
  - *Chunks:* 5a one chunk; 5b+5c one chunk (scenarios need a worker
    harness to drive).
- **Phase 6 — Reference storage backend (`eden-storage`).**
  - 6a Repository interface (Python Protocol) per spec ch. 8.
  - 6b SQLite concrete impl + migration for the dispatch loop.
  - *Chunk:* 6a+6b one chunk.
- **Phase 7 — Reference git integrator (`eden-git`).**
  - 7a Port `git_manager.py` from direvo (worktree + branch ops).
  - 7b Integrator flow: `work/*` → squash → `trial/*` + eval manifest.
  - *Chunks:* 7a one chunk (port is self-contained); 7b one chunk.
- **Phase 8 — Cross-process reference.**
  - 8a Wire-protocol definition (HTTP + schemas) + orchestrator
    standalone consuming it.
  - 8b Worker hosts (planner, implementer, evaluator) as standalone
    processes.
  - 8c Cut-over: remove in-proc paths; only wire-protocol remains.
  - *Chunks:* 8a one chunk (protocol+first consumer coupled); 8b one
    chunk; 8c one chunk.
- **Phase 9 — Reference Web UI.**
  - 9a Shell + auth stub + navigation + experiment list.
  - 9b Planner module (claim / markdown form / submit).
  - 9c Implementer module (claim / manifest / submit SHA).
  - 9d Evaluator module (claim / metrics form / artifact upload).
  - 9e Observability views + admin-reclaim action.
  - *Chunks:* 9a+9b one chunk (first role establishes the pattern);
    9c one; 9d one; 9e one.
- **Phase 10 — Reference Compose stack.**
  - 10a Infrastructure containers (Postgres, Gitea, blob volume) +
    Compose skeleton.
  - 10b Each reference service dockerized.
  - 10c Setup script (registers experiment end-to-end).
  - 10d LLM worker hosts (planner context-accumulating, implementer
    sandbox-spawning).
  - 10e End-to-end integration test in Compose.
  - *Chunks:* 10a one; 10b+10c one chunk; 10d one; 10e one.
- **Phase 11 — Conformance suite v1.**
  - 11a Harness (test scaffold + implementation-under-test adapter).
  - 11b State-machine scenarios (task lifecycle, claim tokens,
    transactional events).
  - 11c Role-contract scenarios (per-role submission semantics).
  - 11d Integrator scenarios (squash shape, eval-manifest shape).
  - *Chunks:* 11a+11b one chunk (harness needs first scenarios to
    validate itself); 11c one; 11d one.
- **Phase 12 — Multi-experiment (leases, control plane, switcher).**
  Units and chunking will be named closer to the time — too far
  ahead to estimate coupling accurately.
- **Phase 13 — Kubernetes reference deployment.** Same note.

Each phase and unit lists its exit criterion and what it explicitly
does *not* do, to prevent creep.

### F. Root docs

- **`README.md`** — three-sentence elevator pitch + "EDEN is a protocol
  for directed-code-evolution orchestration; this repo ships the spec,
  a reference implementation, and a conformance suite. Anyone can
  build a conforming component in any language." Links to `spec/`,
  `reference/`, `conformance/`, `docs/roadmap.md`. Current status:
  "Phase 0 (bootstrap); nothing runnable yet."
- **`AGENTS.md`** — agent-facing. Layout tour with the spec/reference/
  conformance split made loud. Commands section lists only what
  currently works (markdownlint) and calls out what's planned
  (`uv sync`, `ruff`, `pyright`, `pytest`, schema validation — to
  be added in Phase 3+). Contribution conventions: **spec edits
  require extra care** (versioned, normative, reviewed);
  **reference edits are normal code changes**; **schema changes
  must update the spec and the Pydantic bindings in lockstep** (CI
  enforces this starting in Phase 3).
- **`CONTRIBUTING.md`** — two-path structure mirroring the repo
  structure: "Contributing to the spec" (RFC discipline, change
  proposals, versioning) and "Contributing to the reference
  implementation" (standard code-review workflow).
- **`STYLE_GUIDE.md`** — ported from direvo.
- **`CLAUDE.md`** — symlink to `AGENTS.md`.

### G. Build & CI config

**Bootstrap is docs-only**, so CI runs exactly one check.

- `.github/workflows/ci.yml` — single workflow named `ci`, single job
  named **`docs-lint`**. That job name is also the required status
  check in branch protection (pinned explicitly so the `gh api` call
  is deterministic).
- The `docs-lint` job:
  1. Checks out.
  2. Runs exactly this command (same in CI and local verification):

     ```bash
     markdownlint-cli2 "**/*.md" "#node_modules" "#.venv" "#docs/archive/**" "#docs/plans/review/**"
     ```

     Configuration comes from the repo's `.markdownlint.json`. Two
     deliberate exclusions:
     - `docs/archive/**` — archived historical documents (moved, not
       rewritten) should not be held to the new lint rules; that
       would be busywork against the archive's purpose.
     - `docs/plans/review/**` — `/codex-review` transcripts are
       tool-generated faithful records of review sessions; reformatting
       them to satisfy markdownlint would damage their value as
       records, and the rules they commonly violate (MD036
       emphasis-as-heading, MD041 no-h1, MD040 fenced-code-language)
       reflect Codex's output style, not a repo quality concern.

     The `#node_modules` and `#.venv` exclusions guard against noise
     from future dependency installs.
  3. Exits 0 if all markdown passes.
- **No Python job** at bootstrap (no Python files exist).
- **No JSON Schema job** at bootstrap (no schemas exist, and an
  empty-glob job is brittle — either a find returning nothing exits 0
  quietly and hides a real misconfiguration, or the tool errors on
  no-input. Easier to add this job in Phase 1 when the first schema
  lands.)
- **Later phases add jobs**, each as a distinct job name (so branch
  protection can require each one as it stabilizes):
  - Phase 1: `schema-validity` — validates `spec/v0/schemas/*.schema.json`.
  - Phase 3: `python-lint` (ruff), `python-typecheck` (pyright),
    `python-test` (pytest) — once the first package exists and has a
    real test.
  - Phase 3+: `schema-parity` — Pydantic models match their JSON
    Schemas.
  - Phase 11: `conformance` — runs the conformance suite against
    the reference impl.

### H. Git + GitHub execution

Same as before:

1. `git init -b main`
2. Initial commit containing all of the above.
3. `gh auth status`.
4. `gh repo create eden --private --source . --remote origin --push \
   --description "A protocol for directed code evolution orchestration"`.
5. `gh repo edit --add-topic protocol --add-topic specification \
   --add-topic directed-evolution --add-topic orchestration \
   --add-topic ai --add-topic research-automation`.
6. Wait for the first `docs-lint` run on `main` to complete (so the
   check name is registered in GitHub's status-check cache), **then**
   enable branch protection.
7. Branch protection uses **classic branch protection** (not the
   newer repository rulesets — classic is better-documented for
   scripted setup and the two tools can coexist). Call:

   ```bash
   gh api -X PUT "repos/<user>/eden/branches/main/protection" \
     -H "Accept: application/vnd.github+json" \
     --input - <<'JSON'
   {
     "required_status_checks": {
       "strict": true,
       "contexts": ["docs-lint"]
     },
     "enforce_admins": false,
     "required_pull_request_reviews": {
       "required_approving_review_count": 0,
       "dismiss_stale_reviews": false,
       "require_code_owner_reviews": false
     },
     "restrictions": null,
     "allow_force_pushes": false,
     "allow_deletions": false
   }
   JSON
   ```

   Rationale for each field:
   - `contexts: ["docs-lint"]` — exact CI job name pinned in section G.
     Added to as Phase 3 / Phase 11 introduce more jobs.
   - `enforce_admins: false` — solo-repo hotfix path stays open.
   - `required_approving_review_count: 0` — solo contributor;
     bumping this to 1+ would block every merge. Increase when
     collaborators arrive.
   - `strict: true` — branches must be up-to-date with `main` before
     merging; cheap safety on a low-traffic repo.
   - `allow_force_pushes: false`, `allow_deletions: false` — keep
     `main` durable.

## Files to Reference

- `direvo/pyproject.toml` — ruff/pyright config template.
- `direvo/.gitignore` — baseline.
- `direvo/STYLE_GUIDE.md` — copy verbatim.
- `direvo/AGENTS.md` — structural model for the new one (content
  differs because the project framing differs).
- `eden/docs/naming.md` — source for README's elevator pitch.
- `eden/docs/plans/eden-microservices-refactor.md` — source of
  functional/non-functional requirements for the spec. Read before
  writing spec; then move to `docs/archive/`.
- RFC 2119 — the source for MUST/SHOULD/MAY language conventions used
  in spec prose.

## Verification

Before declaring done:

1. `tree -L 3` shows: `spec/`, `reference/`, `conformance/`, `docs/`
   at top level; `spec/v0/` present; `docs/archive/microservices-refactor-plan.md`
   present; old `docs/plans/eden-microservices-refactor.md` gone.
2. `readlink CLAUDE.md` prints `AGENTS.md`.
3. The exact CI command passes locally:
   `markdownlint-cli2 "**/*.md" "#node_modules" "#.venv" "#docs/archive/**" "#docs/plans/review/**"`.
   (Running the identical command locally avoids works-locally /
   fails-in-CI drift.)
4. No Python-tool gates run — there are no Python files. (Intentional:
   those gates land with Phase 3.)
5. Remote: `gh repo view <user>/eden --json visibility,description,url`
   confirms private + correct description.
6. `gh run list --limit 1` shows the first CI run; `gh run view` shows
   the `docs-lint` job green.
7. `gh api repos/<user>/eden/branches/main/protection` returns the
   exact payload from section H step 7 — in particular
   `required_status_checks.contexts == ["docs-lint"]`.

## Execution Order

Each step lists the **exact** files it creates/moves. Nothing outside
this list is written at Phase 0.

1. Read direvo's `AGENTS.md`, `pyproject.toml`, `STYLE_GUIDE.md` in full.
2. ~~Create `docs/archive/` and move the old microservices plan in with a
   historical-context header.~~ **Already done (2026-04-22) as part of
   the docs cleanup — the old direvo-origin plans and PRDs were deleted
   and only the microservices plan was archived.** No action needed here
   at Phase 0 execution time.
3. Create empty directory tree with `.gitkeep` files per the section
   A.1 tree. Directories: `spec/v0/schemas/`, every `reference/services/*`
   and `reference/packages/*` directory, `reference/scripts/setup-experiment/`,
   `reference/compose/`, `tests/unit/`, `tests/integration/`,
   `.github/workflows/`. No `.gitkeep` in directories that contain a
   real file (e.g. `spec/v0/` contains `README.md`).
4. Write section READMEs:
   - `spec/README.md` — what the EDEN protocol is, versioning policy,
     pointer to `v0/README.md`.
   - `spec/v0/README.md` — the planned-chapters table from section A.2,
     each annotated with "(Phase N)".
   - `reference/README.md` — "one valid implementation", target spec
     version, per-service/package Phase-N map.
   - `conformance/README.md` — stub; "suite lands Phase 11".
   - **No numbered spec chapter files, no `*.schema.json` files.**
5. Write `.gitignore` (port from direvo) and `.markdownlint.json`
   (copy direvo's `{"MD013": false}`). For the review-artifact
   directory under `docs/plans/review/`: **track `*-review.md` and
   `*.patch` files** (the durable review content and diffs);
   **ignore `*.jsonl`, `*.stderr`, `*.stdout`** (Codex tool
   transcripts — large, rarely re-read, regenerable). `.gitignore`
   rules: `docs/plans/review/**/*.jsonl`,
   `docs/plans/review/**/*.stderr`, `docs/plans/review/**/*.stdout`.
6. Write root `README.md`, `AGENTS.md`, `CONTRIBUTING.md`,
   `STYLE_GUIDE.md`. **No `pyproject.toml`**, **no `.python-version`**
   — those land in Phase 3.
7. Create `CLAUDE.md` as a relative symlink to `AGENTS.md`.
8. Write `docs/roadmap.md` (Phase 0–13 with exit criteria and the
   section-A.2 file-to-phase mapping).
9. Write `.github/workflows/ci.yml` — the `docs-lint` job only, per
   section G. Pin `markdownlint-cli2` to a specific major version.
10. Run local verification (steps 1–3 of Verification above).
11. `git init -b main`, commit everything.
12. `gh auth status` sanity check (stop if unauthenticated).
13. `gh repo create … --push`.
14. `gh repo edit --add-topic …`.
15. `gh run watch` the first CI run; fix anything red before enabling
    branch protection.
16. Enable branch protection via `gh api` with the exact payload in
    section H step 7.
17. Report back: repo URL, the protocol-first framing summary, what
    Phase 1 looks like.

## Out of Scope for This Bootstrap

- **Any spec chapter content.** Phase 0 writes `spec/README.md` and
  `spec/v0/README.md` only. **No `00-overview.md`, `01-concepts.md`,
  or any other numbered chapter file is created.** Those land in
  Phases 1, 2, 4, 11, 12 per the section-A.2 mapping.
- **Any JSON Schema files.** `spec/v0/schemas/` is empty (just
  `.gitkeep`) at end of Phase 0. First schema lands in Phase 1.
- **Any reference code.** `reference/` tree has `.gitkeep`s and a
  top-level `README.md` only.
- **Any conformance tests.** Just the `conformance/README.md`.
- **Python toolchain** — `pyproject.toml`, `.python-version`, uv
  workspace, ruff/pyright config, per-member `pyproject.toml` files.
  All land in Phase 3 with the first real package.
- **Python / JSON Schema CI jobs** — added per-phase as the artifacts
  they gate on come into existence.
- Pre-commit hooks, issue templates, CODEOWNERS.
- Porting the fixture experiment — Phase 1.
- Moving/renaming anything inside `direvo/` (untouched).

## Known Risks / Things to Watch

- **Branch protection before the first CI run on `main`.** GitHub
  won't let you require a status-check context it has never seen.
  Order is hard-required: push → wait for `docs-lint` to complete on
  `main` → enable protection. The `gh run watch` step enforces this.
- **`gh auth status` failing.** Surface clearly and stop; don't guess
  at credentials.
- **Protocol versioning discipline from day 0** — tempting to write
  `spec/` without a `v0/` subdirectory "for now." Resist: adding the
  version layer later means rewriting every cross-reference.
- **"Reference" naming overload.** `reference/` (top-level) is the
  reference *impl*; `docs/archive/` (not `docs/reference/`) holds the
  old plan. Avoiding the collision is why the old plan goes to
  `archive/`, not `reference/`.
- **Classic branch protection vs. rulesets.** Both exist; rulesets
  are GitHub's newer system. This plan uses **classic protection**
  because it's better-supported by `gh api` one-liners and the solo
  use case doesn't need rulesets' evaluate-before-enforce mode. If
  the user later wants rulesets, migration is a drop-in replacement
  of the `gh api` call.
- **`markdownlint-cli2` version pinning.** Use a specific major in
  CI (e.g., `markdownlint-cli2@v0.14`) so Phase 0 doesn't silently
  break when the tool upgrades.
