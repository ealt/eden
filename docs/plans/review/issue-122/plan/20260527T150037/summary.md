# Codex plan-review record — issue #122 baseline variant

- **Artifact:** `docs/plans/issue-122-baseline-variant.md`
- **Reviewer:** Codex CLI (`codex exec`, plan profile), session `019e6b74-64c4-7212-8af6-85e2a0188ec5`
- **Rounds:** 4 (0–3). Converged at round 3 ("this looks converged").
- **Snapshots:** `N.md` = plan state at round N; `N.patch` = diff from round N-1; `N-review.md` = Codex's review at round N. (`*.jsonl` / `*.stderr` are regenerable transcripts, dropped by `.gitignore`.)

## What each round changed

**Round 0 → 1 (feasibility blockers).** Codex stopped at level 2. Six grounded gaps:

1. Multi-experiment orchestrator mode (`multi_loop.py`) — a process-level seed-SHA env var can't carry a per-experiment seed. → Recorded `base_commit_sha` on the experiment runtime object, read by both modes (D.2).
2. `create_variant` is worker-auth — relaxing it to direct-success baseline lets any worker fabricate a passing baseline with arbitrary metrics. → Per-`kind` authority: `kind=baseline` create requires the orchestrators group (D.4.1).
3. Direct-success needs a real `05-event-protocol.md` amendment (`variant.started` requires `idea_id`; success-event semantics), not just an impl note. → Event-model amendment (D.4.3).
4. `idea_id` relaxation blast radius — event payloads, integrator/manifest, web-ui lineage, admin observability all assume `idea_id`. → Per-surface carve-out list (D.7).
5. Termination-drain carve incomplete — `multi_loop._experiment_is_drained_terminated` + `driver._integrate_successful_variants` also gate on outstanding-success-without-`variant_commit_sha`. → Three-path carve (D.5).
6. e2e tests assert exact counts (3 variants / 9 tasks). → Added to scope (§8.4).

**Round 1 → 2 (completeness).** Design accepted (level 3). Findings: the `base_commit_sha` field opened a propagation surface (checkpoints ch.10, all backends + schemas, experiment contract model, conformance seed) — enumerated as new §D.11; added conformance auth cases; e2e tests must partition baseline vs ordinary before asserting `variant_commit_sha`/`read_idea`; `ensure_baseline_variant` needs verified read-back not blind `AlreadyExists`; D.7 missed evaluation-task target inheritance.

**Round 2 → 3 (consistency).** One substantive: a migration contradiction (D.2 fail-fast vs migration "tolerated, no backfill"). → Reconciled to a single rule: absent `base_commit_sha` ⇒ skip with a warning, never fail-fast. Plus aligned `variant.started.kind` MUST/SHOULD and added `read_experiment` helper/docstring surfaces.

**Round 3 → final.** Two editorial nits (spec-file count, one stale D.4.3 sentence) — fixed post-review; no design change.

## Decisions deliberately left for operator sign-off (plan §2)

`kind` field name (vs `variant_kind`), default-on vs opt-in, unified `baseline:` block, reuse-`create_variant` vs new wire op, and the §D.11 store-field vs create-at-setup tradeoff. Codex explicitly kept default-on-vs-opt-in visible as the main strategic lever (smaller rollout surface) but did not block on it.
