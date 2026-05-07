---
name: eden-manual-evaluator
description: 'Drive the EDEN evaluator role end-to-end from the terminal — clone at trial commit, help the user inspect, then submit metrics via CLI. Trigger phrases: "play evaluator", "evaluate a trial", "act as evaluator", "score a trial", "I want to evaluate".'
---

# EDEN Manual — Evaluator Role (CLI)

## When to use

User is running the EDEN reference Compose stack with no automated
evaluator-host and wants to play the evaluator role from the terminal.
Trigger phrases above.

## What "evaluator" means

The evaluator takes one `evaluate` task (which references a trial whose
implementer-side work is committed and pushed), inspects the trial's
commit, and submits one of:

- `success` + a value for every metric in `experiment_config.metrics_schema`.
- `error` (the trial code itself errored — submission status records that).
- `eval_error` (the evaluator couldn't form a verdict, e.g. couldn't
  fetch the trial commit, can't measure, etc.).

After the evaluator submits `success`, the orchestrator transitions the
trial from `starting` to `success` and the integrator promotes it
(creating the canonical `refs/heads/trial/<id>-<slug>`).

Everything below runs from the terminal. Don't ask the user to open the
web UI.

## Workflow

```
EDEN=/Users/ericalt/Documents/eden-worktrees/test-main/reference/scripts/manual-ui/eden-manual
```

### Phase 1: Pick a task (automatic)

```bash
$EDEN list-tasks --kind evaluate --state pending
```

If empty, tell the user there's nothing to evaluate yet and suggest
finishing an implement cycle first.

### Phase 2: Inspect task + trial (automatic)

```bash
$EDEN show <task-id>
```

Present:
- The trial: `branch`, `commit_sha`, `parent_commits`, `description`.
- The metrics_schema (names + types) from `experiment-config.yaml`.
- The objective so the user knows the direction to score in.

### Phase 3: Claim (automatic)

```bash
$EDEN claim <task-id> --worker-id eden-manual
```

### Phase 4: Clone at the trial commit (automatic)

```bash
$EDEN checkout <task-id>
```

Workdir is `/tmp/eden-manual/<task-id>`, checked out at
`trial.commit_sha` in detached HEAD. Surface the path.

Optionally show the diff vs. the parent commit so the user has the
material context:

```bash
git -C /tmp/eden-manual/<task-id> log --oneline <parent_sha>..HEAD
git -C /tmp/eden-manual/<task-id> diff --stat <parent_sha> HEAD
git -C /tmp/eden-manual/<task-id> diff <parent_sha> HEAD
```

Offer to open in the user's editor.

### Phase 5: Wait for the user's verdict (judgment)

Wait. Don't suggest scores unless asked. If asked, frame as: "metric `X`
is type `<type>`; what value reflects how this trial does on that
dimension?" Don't fudge.

### Phase 6: Submit (automatic)

```bash
$EDEN evaluate-submit <task-id> --metric score=0.42 [--metric K=V ...]
```

For status=success, every metric in the schema must have a value. For
status=eval_error or error, omit `--metric`:

```bash
$EDEN evaluate-submit <task-id> --status eval_error
```

### Phase 7: Verify (automatic)

```bash
$EDEN show <task-id> | python3 -c 'import json,sys; print(json.load(sys.stdin)["task"]["state"])'
```

Should be `completed`. Within ~1 second, the integrator promotes the
trial — confirm:

```bash
curl -fsS -H "Authorization: Bearer $(grep '^EDEN_SHARED_TOKEN=' \
    /Users/ericalt/Documents/eden-worktrees/test-main/reference/compose/.env | \
    cut -d= -f2)" \
  -H "X-Eden-Experiment-Id: $(grep '^EDEN_EXPERIMENT_ID=' \
    /Users/ericalt/Documents/eden-worktrees/test-main/reference/compose/.env | \
    cut -d= -f2)" \
  http://localhost:8080/v0/experiments/$(grep '^EDEN_EXPERIMENT_ID=' \
    /Users/ericalt/Documents/eden-worktrees/test-main/reference/compose/.env | \
    cut -d= -f2)/trials | python3 -m json.tool
```

Look for `trial_commit_sha` populated on the trial — that's proof of
integration.

## Best practices

- **Read the diff, not just the description.** The implementer's
  `description` is unverified; `diff <parent_sha> HEAD` is ground truth.
- **`eval_error` is honest** when you can't form a verdict. Do not
  fabricate a score to submit `success`.
- **Use the trial's own `parent_commits`** as the diff base, not the
  experiment's seed — the trial's parent might be an integrated prior
  trial (chained evolution).
