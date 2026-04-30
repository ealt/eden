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

## 7. Gitea-as-remote reference deployment

The Phase 10d follow-up B reference deployment has every worker
host (orchestrator integrator, implementer host, evaluator host,
web-ui implementer module) treat **Gitea** as the central git
remote — workers stop touching a shared bare-repo volume and
instead clone a private bare copy from Gitea over HTTP, fetch on
subsequent starts, push their `work/*` and `trial/*` refs back,
and rely on Gitea's CAS for chapter 6 §3.4 atomicity.

### 7.1 Auth — HTTP Basic via per-experiment credential helper

The reference Gitea is exposed on plain HTTP inside the compose
network (no TLS). That matches the reference soft-isolation
posture (the trust boundary is the compose network, not the
wire). A hardened deployment substitutes a TLS-fronted Gitea
behind the same `--gitea-url` flag — the wrapping code is opaque
to the URL scheme.

setup-experiment provisions an admin `eden` user with a
per-experiment password, a repo `eden/<experiment-id>.git`
(idempotent), and a credential-helper script at
`reference/compose/.gitea-creds-<experiment-id>/credential-helper.sh`
that prints `username=eden\npassword=<generated>` for matching
URLs. The script is mounted RO into every worker container at
`/etc/eden/credential-helper.sh` and configured as the local
clone's `credential.helper`. The seed commit is pushed by a
one-shot `eden-repo-init --push-to <gitea-url>` invocation.

The credential-helper script lives on the host filesystem
(mode 0755). The eden user's Gitea password is therefore visible
to anyone with read access to the host — same soft boundary as
the DooD socket mount documented in §8.

### 7.2 Per-role git operations

| Role | Reads | Writes |
|---|---|---|
| Orchestrator/integrator | fetch_all_heads at startup; ls_remote at orphan-reconciliation | local create_ref → push_ref → integrate_trial → on store fail compensating delete_remote_ref |
| Implementer | local create_ref → push_ref of `work/*` after the user `*_command` produces a commit | rolls back local on push failure + submits status=error |
| Evaluator | fetch_ref of `trial.branch` before `git worktree add`; eval_error on transport failure | never pushes |
| Web-ui implementer | same as implementer | same as implementer |

### 7.3 Integrator atomicity ladder (chapter 6 §3.4)

`Integrator.integrate(trial_id)` runs a four-step
publish-then-commit-then-rollback ladder:

1. local `create_ref` — CAS-guarded local ref write.
2. remote `push_ref` — publish to Gitea with
   `--force-with-lease`. Three branches:
   - `RefRefused`: definite remote rejection. Step 4b only.
   - `GitTransportError`: ambiguous; disambiguate by an
     immediate `ls_remote` read-back. Outcomes: remote absent
     (4b only), remote = our SHA (4a + 4b), remote =
     different SHA (4b only), ls_remote also fails (4b only,
     `reconcile_remote_orphans` cleans up at next startup).
3. `store.integrate_trial` — atomic with the
   `trial.integrated` event in the store. Only this step
   commits the trial as integrated.
4. on store failure:
   - 4a. remote `delete_ref` (compensating) if step 2 succeeded.
   - 4b. local `delete_ref` (compensating).

`trial.integrated` is emitted ONLY at step 3 — NOT at step 1.
Local-only state is invisible to chapter 5 §2.2 event-log
consumers.

### 7.4 Startup remote-orphan reconciliation

`Integrator.reconcile_remote_orphans()` runs at orchestrator
startup. It walks `refs/heads/trial/*` on Gitea via `ls-remote`,
recovers the `trial_id` from each commit's
`.eden/trials/<trial_id>/eval.json` tree path
(spec-authoritative — chapter 6 §3.2), and for each calls
`Store.read_trial(trial_id)`. If the trial has no
`trial_commit_sha`, the integrator deletes the remote ref.

The `trial_id` recovery deliberately does NOT parse ref names —
chapter 2 §1.3 treats `trial_id` as opaque, and branch names
following `trial/<trial_id>-<slug>` are a reference-impl
convention conforming alternatives may not match.

This is the backstop for the §7.3 "step 4a failed" case and the
"push transport-fails AND ls-remote also transport-fails" case.

### 7.5 Crash recovery

Each worker host's startup:

1. If `/var/lib/eden/repo` is not yet a git repo, `clone --bare`
   from Gitea via `--gitea-url`.
2. Otherwise,
   `git fetch --prune origin '+refs/heads/*:refs/heads/*'` to
   refresh ALL local heads + delete any local heads no longer
   on the remote (orphan-cleanup as a fetch side effect).
3. Enter the role's normal poll loop.

Long-lived web-ui clones additionally `git fetch origin <ref>`
read-before-display + read-before-write so the rendered view
matches the remote.

## 8. Container-isolated reference deployment (DooD)

The Phase 10d follow-up A reference deployment offers an opt-in
`--exec-mode docker` flag on every host. With this active, each
spawn of the user's `*_command` runs in a sibling docker container
via Docker outside of Docker (DooD): the worker host container
mounts `/var/run/docker.sock` and shells out `docker run`, which
talks to the host docker daemon to start a child container.

The wrap shape is:

```bash
docker run --rm -i --init \
  --cidfile <unique-cidfile> \
  --label eden.host=<container-hostname> \
  --label eden.task_id=<task-or-host-id> \
  --label eden.role=<role> \
  --mount type=volume,source=<vol-name>,target=<path>[,readonly] \
  ... \
  --mount type=bind,source=<host-path>,target=<path>[,readonly] \
  ... \
  -w <cwd-target-inside-child> \
  -e <ENV_KEY> ... \
  <image> bash -lc '<original-command>'
```

The set of `--mount` entries is supplied by the deployment via
repeatable `--exec-volume` / `--exec-bind` flags (the reference
Compose overlay drives them). Mount targets inside the child match
the worker host's own paths exactly so worker-internal env vars
(`EDEN_TASK_JSON`, `EDEN_OUTPUT`, `EDEN_WORKTREE`,
`EDEN_EXPERIMENT_DIR`) resolve consistently in both places.

### 7.1 Mount strategy

DooD means `--mount source=` references are resolved against the
**host docker daemon's catalog**, not the worker host container's
filesystem. So named volumes (`eden-bare-repo`, `eden-worktrees`,
`eden-artifacts-data`) are forwarded by literal name; the
experiment dir is forwarded by host-side absolute path captured by
setup-experiment as `EDEN_EXPERIMENT_DIR_HOST`. The reference
Compose stack pins explicit `name:` on each forwarded volume so
compose's default `<project>_<volume>` prefix doesn't mismatch
the wrap's literal name reference.

### 7.2 Container lifecycle

- **Per-spawn cidfile.** Every spawn writes its container id to
  `<cidfile_dir>/<role>-<spawn-uuid>.cid`. Paths are unique per
  spawn (uuid-suffixed) so `docker run --cidfile` never trips on
  a stale path.
- **Cleanup on every exit branch.** A cleanup callback registered
  at spawn time unlinks the cidfile on every terminal path
  (graceful exit, SIGTERM-then-exit, fast-path, SIGKILL
  escalation).
- **SIGKILL → kill the sibling.** Killing the local `docker run`
  client does NOT kill the spawned container — the daemon parent
  keeps it running. A `post_kill_callback` registered at spawn
  time runs `docker kill <cid> && docker rm -f <cid>` on the
  SIGKILL escalation branch.
- **Startup-time orphan reaping.** Each host calls
  `reap_orphaned_containers(role, host=gethostname())` before its
  main loop, removing any `eden.host=<this-host>
  eden.role=<this-role>` containers left from a prior crash. The
  filter is host-scoped to mirror the cross-host worktree
  isolation in §6.

### 7.3 Identity

The default reference runtime image (`eden-runtime:dev`) runs as
the same `eden:1000` user that the worker host uses, so worktree
files (uid 1000) are readable/writable inside the child without a
`safe.directory` workaround, and any commits the child produces
are owned by the same user the integrator runs as. Experiment
images that need a different uid (e.g. for system-level installs)
override `USER` in their Dockerfile; the wrap deliberately does
NOT pass `--user` so this override is honored.

### 7.4 Socket permissions

The host docker socket is conventionally `root:docker` mode
`0660` on Linux. The reference compose overlay supplies the
worker container's supplementary gid via `group_add:
["${EDEN_DOCKER_GID}"]`. setup-experiment probes the gid by
running `stat -c '%g' /var/run/docker.sock` from inside a
throwaway container that bind-mounts the socket — this is the
gid the worker container will see, which on Docker Desktop /
Colima differs from a host-side stat.

### 7.5 Security boundary

DooD with a shared `/var/run/docker.sock` is **not a hard
isolation boundary**. A `*_command` running in the spawned child
has, via the same shared socket the worker host uses,
daemon-level access: it can `docker run` arbitrary containers,
read other containers' `docker inspect` output (so secrets
passed via `-e KEY` are visible to a malicious sibling), and
mount host paths. Hardened deployments substitute sysbox / Kata /
a per-worker docker daemon; the wrap shape is unchanged in those
deployments — only the daemon socket origin differs.

The reference deployment trades that boundary for operational
simplicity. The intent of `--exec-mode docker` in the reference
is **bug isolation** (a buggy `*_command` writing to disk doesn't
clobber the worker host's filesystem) and **dependency isolation**
(experiment-specific images), not hostile-code containment.

### 7.6 Image strategy

Two layers, in priority order:

1. **Experiment-specific image.** If the experiment dir contains
   a `Dockerfile`, setup-experiment builds it as
   `eden-experiment-<id>:dev` and writes that tag into `.env` as
   `EDEN_EXEC_IMAGE`. The image's `FROM eden-runtime:dev` line
   is recommended (so the eden:1000 user identity is inherited)
   but not required.
2. **Default runtime image.** Otherwise the worker hosts use
   `eden-runtime:dev` — a small image with python3 + git +
   bash + ca-certificates + the eden:1000 user.
