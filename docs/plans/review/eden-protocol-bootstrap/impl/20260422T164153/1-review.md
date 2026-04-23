**Findings**

- **Risk** — [docs/naming.md](/Users/ericalt/Documents/eden/docs/naming.md:35): The package section is improved, but this doc still mixes planned and present-tense state. Lines 35-39 say “The repo ships one complete reference implementation ... plus a conformance suite,” and lines 102-111 present an availability table dated `2025-03-27`. That is still inconsistent with the rest of the Phase 0 docs, which say the implementation and suite are not here yet. Fix by making the whole implementation/package portion explicitly planned or historical until those artifacts actually exist.

- **Risk** — [AGENTS.md](/Users/ericalt/Documents/eden/AGENTS.md:34), [CONTRIBUTING.md](/Users/ericalt/Documents/eden/CONTRIBUTING.md:69), [ci.yml](/Users/ericalt/Documents/eden/.github/workflows/ci.yml:22): The command text now matches CI, but local verification is still version-sensitive. Running the exact command here with the installed `markdownlint-cli2 v0.20.0` produces 76 `MD060` table-style errors; CI is only green because it pins `0.14.0`. Fix by either documenting the required local version explicitly, e.g. `npx markdownlint-cli2@0.14.0 ...`, or by reformatting tables / config so newer markdownlint releases also pass.

- **Nit** — [AGENTS.md](/Users/ericalt/Documents/eden/AGENTS.md:21): The status sentence was softened correctly, but line 23 now starts with `+ push + branch protection...`, which renders as malformed prose/list syntax. Fix by making it a normal continuation of the sentence: “the commit + push + branch protection steps finish the phase.”

- **Nit** — [docs/roadmap.md](/Users/ericalt/Documents/eden/docs/roadmap.md:107): Phase 3 unit `3a` now has an accidental nested `- \`.python-version\`` on line 109. Section E describes this as an inline part of the unit’s file list, not a sub-bullet. Fix by restoring the inline wording: ``... + `reference/packages/eden-contracts/pyproject.toml` + `.python-version`...``.

**Overall Assessment**

The substantive round-0 issues are fixed: Phases 9 and 10 are back in sync with the plan, the premature “Phase 0 complete” claims are gone, the conformance wording matches the roadmap, and the roadmap opening prose is fixed. I’d treat this as close to sign-off, with one small follow-up pass still warranted for the lingering `docs/naming.md` inconsistency and the two formatting regressions above.