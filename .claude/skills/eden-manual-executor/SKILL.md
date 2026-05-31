---
name: eden-manual-executor
description: 'Drive the EDEN executor role end-to-end from the terminal — claim, clone at parent, let user edit, then commit/push/submit via CLI. Trigger phrases: "play executor", "execute an idea", "act as executor", "I want to execute".'
---

# EDEN Manual — Executor Role (CLI)

## When to use

User is running the EDEN reference Compose stack with no automated
executor-host and wants to play the executor role from the terminal.
Trigger phrases above.

## What "executor" means

The executor takes one `execution` task (which references an idea),
realizes the idea as a git commit on top of `idea.parent_commits`,
pushes that commit + the canonical `work/<variant_id>-<slug>` ref to
forgejo, and records the variant.

Everything below runs from the terminal. Don't ask the user to open the
web UI.

## Workflow

```text
EDEN=/Users/ericalt/Documents/eden-worktrees/test-main/reference/scripts/manual-ui/eden-manual
```

### Phase 1: Pick a task (automatic)

```bash
$EDEN list-tasks --kind execution --state pending
```

Default: pick the first pending. If zero pending, suggest the user play
ideator first.

> The web-ui executor page is the point-and-click alternative to this
> CLI listing. Its pending-task table (issue #137) is a high-signal
> **slug / priority / target / created by** grid, priority-sorted by
> default, with **eligible for me** / **target** / **group by creator**
> filter chips and a per-row **context links** expander. Sort + filter
> state lives in the URL query string, so a curated view is shareable.
> When helping the user choose a task, mirror that signal here: lead
> with the highest-priority eligible task's slug.

### Phase 2: Inspect (automatic)

```bash
$EDEN show <task-id>
```

This returns task + idea + (if reachable) inline content text.
Present a digest:

- idea slug, priority, parent_commits
- the **content text in full** — the user needs to see the spec
- the experiment's objective from `experiment-config.yaml`

### Phase 3: Claim (automatic)

```bash
$EDEN claim <task-id> --worker-id eden-manual
```

The variant_id is persisted to `/tmp/eden-manual/.claims.json`
(post-12a-1, claim ownership is identity-keyed — no per-claim
opaque token; the CLI's worker bearer is cached at
`/tmp/eden-manual/.credentials.json`). The variant_id is stable
for the life of this claim.

### Phase 4: Clone at the parent commit (automatic)

```bash
$EDEN checkout <task-id>
```

Clones forgejo (with creds embedded) into `/tmp/eden-manual/<task-id>` and
checks out `idea.parent_commits[0]` in detached HEAD. Surface the
workdir path.

Offer to open it in the user's editor — ask "Cursor or VS Code?" if you
don't already know:

```bash
cursor /tmp/eden-manual/<task-id>
# or: code /tmp/eden-manual/<task-id>
```

### Phase 5: Wait for the user (judgment)

Wait for the user to say "done" / "ready to submit" / similar. Don't
write code unless the user explicitly asks — the user IS the executor.

When they're ready, optionally show what's about to be committed:

```bash
git -C /tmp/eden-manual/<task-id> status
git -C /tmp/eden-manual/<task-id> diff --stat
```

### Phase 6: Commit + push + submit (automatic)

Ask the user briefly for a commit message (default: derive from idea
slug, e.g. `execute: <slug>`). `push` derives the canonical work-branch
shape from the claim record + task — no `--branch` flag needed.

```bash
$EDEN push <task-id> --message "<msg>"
```

Capture the SHA from the JSON output. Then submit:

```bash
$EDEN execution-submit <task-id> --sha <sha> --description "<short note>"
```

This:

1. Fetches origin in the workdir.
2. Verifies commit exists + descends from declared parent_commits.
3. Refuses no-op variants (tree-identical to `parent_commits[0]`).
4. Creates a `Variant(starting, branch=work/<variant_id>-<slug>)`
   in the store. NOTE: `commit_sha` is NOT set at create_variant
   time — it's written atomically when the orchestrator accepts
   the submit (per spec ch03 §3.2 step 1).
5. Pushes `<sha>:refs/heads/work/<variant_id>-<slug>` to forgejo
   (idempotent — `push` above already pushed to the same ref).
6. Submits the execution task with `status=success` and the
   `commit_sha` in the payload.

### Phase 7: Verify (automatic)

The orchestrator dispatches an `evaluation` task within ~1 second; the
variant transitions to `success` when the evaluator submits, and the
integrator integrates it only after that.

```bash
$EDEN list-tasks --kind evaluation --state pending
```

Confirm an evaluation task for *this* variant appeared. Note the
task_id — the user can play evaluator next via `/eden-manual-evaluator`.

## Best practices

- **Don't pre-empt the user's code changes.** The whole value of the
  manual executor is human judgment. Wait for "done".
- **Don't pass `--branch` (it no longer exists).** `push` always
  pushes to the canonical `work/<variant_id>-<slug>` ref derived from
  the claim record + task. Earlier versions of the CLI accepted
  `--branch <slug>` and left an operator-leaking bare-slug branch on
  Forgejo; that was removed in #169.
- **Errors after `claim` but before `execution-submit`** leave the task
  claimed. The orchestrator's expired-claim sweeper recovers it
  automatically when `--ttl-seconds` is set; without TTL, use
  admin-reclaim or the `tasks/<id>/reclaim` wire endpoint.
- **If reachability check fails** (`commit X not reachable in workdir`):
  the user almost certainly forgot to push. Run `git -C <workdir> push
  origin <branch>` and retry execution-submit.
- **No-op variant guard**: if `execution-submit` exits with
  `variant tree is identical to parent_commits[0] tree`, the
  proposed variant doesn't actually change the parent's tree. Either
  produce a real change or — if the intent is to declare "I tried
  and failed" — use `--status error` instead.
- **`wire error 401` on `/workers...`?** The running stack's
  `EDEN_ADMIN_TOKEN` has diverged from the `.env` file the CLI is
  reading. Bounce the stack against the current `.env`, or re-checkout
  the worktree the stack was brought up against.
