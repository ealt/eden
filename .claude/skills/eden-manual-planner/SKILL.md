---
name: eden-manual-planner
description: 'Drive the EDEN planner role end-to-end from the terminal, no web-UI. Trigger phrases: "play planner", "draft a plan", "act as planner", "submit a plan", "I want to plan".'
---

# EDEN Manual — Planner Role (CLI)

## When to use

The user is running an EDEN reference Compose stack with no automated
planner-host and wants to play the planner role from the terminal. Trigger
phrases above. The Compose stack must be up (`docker compose --env-file
.env ps` should show postgres / gitea / task-store-server / orchestrator /
web-ui all healthy).

## What "planner" means

A planner claims a `plan` task, drafts one or more *proposals* (slug,
priority, parent_commits, rationale), and submits them. Each ready
proposal becomes an `implement` task once the orchestrator dispatches it.

Everything below runs from the terminal. Do not ask the user to open the
web UI.

## Workflow

The CLI lives at `reference/scripts/manual-ui/eden-manual` (relative to
the repo root). Use the absolute path when invoking from the user's
shell to avoid cwd-related fragility:

```
EDEN=/Users/ericalt/Documents/eden-worktrees/test-main/reference/scripts/manual-ui/eden-manual
```

(adjust the worktree root if the user's path differs).

### Phase 1: Gather context (automatic)

Run all three in parallel — present a unified digest to the user:

```bash
$EDEN list-tasks --kind plan --state pending
$EDEN list-commits
cat /Users/ericalt/Documents/eden-worktrees/test-main/reference/compose/experiment-config.yaml
```

Show:
- Pending plan tasks.
- Available parent commits (base + integrated trials).
- The experiment's `objective` and `metrics_schema` (so user knows what
  shapes a *good* proposal).

### Phase 2: Pick a task and claim it (automatic)

Default: claim the first pending plan task. If multiple, briefly mention
the others. Don't ask unless the user has expressed a preference.

```bash
$EDEN claim <task-id> --worker-id eden-manual
```

The token is persisted in `/tmp/eden-manual/.claims.json` automatically.

### Phase 3: Elicit proposals from the user (judgment)

Ask the user — concisely, one prompt — for:
- How many proposals to draft (default 1).
- For each: slug, parent_commits (suggest from `list-commits`), priority
  (default 1.0), and rationale.

If the user is vague, suggest a concrete proposal yourself and confirm
before proceeding.

### Phase 4: Submit (automatic)

Build a JSON file at `/tmp/eden-manual/.proposals.json` with the
user's drafted values:

```json
{
  "proposals": [
    {
      "slug": "...",
      "priority": 1.0,
      "parent_commits": ["..."],
      "rationale": "..."
    }
  ]
}
```

Submit:

```bash
$EDEN plan-submit <task-id> --proposals-file /tmp/eden-manual/.proposals.json
```

Surface the resulting `proposal_ids` to the user.

### Phase 5: Verify (automatic)

```bash
$EDEN list-tasks --kind implement --state pending
```

Within ~1 second the orchestrator should dispatch each ready proposal
into a new `implement` task. Confirm to the user.

If nothing appears within ~5 seconds, check orchestrator logs:

```bash
cd /Users/ericalt/Documents/eden-worktrees/test-main/reference/compose && \
    docker compose --env-file .env logs --tail 40 orchestrator
```

## Best practices

- **slug rules**: lowercase, kebab-case, must match `^[a-z0-9][a-z0-9-]*$`.
- **One proposal per submit at first.** Multi-proposal submission works
  but failure modes are gnarlier. Single-shot is the default.
- **parent_commits is reachability-checked at implementer-submit time** —
  always pick a value from `list-commits` so the implementer doesn't
  fail on a nonsense SHA.
- **Rationale is the spec** the implementer reads. Prefer concrete language
  over vague ("add a single line to README" beats "improve docs").
