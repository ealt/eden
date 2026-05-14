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
pushes that commit + the canonical `work/<slug>-<variant_id>` ref to
gitea, and records the variant.

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

Token + variant_id are persisted to `/tmp/eden-manual/.claims.json`.
The variant_id is stable for the life of this claim.

### Phase 4: Clone at the parent commit (automatic)

```bash
$EDEN checkout <task-id>
```

Clones gitea (with creds embedded) into `/tmp/eden-manual/<task-id>` and
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
slug, e.g. `execute: <slug>`). Use `idea.slug` as the local branch name
to mirror the canonical work-branch shape.

```bash
$EDEN push /tmp/eden-manual/<task-id> --branch <slug> --message "<msg>"
```

Capture the SHA from the JSON output. Then submit:

```bash
$EDEN execution-submit <task-id> --sha <sha> --description "<short note>"
```

This:

1. Fetches origin in the workdir.
2. Verifies commit exists + descends from declared parent_commits.
3. Creates a `Variant(starting, branch=work/<slug>-<variant_id>,
   commit_sha=<sha>)` in the store.
4. Pushes `<sha>:refs/heads/work/<slug>-<variant_id>` to gitea.
5. Submits the execution task with status=success.

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
- **Use `idea.slug` as the local branch name.** It matches the
  canonical work-branch shape and avoids confusion when looking at
  gitea.
- **Errors after `claim` but before `execution-submit`** leave the task
  claimed. The orchestrator's expired-claim sweeper recovers it
  automatically when `--ttl-seconds` is set; without TTL, use
  admin-reclaim or the `tasks/<id>/reclaim` wire endpoint.
- **If reachability check fails** (`commit X not reachable in workdir`):
  the user almost certainly forgot to push. Run `git -C <workdir> push
  origin <branch>` and retry execution-submit.
