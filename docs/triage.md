# EDEN issue triage

This document is the operating manual for the GitHub issue tracker. It covers the label axes in use, when to apply each label, and the heuristics that keep the backlog navigable as it grows.

If you're filing an issue, read [§1 Label axes](#1-label-axes) + [§2 When labels are applied](#2-when-labels-are-applied). If you're triaging a batch of issues, read all of it.

## 1. Label axes

Four axes capture distinct facets of an issue. Each axis has a small fixed value set.

### Type

| Label | Meaning |
|---|---|
| `bug` | Something isn't working as the spec / docs / code says it should. |
| `enhancement` | New feature, behavior, or interface. |
| `documentation` | A change to docs that doesn't change behavior. |
| `question` | A question, not a change request. Usually closed once answered. |
| `duplicate` | Tracked elsewhere; closed pointing at the canonical issue. |
| `manual-ui` | Operator-discovered (from manual UI / CLI use). Migrated from `MANUAL_UI_ISSUES.md` and continued for new operator-discovered items. Compatible with the `bug` / `enhancement` axis. |
| `wontfix` | Intentionally not addressed. Closed. |

### Triage disposition (`triage:*`)

Captures where an issue sits in the design → implementation pipeline.

| Label | Meaning |
|---|---|
| `triage:ready` | Scope is clear; an implementer could pick this up and ship. |
| `triage:needs-plan` | Substantive design surface remaining. Plan before implementing. |
| `triage:in-flight` | A PR or branch is actively addressing this. |
| `triage:deferred-future` | Tracked but not actionable now (depends on prerequisites, or is aspirational). |

### Priority (`priority:*`)

Urgency, NOT importance.

| Label | Meaning |
|---|---|
| `priority:0-blocking` | Blocks an operator workflow today. Rare; if more than 2-3 issues are `priority:0` simultaneously, the project is in firefighting mode. |
| `priority:1-near-term` | Near-term ergonomic / demo polish. Should land within the current phase or the next. |
| `priority:2-planned` | Planned roadmap work. Well-scoped; not urgent. |
| `priority:3-future` | Aspirational / deep-future / blocked on prerequisites. |

### Cluster (`cluster:*`)

Groups related issues whose work coordinates. Cluster only when **≥3 issues share a workflow** — don't pre-create clusters with <3 members.

Today's clusters:

| Label | Coordination surface |
|---|---|
| `cluster:identity` | Operator / worker identity, auth, multi-experiment. |
| `cluster:durability` | Substrate persistence, recovery, checkpoints, artifacts substrate. |
| `cluster:observability` | Logs, dashboards, search, exploration tools. |

New clusters get created when a third issue joins a previously-2-member set. Adding a cluster label is a triage action, not a filing action.

## 2. When labels are applied

At issue creation, the filer applies:

- **Type** (`bug` / `enhancement` / etc.) — required.
- **`manual-ui`** — when applicable.
- **`cluster:*`** — if the cluster fit is obvious. Otherwise leave to triage.

At triage pass, the triager applies:

- **`triage:*`** — after a quick scope read. Default to `triage:needs-plan` if design surface remains; `triage:ready` if scope is unambiguous.
- **`priority:*`** — based on operator impact and urgency. Use `priority:1-near-term` if uncertain.
- **`cluster:*`** — if not already applied and a cluster fits (≥3-member rule).

During implementation:

- **`triage:in-flight`** — added when a PR or branch is opened against the issue. Removed when the issue closes.

## 3. Heuristics

### Cluster creation

- Default: no cluster.
- Adding a cluster: when a third issue would share a workflow with two existing ones. The triggering moment is the third issue, not the first.
- Don't pre-create clusters speculatively (e.g., `cluster:ops-ux`) — they age out as labels-without-issues.

### Closing as subsumed

When another issue's scope absorbs this one cleanly:

1. Add a closing comment pointing at the canonical issue: "Subsumed by #N — that issue's [section] covers [what this proposed]."
2. Close with reason "not planned" (GitHub's term for "won't fix as a separate issue").
3. The canonical issue's body or comments should mention what it absorbed (so the cross-reference is bidirectional).

Recent examples: #135 + #138 were closed-as-subsumed by #137's design refinement that put slug in its own column and aggregated artifact links into an expanded-row section.

### When `priority:0-blocking` is appropriate

"Blocks an operator workflow today" means: with current state, the operator literally can't complete the task they're trying to do. Examples:

- A substrate-durability regression discovered after a Docker Desktop restart (#129 — was `priority:0` until fixed).
- A wire endpoint returning 500 instead of a structured error (when the operator can't recover).

`priority:0` should NOT be used for:

- Bugs that have a workaround (use `priority:1` and document the workaround).
- Bugs that affect a non-critical surface (use `priority:1` or `priority:2`).
- Bugs in a deferred / aspirational surface (use `priority:3`).

If more than 2-3 issues are simultaneously `priority:0`, the project is in firefighting mode — stop and reduce.

### `priority:3-future` vs `triage:deferred-future`

- `priority:3-future` is the urgency: "this isn't current-phase work."
- `triage:deferred-future` is the disposition: "we know about this; we're not going to act on it soon."

They overlap heavily; most `priority:3` items are also `triage:deferred-future`. The distinction lets a `priority:3` item still be `triage:ready` if its scope is clear but it's not current-phase (e.g., a Phase 13 implementation issue with a clear plan).

## 4. The deferral-tracking rule

Every CHANGELOG-narrated deferral lands as a GitHub issue at the moment of deferral. The CHANGELOG entry references the issue by number.

**Why:** during the 2026-05-22..23 audit, at least 7 deferrals were found that lived only in CHANGELOG narratives and never made it to GitHub issues. Each was a latent backlog item invisible to issue triage. Markdown notes don't surface in `gh issue list`; they don't get prioritized in roadmap meetings; they drift out of mind.

**How to apply:**

- When a chunk lands with deferred items — features, fixes, or polish the chunk explicitly chose not to address — file each deferral as a GitHub issue **as part of the chunk's merge**, not as a follow-up.
- The CHANGELOG entry's "deferred items" prose references each filed issue: "Deferred: X — see #N."
- During chunk-completion review, the reviewer checks that every deferral phrase in the CHANGELOG entry has a matching issue link within the same entry.
- Phrases that trigger this check include: `deferred`, `not yet`, `future revision`, `follow-up`, `out of scope`. If the prose contains one of these without an issue link, the deferral isn't tracked.

## 5. Standard backlog views

The recurring `gh issue list` queries useful during planning:

| View | Query |
|---|---|
| Quick wins (ready + soon) | `gh issue list --label 'triage:ready' --label 'priority:1-near-term'` |
| Architectural work in flight | `gh issue list --label 'cluster:identity' --label 'priority:2-planned'` |
| Parking lot | `gh issue list --label 'priority:3-future'` |
| Needs scoping | `gh issue list --label 'triage:needs-plan'` |
| All open by cluster | `gh issue list --label 'cluster:durability'` |

A small `scripts/triage-matrix.sh` could generate the priority×cluster table that comes up during planning passes; not yet implemented.

## 6. Where this doc is referenced

This doc isn't useful unless a user / agent can find it. Cross-references live at:

- [`AGENTS.md`](AGENTS.md) — `Commit guidelines` + `Related docs` sections.
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — issue-filing guidance.
- `.github/ISSUE_TEMPLATE/*.md` — header note on every issue creation form.
- `.github/PULL_REQUEST_TEMPLATE.md` — reference under "Related issues."

If a new surface is added where someone might look for label conventions, add a cross-reference there too.
