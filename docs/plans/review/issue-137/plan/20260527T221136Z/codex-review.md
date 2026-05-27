# Codex plan-review record — issue #137 pending-task-list redesign

- **Plan:** [`docs/plans/issue-137-pending-task-list-redesign.md`](../../../../issue-137-pending-task-list-redesign.md)
- **PR:** [#239](https://github.com/ealt/eden/pull/239)
- **Branch:** `plan/issue-137-task-list-redesign`
- **Reviewer:** Codex (codex-rescue), plan-stage review
- **Rounds:** 4 (round 0 NEEDS_REVISION → … → round 3 NEEDS_REVISION → **round 4 APPROVED**)
- **Date:** 2026-05-27

This file records the durable summary of the iteration. Per `.gitignore`, regenerable
transcripts (`*.jsonl` / `*.stdout` / `*.stderr` / `prompt.txt`) are not committed.

## Round 0 — NEEDS_REVISION (3 blocking + 1 non-blocking)

1. **Eligibility predicate ≠ real claim predicate; wrong group-resolution error model.** The §3.5
   ladder checks *registration first, then target*; `resolve_worker_in_group` returns `False`
   (not raises) for unknown worker/group and only propagates transport/auth errors.
2. **Sort contract self-contradictory.** "created by" implied sortable but only `sort=created`
   (=created_at) was allow-listed; degraded-row `-inf`/`""` sentinels sort to the *top* under
   ascending order.
3. **Expansion links assume surfaces that don't exist.** Forgejo browser browse-URL + hardcoded
   `idea.md` artifact link.
4. *(non-blocking)* Eligibility-resolution failures folded into the "idea read failed" counter
   would mislead.

**Fix (commit 253d167):** registration-first predicate resolved once per render; corrected
error model (transport-only catch, NotFound = legitimate False); sort axes = {priority, slug} with
created_at as implicit tiebreak + degraded-row partitioning (direction-safe); expansion re-scoped
to existing surfaces; Forgejo browse-URL deferred; split warning counters (new §D.6).

## Round 1 — NEEDS_REVISION (2 blocking + 2 non-blocking)

1. **Direct-file artifacts dropped.** D.3 covered only the bundle/manifest path; `serve_artifact`
   also serves single-file `?uri=...` artifacts.
2. **Wave 3 e2e vs disabled-button contract.** A disabled button has no clickable claim path.
3. *(non-blocking, later proven correct)* "ideas/variants have index pages only" — flagged as
   stale.
4. *(non-blocking)* Risks section still said "explicit sentinel keys."

**Fix (commit e01e472):** D.3 covers both bundle and direct-file artifact shapes; Wave 3 split
into a forged/direct-POST route-level "no 500" regression + a rendered-page disabled-button e2e;
Risks updated to partitioning.

## Round 2 — NEEDS_REVISION (1 blocking + 2 non-blocking)

1. **idea/variant per-id detail pages DO exist** — `/admin/ideas/{idea_id}/` (observability.py:489)
   and `/admin/variants/{variant_id}/` (observability.py:301); the round-1 grep missed them
   because the decorators are split across two lines. (Verified against source before adopting.)
2. *(non-blocking)* §4 + §12 still used the old single-e2e wording.
3. *(non-blocking)* non-`file://` URIs could be direct external links.

**Fix (commit 8955a35):** expansion links idea/variant to their per-id detail pages; §11 followup
removed; §4 + §12 synced to the two-test shape; non-`file://` URIs render as external links.

## Round 3 — NEEDS_REVISION (1 blocking)

1. **Registration probe (`read_worker`) lacked a failure ladder** — a transport-indeterminate
   read could escape as a 500 before any per-row fallback.

**Fix (commit a4aa58c):** D.4 specifies the `read_worker` 3-outcome ladder (succeeds → registered;
NotFound → all-ineligible + note; transport/auth → page-level "eligibility unknown" degraded
render + §D.6 counter; never 500). Group probes skipped on a registration-unknown page.

## Round 4 — APPROVED

> The round-4 D.4 revision resolves the last load-bearing gap … No remaining plan-stage blockers
> in the current document. The plan … is cleared for implementation.

## Notes for the implementer

- Two issue-body premises were verified **stale** against source and scoped around (§1.1): the
  "ineligible claim → HTTP 500" premise (already fixed; server-side prong is test-only) and the
  closed-issue subsumption (#135/#138/#132). #128 is open and independent.
- The eligibility projection mirrors the §3.5 ladder but does not re-specify it; the store remains
  authoritative. Watch the group-probe wire-cost (memoize) and the two distinct degraded paths
  (read-failure vs eligibility-unknown).
