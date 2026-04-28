# Worker host protocol — informative

> **Status: informative.** This chapter records a convention used by
> the **reference** worker hosts (planner / implementer / evaluator)
> for invoking user-supplied commands. Conforming EDEN
> implementations are **not** required to use this convention. The
> normative role contracts live in [chapter 3](../03-roles.md); the
> normative state machine and submission semantics live in
> [chapter 4](../04-task-protocol.md). Whether a worker host runs the
> user's logic in-process, as a subprocess, in a container, or via
> RPC is a deployment choice.

This chapter is included so that someone writing an alternative
host can observe what the reference impl does and either match it
(for drop-in compatibility with the fixture experiment) or pick
deliberately different conventions.

## 1. Common shape

Each role's host invokes a command string from the experiment-config
YAML:

- planner: `plan_command`
- implementer: `implement_command`
- evaluator: `evaluate_command`

All three keys are accepted as additional properties on the
[`experiment-config.schema.json`](../schemas/experiment-config.schema.json)
schema (the schema does not pin them; user-supplied tooling may
ignore or repurpose them).

The host invokes the command via `shell=True` so user expressions
like `python3 ${EDEN_EXPERIMENT_DIR}/plan.py` expand against the
host-supplied environment.

### 1.1 Environment supplied to every subprocess

| Variable | Meaning |
|---|---|
| `EDEN_EXPERIMENT_DIR` | Absolute host-side path to the user's experiment directory. |
| `EDEN_TASK_JSON` | Relative path (under cwd) to a JSON file the host wrote describing the current task. |
| `EDEN_OUTPUT` | Relative path (under cwd) the subprocess MUST write its outcome JSON to. |
| `EDEN_WORKTREE` | (implementer / evaluator only) Absolute path to the per-task git worktree (also equals cwd, redundant for convenience). |

User-supplied env from a `--*-env-file` flag is also injected
(intended for LLM API keys etc.).

### 1.2 cwd

| Role | cwd |
|---|---|
| planner | `EDEN_EXPERIMENT_DIR` |
| implementer | the per-task git worktree |
| evaluator | the per-task git worktree |

## 2. Planner subprocess: long-running JSON-line protocol

The planner subprocess is launched **once per host** and serves
every plan task that arrives during the host's lifetime. The
intent is that user code can hold accumulating session state (e.g.
a running LLM conversation) across plan tasks.

Wire format: one JSON object per line, on both stdin and stdout.

### 2.1 Startup handshake

The subprocess MUST emit, on stdout, exactly one line of the form:

```json
{"event": "ready"}
```

Until that line is observed, the host does not dispatch tasks.
Lines emitted before `ready` are treated as debug-only and dropped.

If `ready` is not received within the host's startup deadline, the
host kills the subprocess.

### 2.2 Plan dispatch

For each plan task, the host writes one stdin line of the form:

```json
{"event": "plan", "task_id": "plan-…", "experiment_id": "exp-1",
 "objective": {"expr": "score", "direction": "maximize"},
 "metrics_schema": {"score": "real"},
 "history": [
   {"trial_id": "trial-…", "status": "success",
    "commit_sha": "abc…", "metrics": {"score": 0.7}},
   …
 ]}
```

`history` is a flat list of recently completed evaluate-task
results, newest first, capped at 50 entries by the reference host.

### 2.3 Worker response

The subprocess writes any number of `proposal` lines followed by
exactly one terminator (`plan-done` or `plan-error`). All lines
MUST carry the same `task_id` as the dispatch.

```json
{"event": "proposal", "task_id": "plan-…",
 "slug": "p0", "priority": 1.0,
 "parent_commits": ["abc…"],
 "rationale": "free-form markdown text"}
{"event": "plan-done", "task_id": "plan-…"}
```

If `rationale` is present, the host writes it to
`<artifacts_dir>/proposals/<proposal_id>/rationale.md` and uses the
resulting `file://` URI as the proposal's `artifacts_uri`. If
`rationale` is absent, the subprocess MUST set `artifacts_uri`
explicitly.

A `plan-error` terminator submits a chapter-3 `PlanSubmission`
with `status="error"`.

### 2.4 Error handling

- A line that fails to parse as JSON, lacks an `event` field, or
  carries a `task_id` other than the current dispatch is treated
  as a protocol violation; the host submits the current task as
  `error` and re-spawns the subprocess.
- Subprocess EOF or crash while a task is in flight: the host
  submits `error` and respawns.
- Host shutdown (SIGTERM): the host sends SIGTERM to the
  subprocess's process group, waits the configured shutdown
  deadline, then SIGKILLs.

## 3. Implementer subprocess: per-task short-lived

The host honors [chapter 3 §3.2 step 1](../03-roles.md): the trial
MUST be persisted with `status == "starting"` **before** any
repository write becomes observable. The reference flow is:

1. Host generates `trial_id` and computes the canonical work-branch
   name `work/<slug>-<trial_id>`.
2. Pre-Phase-1 ref-collision guard.
3. `Store.create_trial(status="starting")` (no `commit_sha` yet).
4. `git worktree add --detach <wt> <parent_commits[0]>`.
5. Write `<wt>/.eden/task.json`:

   ```json
   {
     "task_id": "implement-…",
     "trial_id": "trial-…",
     "proposal_id": "proposal-…",
     "proposal_slug": "p0",
     "parent_commits": ["abc…", …],
     "branch": "work/p0-trial-…",
     "rationale_path": "/abs/path/to/rationale.md",
     "output_path": ".eden/outcome.json"
   }
   ```

6. Spawn `implement_command` with cwd = worktree.
7. Read `<wt>/.eden/outcome.json`:

   ```json
   {"status": "success", "commit_sha": "def…",
    "description": "free-form summary"}
   ```

   or `{"status": "error", "description": "…"}`.
8. Validate `commit_sha` exists and `is_ancestor(parent, commit_sha)`
   for every parent in `proposal.parent_commits` (chapter 3 §3.3).
9. `repo.create_ref("refs/heads/work/<…>", commit_sha)`.
10. `Store.submit(...)` with retry-before-orphan + committed-state
    read-back.
11. `git worktree remove --force <wt>`.

### 3.1 Failure modes

All four of (subprocess exit-nonzero, missing outcome.json,
malformed outcome, `outcome.status != "success"`) terminalize as
`ImplementSubmission(status="error", trial_id=…)`. The
`Store._reject_implement` path composite-commits the trial to
`error` atomically with the task transition. The user-supplied
`description` field on `outcome.json` is logged for diagnostics
but **not** propagated to the wire (the submission dataclass has
no free-form field; see §5).

## 4. Evaluator subprocess: per-task short-lived

1. `git worktree add --detach <wt> <trial.commit_sha>`.
2. Write `<wt>/.eden/eval-task.json`:

   ```json
   {
     "task_id": "evaluate-…",
     "trial_id": "trial-…",
     "trial_branch": "trial/…-p0",
     "trial_commit_sha": "ghi…",
     "metrics_schema": {"score": "real"},
     "objective": {"expr": "score", "direction": "maximize"},
     "output_path": ".eden/eval-outcome.json"
   }
   ```

3. Run `evaluate_command` with cwd = worktree.
4. Read outcome:

   ```json
   {"status": "success", "metrics": {"score": 0.83},
    "artifacts_uri": "file:///…"}
   ```

   or `{"status": "error" | "eval_error"}`.
5. Validate metrics against `metrics_schema` via
   `Store.validate_metrics`. Validation failures route to
   `eval_error`.
6. `Store.submit(...)`. Cleanup worktree.

## 5. Failure-context surface

The chapter-3 submission shapes carry no free-form `description`
field. Worker-side failure context (subprocess exit code,
malformed outcome, deadline-exceeded, etc.) is surfaced **only via
the host's structured logger**. Expanding the wire failure
vocabulary (a normative `Submission.diagnostic` field, or a
dedicated `task.diagnostic` event) is left to a future spec
revision.

### 5.1 Stderr task-id attribution is best-effort

The reference host stamps each forwarded stderr line with the
current task id. For the **per-task** implementer and evaluator
subprocesses this is exact (the subprocess is spawned per task and
exits before the next one begins). For the **long-running** planner
subprocess it is best-effort: stdout (the protocol channel) and
stderr (the diagnostic channel) are independent pipes, and a stderr
line whose underlying syscall completed under task A may be
delivered to the host after the host has already advanced the
shared task-id holder to task B. Users who need exact per-task
diagnostics should frame those messages on the protocol stdout
channel instead.

## 6. Cross-host worktree isolation

When multiple host containers share a single bare repo (the
reference Compose deployment), each host writes its worktrees
under a host-private subdir of the form
`<worktrees_root>/<container_hostname>/<task_id>/`. Startup-time
cleanup of leftover worktrees uses path-scoped
`git worktree remove --force <path>`, walking only the host's own
subdir; the repo-global `git worktree prune` is **not** used. This
makes cross-host races impossible by construction.
