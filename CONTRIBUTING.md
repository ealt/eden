# Contributing to EDEN

Thanks for your interest in EDEN.

EDEN is a **protocol** for directed evolution orchestration. This repo contains three kinds of artifact that call for different contribution discipline: the **specification** (`spec/`), the **reference implementation** (`reference/`), and the **conformance suite** (`conformance/`). Read the section that matches where your change lives.

## Current phase

**Phase 11 complete.** Chapters 00–09 of spec/v0 are on the protected `main`, alongside the full reference implementation (workspace under [`reference/`](reference/) — six services + five packages on a Compose stack with Postgres + Forgejo) and the conformance suite at the **v1+roles+integrator** level (110 scenarios green). CI gates the reference impl on docs-lint, schema-validity, schema-parity, python-lint, python-typecheck, python-test, python-test-postgres, conformance, compose-smoke, compose-smoke-subprocess, compose-smoke-subprocess-docker, and compose-e2e. See [`CHANGELOG.md`](CHANGELOG.md) for the canonical per-chunk "what's done" record, [`AGENTS.md`](AGENTS.md) for the agent contract (commands, naming discipline, pitfalls), and [`docs/roadmap.md`](docs/roadmap.md) for the remaining Phase 12–13 plan.

If you want to contribute, useful areas are: spec gaps surfaced by [`docs/conformance-coverage.md`](docs/conformance-coverage.md), open issues labeled [`manual-ui`](https://github.com/ealt/eden/issues?q=is%3Aopen+label%3Amanual-ui), or scoping work for Phase 12 (multi-experiment / control plane).

## Contributing to the spec

The spec is the authoritative source of protocol semantics. A change to the spec is a change to what "EDEN" means.

### Spec conventions

- **Versioning.** `spec/v0/` is a single lineage. Within a version, changes should be additive or clarifying. Breaking changes go to a new version (`spec/v1/`, …), never in-place.
- **Normative language.** Use RFC 2119 keywords: **MUST**, **SHOULD**, **MAY**. If prose doesn't use one, it's informative.
- **Wire-format changes propagate.** A change to a JSON Schema file under `spec/v*/schemas/` must be reflected in the Markdown chapter and in the Pydantic bindings in `reference/packages/eden-contracts/`. CI's `schema-parity` job enforces that models and schemas agree on the accept/reject corpus and on round-trip emission.
- **No technology choices in normative text.** The spec talks about *semantics*, not mechanisms. "A conforming task store MUST provide atomic claim with linearizable semantics" — yes. "Uses Postgres `SELECT ... FOR UPDATE`" — no, that's reference-impl detail.

### Process

1. Open an issue describing the change and why. Spec changes benefit from discussion before implementation.
2. Draft the prose + schema change in a branch.
3. Open a PR; expect careful review. Reviewers will check: RFC language, cross-reference consistency, whether the change is additive or breaking, and whether the change needs a conformance scenario.

## Contributing to the reference implementation

Standard code-review workflow applies.

### Prerequisites

Contributors touching the reference implementation need:

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (run `uv sync` at the repo root to install the workspace virtualenv)
- Docker (for the Compose stack and the docker-backed CI smoke jobs)

The reference web UI is server-side Jinja with HTMX vendored under `reference/services/web-ui/src/eden_web_ui/static/` — there is **no** Node runtime requirement for the UI. Node is needed only for `npx markdownlint-cli2` (spec contributors); see [`AGENTS.md`](AGENTS.md#commands) for the exact pinned commands (version-matched to CI to avoid works-locally / fails-in-CI drift).

### Impl conventions

- Normative behavior in the reference impl must match the spec. If the impl is correct and the spec is wrong, the spec should change first.
- Pass the conformance suite (Phase 11+).
- Follow the [style guide](STYLE_GUIDE.md).

## Contributing to the conformance suite

The conformance suite lives under [`conformance/`](conformance/) at the v1+roles+integrator level (chunk 11d).

- Scenarios must be **implementation-agnostic** — they drive an implementation-under-test via its advertised protocol surface (the chapter-7 HTTP binding), not via language-specific hooks.
- A scenario must cite the spec paragraph it validates. The first line of its docstring carries the citation in the form `spec/v0/<chapter>.md §<sec>`; [`conformance/src/conformance/tools/check_citations.py`](conformance/src/conformance/tools/check_citations.py) gates this in CI.
- See [`docs/conformance-coverage.md`](docs/conformance-coverage.md) for the current MUST/SHOULD coverage matrix; new scenarios that close uncovered MUSTs are especially welcome.

## Operator-facing disciplines

EDEN's reference implementation has substantial operator-facing surface area (the manual-UI CLI, the web UI's operator routes, the experiment-lifecycle setup scripts, the public wire endpoints). Bugs in this surface are easy to ship past automated tests because the tests don't exercise the surface the operator actually touches. The two disciplines below close that gap.

### Fresh-operator walkthrough (per surface change)

Any PR that changes an operator-facing surface MUST include a fresh-operator walkthrough of the changed surface. The PR template's "Fresh-operator walkthrough" section is the record.

**What counts as an operator-facing surface.** A change qualifies if it touches any of:

- Web UI templates or route handlers under `reference/services/web-ui/`.
- Wire endpoints under `reference/packages/eden-wire/` whose contract a manual operator or third-party tool depends on.
- CLI subcommands under `reference/scripts/manual-ui/` (`eden-manual`, `eden-experiment`) — flags, output shape, error messages.
- Operator workflow docs (`docs/user-guide.md`, `docs/observability.md`, the `eden-manual-*` skills under `.claude/skills/`).
- Operator-visible defaults in `setup-experiment.sh`, compose configs, or environment variables that the operator supplies.

Internal-only changes (refactors with no operator-visible delta, library-internal renames, test-only additions) do NOT require a walkthrough — write "N/A — internal change only" in the PR's walkthrough section.

**Walkthrough shape.** Perform the operator workflow that touches the changed surface, AS A FRESH OPERATOR (no insider context, no autocomplete from session history). Use the canonical docs / skills as your only guide. Specifically:

1. Start from a clean state (e.g., `eden-experiment down --purge` then `eden-experiment up`).
2. Follow the operator-facing docs / skills as written — if a step is ambiguous or wrong, the walkthrough has surfaced a doc bug; file it.
3. Exercise the specific code path your PR changed.
4. Capture observations: terminal output, error messages, points of friction, any "I had to guess" moments.

**What the record looks like in the PR.** A short note in the "Fresh-operator walkthrough" section. Either "passed cleanly" with a 1-2 line summary of what was exercised, OR a list of issues the walkthrough surfaced (each filed as a GH issue per the deferral-tracking rule in [`AGENTS.md`](AGENTS.md)). A walkthrough that surfaced zero friction is the goal; one that surfaces friction is the system working.

### Operator dogfooding (scheduled ritual)

In addition to per-surface walkthroughs, run a periodic end-to-end manual-UI dogfooding session. Cadence target: monthly at minimum, or before any phase milestone that the operator UX gates on.

**Shape.** Run a complete experiment from `setup-experiment` through ideator → executor → evaluator → integrator, using the manual surfaces (`eden-manual` CLI + web UI operator routes) end-to-end. Don't follow a fixed script — let observed friction direct attention. Notes go into a brief retrospective doc; bugs and design gaps surface as GitHub issues with `cluster:*` and `priority:*` labels per [`docs/triage.md`](docs/triage.md).

**Output.** The dogfooding session is "successful" when (a) the experiment reaches a successful integrated variant and (b) the session has produced a list of operator-facing issues triaged into the backlog. Empty issue lists from a multi-hour session are a yellow flag — they typically mean the operator was running on insider context rather than reading docs.

**Why this is a separate discipline from the per-surface walkthrough.** The per-surface walkthrough catches regressions in the specific code path being changed. The scheduled dogfooding catches the integrative failures — surfaces that pass individually but compound into friction across a full workflow, or surfaces nobody changed recently but that have rotted relative to their docs.

## PR descriptions

Use the template at [`.github/PULL_REQUEST_TEMPLATE.md`](.github/PULL_REQUEST_TEMPLATE.md). The sections it requires aren't decorative — each one closes a class of past escape.

- **Summary** — the diff shows the "what"; explain the "why".
- **What this does NOT cover** — enumerate the gaps this PR deliberately leaves in place. Every deferred follow-up is a filed GH issue (see [`AGENTS.md`](AGENTS.md) "Deferrals MUST be tracked as GitHub issues" and [`docs/triage.md`](docs/triage.md) §4). Reviewers check that the section's deferral phrases each link to an issue. "No deferred items" is a valid value when there genuinely are none.
- **Fresh-operator walkthrough** — required when the PR changes an operator-facing surface (see above). "N/A — internal change only" otherwise, with a brief reason.
- **Test plan** — markdown checklist referencing the canonical commands in [`AGENTS.md`](AGENTS.md#commands). Narrowed subsets of `uv run pytest -q` are NOT a substitute for the literal pre-push gate; the compose smokes catch pipeline-shape regressions pytest can't reach.
- **Related issues** — `Closes #N` for issues this PR closes; `Refs #M` for related-but-not-closed.

When the PR's scope expands mid-flight (a one-line fix grows into a multi-commit refactor; a single-issue close grows into a cluster fix), rewrite the description rather than appending — multi-commit PRs need full-rewrite descriptions, not append-only logs.

## Questions

Open an issue.
