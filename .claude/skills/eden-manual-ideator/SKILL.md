---
name: eden-manual-ideator
description: 'Drive the EDEN ideator role end-to-end from the terminal. Trigger phrases: "play ideator", "draft an idea", "act as ideator", "submit an idea", "I want to ideate".'
---

# EDEN Manual — Ideator Role (CLI)

## When to use

The user is running an EDEN reference Compose stack with no automated
ideator-host and wants to play the ideator role from the terminal.
Trigger phrases above. The Compose stack must be up (`docker compose
--env-file .env ps` should show postgres / gitea / task-store-server /
orchestrator / web-ui all healthy).

## What "ideator" means

An ideator claims an `ideation` task, drafts one or more *ideas* (slug,
priority, parent_commits, content), and submits them. Each ready idea
becomes an `execution` task once the orchestrator dispatches it.

Everything below runs from the terminal. Do not ask the user to open the
web UI.

## Workflow

The CLI lives at `reference/scripts/manual-ui/eden-manual` (relative to
the repo root). Use the absolute path when invoking from the user's
shell to avoid cwd-related fragility:

```text
EDEN=/Users/ericalt/Documents/eden-worktrees/test-main/reference/scripts/manual-ui/eden-manual
```

(adjust the worktree root if the user's path differs).

### Phase 1: Gather context (automatic)

Run all three in parallel — present a unified digest to the user:

```bash
$EDEN list-tasks --kind ideation --state pending
$EDEN list-commits
cat /Users/ericalt/Documents/eden-worktrees/test-main/reference/compose/experiment-config.yaml
```

Show:

- Pending ideation tasks.
- Available parent commits (base + integrated variants).
- The experiment's `objective` and `evaluation_schema` (so user knows
  what shapes a *good* idea).

### Phase 2: Pick a task and claim it (automatic)

Default: claim the first pending ideation task. If multiple, briefly
mention the others. Don't ask unless the user has expressed a preference.

```bash
$EDEN claim <task-id> --worker-id eden-manual
```

Post-12a-1: claim ownership is identity-keyed (no per-claim opaque token). The CLI persists `{worker_id}` per task in `/tmp/eden-manual/.claims.json` so the submit step picks the matching worker bearer (cached at `/tmp/eden-manual/.credentials.json`).

### Phase 3: Elicit ideas from the user (judgment)

Ask the user — concisely, one prompt — for:

- How many ideas to draft (default 1).
- For each: slug, parent_commits (suggest from `list-commits`), priority
  (default 1.0), and content.

If the user is vague, suggest a concrete idea yourself and confirm
before proceeding.

### Phase 4: Submit (automatic)

Build a JSON file at `/tmp/eden-manual/.ideas.json` with the user's
drafted values:

```json
{
  "ideas": [
    {
      "slug": "...",
      "priority": 1.0,
      "parent_commits": ["..."],
      "content": "..."
    }
  ]
}
```

Submit:

```bash
$EDEN ideation-submit <task-id> --ideas-file /tmp/eden-manual/.ideas.json
```

Surface the resulting `idea_ids` to the user.

### Phase 5: Verify (automatic)

```bash
$EDEN list-tasks --kind execution --state pending
```

Within ~1 second the orchestrator should dispatch each ready idea into
a new `execution` task. Confirm to the user.

If nothing appears within ~5 seconds, check orchestrator logs:

```bash
cd /Users/ericalt/Documents/eden-worktrees/test-main/reference/compose && \
    docker compose --env-file .env logs --tail 40 orchestrator
```

## Best practices

- **slug rules**: lowercase, kebab-case, must match `^[a-z0-9][a-z0-9-]*$`.
- **One idea per submit at first.** Multi-idea submission works but
  failure modes are gnarlier. Single-shot is the default.
- **parent_commits is reachability-checked at executor-submit time** —
  always pick a value from `list-commits` so the executor doesn't fail
  on a nonsense SHA.
- **Content is the spec** the executor reads. Prefer concrete language
  over vague ("add a single line to README" beats "improve docs").
- **`wire error 401` on `/workers...`?** The running stack's
  `EDEN_ADMIN_TOKEN` has diverged from the `.env` file the CLI is
  reading. Bounce the stack against the current `.env`, or re-checkout
  the worktree the stack was brought up against.
