---
name: eden-manual-evaluator
description: 'Drive the EDEN evaluator role end-to-end from the terminal — clone at variant commit, help the user inspect, then submit evaluation via CLI. Trigger phrases: "play evaluator", "evaluate a variant", "evaluate a trial", "act as evaluator", "score a variant", "I want to evaluate".'
---

# EDEN Manual — Evaluator Role (CLI)

(Terminology note: "evaluator" is unchanged in the directed-evolution
vocab. What it evaluates was renamed: "trial" → "variant", and the
evaluator's submission carries an `evaluation` dict, not `metrics`. See
[`docs/glossary.md`](../../../docs/glossary.md).)

## When to use

User is running the EDEN reference Compose stack with no automated
evaluator-host and wants to play the evaluator role from the terminal.
Trigger phrases above.

## What "evaluator" means

The evaluator takes one `evaluation` task (which references a variant
whose executor-side work is committed and pushed), inspects the variant's
commit, and submits one of:

- `success` + a value for every entry in
  `experiment_config.evaluation_schema`.
- `error` (the variant code itself errored — submission status records
  that).
- `evaluation_error` (the evaluator couldn't form a verdict, e.g.
  couldn't fetch the variant commit, can't measure, etc.).

After the evaluator submits `success`, the orchestrator transitions the
variant from `starting` to `success` and the integrator integrates it
(creating the canonical `refs/heads/variant/<id>-<slug>`).

Everything below runs from the terminal. Don't ask the user to open the
web UI unless the CLI gap below bites.

## CLI rename gap (current limitation)

The `eden-manual` CLI script under `reference/scripts/manual-ui/` has
not yet been updated for the directed-evolution vocab rename. It still
uses the legacy `--kind plan/implement/evaluate`, `plan-submit`,
`implement-submit`, and `eval_error` spellings. The wire and web UI are
fully on canonical vocab. Until the CLI catches up:

- The commands in this skill below use the canonical vocab. If a
  subcommand or `--kind` value is rejected by `eden-manual` as
  "unknown", you've hit the gap.
- Workaround for the gap: drive the role flow through
  http://localhost:8090/evaluator/ in a browser. The web UI is on
  canonical vocab and works end-to-end.

## Workflow

```
EDEN=/Users/ericalt/Documents/eden-worktrees/test-main/reference/scripts/manual-ui/eden-manual
```

### Phase 1: Pick a task (automatic)

```bash
$EDEN list-tasks --kind evaluation --state pending
```

If empty, tell the user there's nothing to evaluate yet and suggest
finishing an execution cycle first.

### Phase 2: Inspect task + variant (automatic)

```bash
$EDEN show <task-id>
```

Present:
- The variant: `branch`, `commit_sha`, `parent_commits`, `description`.
- The evaluation_schema (names + types) from `experiment-config.yaml`.
- The objective so the user knows the direction to score in.

### Phase 3: Claim (automatic)

```bash
$EDEN claim <task-id> --worker-id eden-manual
```

### Phase 4: Clone at the variant commit (automatic)

```bash
$EDEN checkout <task-id>
```

Workdir is `/tmp/eden-manual/<task-id>`, checked out at
`variant.commit_sha` in detached HEAD. Surface the path.

Optionally show the diff vs. the parent commit so the user has the
material context:

```bash
git -C /tmp/eden-manual/<task-id> log --oneline <parent_sha>..HEAD
git -C /tmp/eden-manual/<task-id> diff --stat <parent_sha> HEAD
git -C /tmp/eden-manual/<task-id> diff <parent_sha> HEAD
```

Offer to open in the user's editor.

### Phase 5: Wait for the user's verdict (judgment)

Wait. Don't suggest scores unless asked. If asked, frame as: "evaluation
field `X` is type `<type>`; what value reflects how this variant does on
that dimension?" Don't fudge.

### Phase 6: Submit (automatic)

```bash
$EDEN evaluation-submit <task-id> --field score=0.42 [--field K=V ...]
```

For status=success, every field in the evaluation_schema must have a
value. For status=evaluation_error or error, omit `--field`:

```bash
$EDEN evaluation-submit <task-id> --status evaluation_error
```

### Phase 7: Verify (automatic)

```bash
$EDEN show <task-id> | python3 -c 'import json,sys; print(json.load(sys.stdin)["task"]["state"])'
```

Should be `completed`. Within ~1 second, the integrator integrates the
variant — confirm:

```bash
curl -fsS -H "Authorization: Bearer $(grep '^EDEN_SHARED_TOKEN=' \
    /Users/ericalt/Documents/eden-worktrees/test-main/reference/compose/.env | \
    cut -d= -f2)" \
  -H "X-Eden-Experiment-Id: $(grep '^EDEN_EXPERIMENT_ID=' \
    /Users/ericalt/Documents/eden-worktrees/test-main/reference/compose/.env | \
    cut -d= -f2)" \
  http://localhost:8080/v0/experiments/$(grep '^EDEN_EXPERIMENT_ID=' \
    /Users/ericalt/Documents/eden-worktrees/test-main/reference/compose/.env | \
    cut -d= -f2)/variants | python3 -m json.tool
```

Look for `variant_commit_sha` populated on the variant — that's proof
of integration.

## Best practices

- **Read the diff, not just the description.** The executor's
  `description` is unverified; `diff <parent_sha> HEAD` is ground truth.
- **`evaluation_error` is honest** when you can't form a verdict. Do
  not fabricate a score to submit `success`.
- **Use the variant's own `parent_commits`** as the diff base, not the
  experiment's seed — the variant's parent might be an integrated prior
  variant (chained evolution).
