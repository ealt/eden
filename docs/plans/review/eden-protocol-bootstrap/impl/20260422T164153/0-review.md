**Findings**

- **Bug** — [docs/roadmap.md](/Users/ericalt/Documents/eden/docs/roadmap.md:258): Phase 9 does not match plan Section E. The plan defines five units: `9a` shell, `9b` planner module, `9c` implementer module, `9d` evaluator module, `9e` observability/admin reclaim, with chunks `9a+9b`, `9c`, `9d`, `9e`. The implementation collapses planner into `9a` and renumbers the rest as `9b`-`9d`. Fix by restoring the five-unit breakdown from Section E so roadmap units/chunks remain the plan’s tracking granularity.

- **Bug** — [docs/roadmap.md](/Users/ericalt/Documents/eden/docs/roadmap.md:288): Phase 10 also drifts from Section E. The plan defines separate units `10b` “dockerize each reference service” and `10c` “setup script,” chunked together as `10b+10c`; the implementation merges them into one `10b` and shifts later units down. Fix by restoring `10a`-`10e` exactly as designed so the roadmap preserves the intended unit/chunk decomposition.

- **Bug** — [README.md](/Users/ericalt/Documents/eden/README.md:26), [AGENTS.md](/Users/ericalt/Documents/eden/AGENTS.md:21), [CONTRIBUTING.md](/Users/ericalt/Documents/eden/CONTRIBUTING.md:13): These docs say “Phase 0 (bootstrap) complete,” but the review brief says steps 11-16 are still pending, and the repo currently has no commit yet. That is premature against plan Section H and the Execution Order. Fix by either delaying that claim until commit/push/CI/branch-protection are actually done, or softening it to say the scaffold is prepared and Git/GitHub execution is still pending.

- **Bug** — [AGENTS.md](/Users/ericalt/Documents/eden/AGENTS.md:32): The documented markdownlint command omits `"#docs/plans/review/**"` even though Section G and [ci.yml](/Users/ericalt/Documents/eden/.github/workflows/ci.yml:26) include it. “Matches CI exactly” is therefore false, and contributors following AGENTS can get different results from CI. Fix by updating the command string to match the workflow verbatim.

- **Risk** — [docs/naming.md](/Users/ericalt/Documents/eden/docs/naming.md:79): This page still says the Python reference implementation is published as `direvo`, that both `direvo` and `eden` work as CLI commands, and includes a registry-availability table “Checked 2025-03-27.” That conflicts with the Phase 0 docs that say there is no implementation yet. Fix by removing or clearly scoping this section as historical/future naming context, or moving package-availability claims out of the bootstrap-facing doc.

- **Risk** — [conformance/README.md](/Users/ericalt/Documents/eden/conformance/README.md:34): “Integrator scenarios ... branch protection semantics” does not match plan Section E / roadmap Phase 11, which calls for squash shape, eval-manifest shape, and `work/*` access discipline. Branch protection is GitHub repo policy, not protocol conformance. Fix by aligning this line with the roadmap language.

- **Nit** — [docs/roadmap.md](/Users/ericalt/Documents/eden/docs/roadmap.md:3): The opening sentence is broken by the leading `+` on line 5, so it renders as malformed prose/list syntax. Fix by making it a single paragraph or an intentional list.

**Assumptions / Notes**

- I reviewed against the current worktree state, not the intended post-review final state.
- All relative markdown links I checked resolve locally.
- I could not reproduce the exact CI lint environment: the installed local `markdownlint-cli2` here is `v0.20.0`, while CI pins `0.14.0`.

**Overall Assessment**

The Phase 0 scaffold itself is largely in place: the directory tree matches Section A.1, the section READMEs are present, the symlink is correct, and the CI workflow uses the planned docs-only command. I would not sign this off yet, though. The biggest problems are roadmap drift in Phases 9 and 10, premature “Phase 0 complete” status claims, and a few doc inconsistencies that will confuse future contributors if they land unchanged.
