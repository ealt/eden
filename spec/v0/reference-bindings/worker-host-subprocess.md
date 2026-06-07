# Worker host protocol — informative

> **Status: informative.** This chapter records a convention used by
> the **reference** worker hosts (ideator / executor / evaluator)
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

- ideator: `ideation_command`
- executor: `execution_command`
- evaluator: `evaluation_command`

All three keys are accepted as additional properties on the
[`experiment-config.schema.json`](../schemas/experiment-config.schema.json)
schema (the schema does not pin them; user-supplied tooling may
ignore or repurpose them).

The host invokes the command via `shell=True` so user expressions
like `python3 ${EDEN_EXPERIMENT_DIR}/ideation.py` expand against the
host-supplied environment.

### 1.1 Environment supplied to every subprocess

| Variable | Meaning |
|---|---|
| `EDEN_EXPERIMENT_DIR` | Absolute host-side path to the user's experiment directory. |
| `EDEN_TASK_JSON` | Relative path (under cwd) to a JSON file the host wrote describing the current task. |
| `EDEN_OUTPUT` | Relative path (under cwd) the subprocess MUST write its outcome JSON to. |
| `EDEN_WORKTREE` | (executor / evaluator only) Absolute path to the per-task git worktree (also equals cwd, redundant for convenience). |
| `EDEN_WORKER_ID` | The host's registered `worker_id` ([chapter 2 §6](../02-data-model.md)). User code that issues wire calls of its own assembles its bearer as `f"{EDEN_WORKER_ID}:{EDEN_WORKER_CREDENTIAL}"` per [chapter 7 §13.1](../07-wire-protocol.md). Always set when the host has a registered identity; absent only when auth is disabled (in-process / test posture). |
| `EDEN_WORKER_CREDENTIAL` | The **secret half** of the host's §13.1 bearer (the part after `:`). Forwarded as a separate env var so user code can re-assemble the bearer with `EDEN_WORKER_ID` and so the variable's role is single-purpose. Set iff the host has a credential (§13 auth enabled); absent in test posture. Treat as sensitive: do not log; do not pass through to nested processes that don't need it. |
| `EDEN_REPO_DIR` | (all subprocess-mode roles, optional) Absolute host-side path to a bare git clone of the experiment's central repo (e.g. `/var/lib/eden/repo`). Set when the host is configured with `--repo-path` (subprocess mode). Lets user code `git log` / `git show` against the full ref space (`refs/heads/work/*`, `refs/heads/variant/*`) without making one-off wire calls. See §9 for the substrate-access posture. |
| `EDEN_ARTIFACT_URL` | (all subprocess-mode roles, optional) HTTP base URL ending in `/`, e.g. `http://task-store-server:8080/_reference/experiments/<experiment-id>/artifacts/`, with the deployment's `experiment_id` already interpolated. User code appends a relative path and GETs the bytes under the §13.1 bearer reconstructed from `EDEN_WORKER_ID` + `EDEN_WORKER_CREDENTIAL`. Reference-only route per §9. |
| `EDEN_ARTIFACT_PATH_ROOT` | (all subprocess-mode roles, optional) Absolute host-side filesystem root the `EDEN_ARTIFACT_URL` is rooted at (e.g. `/var/lib/eden/artifacts`). User code translates a `file:///var/lib/eden/artifacts/foo.md` URI from the wire into the relative path `foo.md` by stripping this prefix, then concatenates onto `EDEN_ARTIFACT_URL`. Pair with `EDEN_ARTIFACT_URL`; both or neither. |
| `EDEN_READONLY_STORE_URL` | (all subprocess-mode roles, optional) Postgres DSN with read-only privileges, e.g. `postgresql://eden_readonly:<pwd>@postgres:5432/eden`. User code connects via any Postgres client (e.g. psycopg) and runs `SELECT` against the eden schema. Excludes credential material — see §9 for the column-grant posture. |

User-supplied env from a `--*-env-file` flag is also injected
(intended for LLM API keys etc.).

### 1.2 cwd

| Role | cwd |
|---|---|
| ideator | `EDEN_EXPERIMENT_DIR` |
| executor | the per-task git worktree |
| evaluator | the per-task git worktree |

## 2. Ideator subprocess: long-running JSON-line protocol

The ideator subprocess is launched **once per host** and serves
every ideation task that arrives during the host's lifetime. The
intent is that user code can hold accumulating session state (e.g.
a running LLM conversation) across ideation tasks.

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

### 2.2 Ideation-task dispatch

For each ideation task, the host writes one stdin line of the form:

```json
{"event": "ideation", "task_id": "ideation-…", "experiment_id": "exp-1",
 "objective": {"expr": "score", "direction": "maximize"},
 "evaluation_schema": {"score": "real"},
 "history": [
   {"variant_id": "variant-…", "status": "success",
    "commit_sha": "abc…", "evaluation": {"score": 0.7}},
   …
 ]}
```

`history` is a flat list of recently completed evaluation-task
results, newest first, capped at 50 entries by the reference host.

### 2.3 Worker response

The subprocess writes any number of `idea` lines followed by
exactly one terminator (`ideation-done` or `ideation-error`). All lines
MUST carry the same `task_id` as the dispatch.

```json
{"event": "idea", "task_id": "ideation-…",
 "slug": "p0", "priority": 1.0,
 "parent_commits": ["abc…"],
 "content": "free-form markdown text"}
{"event": "ideation-done", "task_id": "ideation-…"}
```

If `content` is present, the host writes it to
`<artifacts_dir>/ideas/<idea_id>/content.md` and uses the
resulting `file://` URI as the idea's `artifacts_uri` (the
entity-hierarchical layout — see §10). If `content` is absent,
the subprocess MUST set `artifacts_uri` explicitly.

> *Wire-transfer migration (issue #166).* Issue #166 added the
> wire-level `deposit_artifact` / `fetch_artifact` endpoints
> ([chapter 7 §16](../07-wire-protocol.md)); under the deferred hard
> cutover ([#290](https://github.com/ealt/eden/issues/290)) the host
> will instead **deposit** the content bytes over the wire and stamp
> the returned opaque `eden://artifacts/<id>` URI. The `file://`
> layout described here is the current reference-host behavior until
> that cutover lands.

An `ideation-error` terminator submits a chapter-3 `IdeaSubmission`
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

## 3. Executor subprocess: per-task short-lived

The host honors [chapter 3 §3.2 step 1](../03-roles.md): the variant
MUST be persisted with `status == "starting"` **before** any
repository write becomes observable. The reference flow is:

1. Host generates `variant_id` and computes the canonical work-branch
   name `work/<variant_id>-<slug>` (field order mirrors the integrator's
   `variant/<variant_id>-<slug>` shape from chapter 06 §3.2 so operators
   reading Forgejo see consistent `<variant_id>-<slug>` ordering across
   both refs).
2. Pre-Phase-1 ref-collision guard.
3. `Store.create_variant(status="starting")` (no `commit_sha` yet).
4. `git worktree add --detach <wt> <parent_commits[0]>`.
5. Write `<wt>/.eden/task.json`:

   ```json
   {
     "task_id": "execute-…",
     "variant_id": "variant-…",
     "idea_id": "idea-…",
     "idea_slug": "p0",
     "parent_commits": ["abc…", …],
     "branch": "work/variant-…-p0",
     "content_path": "/abs/path/to/content.md",
     "output_path": ".eden/outcome.json"
   }
   ```

6. Spawn `execution_command` with cwd = worktree.
7. Read `<wt>/.eden/outcome.json`:

   ```json
   {"status": "success", "commit_sha": "def…",
    "description": "free-form summary"}
   ```

   or `{"status": "error", "description": "…"}`.
8. Validate `commit_sha` exists and `is_ancestor(parent, commit_sha)`
   for every parent in `idea.parent_commits` (chapter 3 §3.3).
9. `repo.create_ref("refs/heads/work/<…>", commit_sha)`.
10. `Store.submit(...)` with retry-before-orphan + committed-state
    read-back.
11. `git worktree remove --force <wt>`.

### 3.1 Failure modes

All four of (subprocess exit-nonzero, missing outcome.json,
malformed outcome, `outcome.status != "success"`) terminalize as
`VariantSubmission(status="error", variant_id=…)`. The
`Store._reject_execution` path composite-commits the variant to
`error` atomically with the task transition. The user-supplied
`description` field on `outcome.json` is logged for diagnostics
but **not** propagated to the wire (the submission dataclass has
no free-form field; see §5).

## 4. Evaluator subprocess: per-task short-lived

1. `git worktree add --detach <wt> <variant.commit_sha>`.
2. Write `<wt>/.eden/eval-task.json`:

   ```json
   {
     "task_id": "evaluate-…",
     "variant_id": "variant-…",
     "variant_branch": "variant/…-p0",
     "variant_commit_sha": "ghi…",
     "evaluation_schema": {"score": "real"},
     "objective": {"expr": "score", "direction": "maximize"},
     "output_path": ".eden/eval-outcome.json"
   }
   ```

3. Run `evaluation_command` with cwd = worktree.
4. Read outcome:

   ```json
   {"status": "success", "evaluation": {"score": 0.83},
    "artifacts_uri": "file:///…"}
   ```

   or `{"status": "error" | "evaluation_error"}`. (Under the deferred
   #166 cutover the host stages the subprocess's artifact bytes and
   deposits them over the wire, stamping an `eden://artifacts/<id>`
   URI — see §10.)
5. Validate evaluation against `evaluation_schema` via
   `Store.validate_evaluation`. Validation failures route to
   `evaluation_error`.
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
current task id. For the **per-task** executor and evaluator
subprocesses this is exact (the subprocess is spawned per task and
exits before the next one begins). For the **long-running** ideator
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

## 7. Forgejo-as-remote reference deployment

The Phase 10d follow-up B reference deployment has every worker
host (orchestrator integrator, executor host, evaluator host,
web-ui executor module) treat **Forgejo** as the central git
remote — workers stop touching a shared bare-repo volume and
instead clone a private bare copy from Forgejo over HTTP, fetch on
subsequent starts, push their `work/*` and `variant/*` refs back,
and rely on Forgejo's CAS for chapter 6 §3.4 atomicity.

### 7.1 Auth — HTTP Basic via per-experiment credential helper

The reference Forgejo is exposed on plain HTTP inside the compose
network (no TLS). That matches the reference soft-isolation
posture (the trust boundary is the compose network, not the
wire). A hardened deployment substitutes a TLS-fronted Forgejo
behind the same `--forgejo-url` flag — the wrapping code is opaque
to the URL scheme.

setup-experiment provisions an admin `eden` user with a
per-experiment password, a repo `eden/<experiment-id>.git`
(idempotent), and a credential-helper script at
`reference/compose/.forgejo-creds-<experiment-id>/credential-helper.sh`
that prints `username=eden\npassword=<generated>` for matching
URLs. The script is mounted RO into every worker container at
`/etc/eden/credential-helper.sh` and configured as the local
clone's `credential.helper`. The seed commit is pushed by a
one-shot `eden-repo-init --push-to <forgejo-url>` invocation.

The credential-helper script lives on the host filesystem
(mode 0755). The eden user's Forgejo password is therefore visible
to anyone with read access to the host — same soft boundary as
the DooD socket mount documented in §8.

### 7.2 Per-role git operations

| Role | Reads | Writes |
|---|---|---|
| Orchestrator/integrator | fetch_all_heads at startup; ls_remote at orphan-reconciliation | local create_ref → push_ref → integrate_variant → on store fail compensating delete_remote_ref |
| Executor | local create_ref → push_ref of `work/*` after the user `*_command` produces a commit | rolls back local on push failure + submits status=error |
| Evaluator | fetch_ref of `variant.branch` before `git worktree add`; evaluation_error on transport failure | never pushes |
| Web-ui executor | same as executor | same as executor |

### 7.3 Integrator atomicity ladder (chapter 6 §3.4)

`Integrator.integrate(variant_id)` runs a four-step
publish-then-commit-then-rollback ladder:

1. local `create_ref` — CAS-guarded local ref write.
2. remote `push_ref` — publish to Forgejo with
   `--force-with-lease`. Three branches:
   - `RefRefused`: definite remote rejection. Step 4b only.
   - `GitTransportError`: ambiguous; disambiguate by an
     immediate `ls_remote` read-back. Outcomes: remote absent
     (4b only), remote = our SHA (4a + 4b), remote =
     different SHA (4b only), ls_remote also fails (4b only,
     `reconcile_remote_orphans` cleans up at next startup).
3. `store.integrate_variant` — atomic with the
   `variant.integrated` event in the store. Only this step
   commits the variant as integrated.
4. on store failure:
   - 4a. remote `delete_ref` (compensating) if step 2 succeeded.
   - 4b. local `delete_ref` (compensating).

`variant.integrated` is emitted ONLY at step 3 — NOT at step 1.
Local-only state is invisible to chapter 5 §2.2 event-log
consumers.

### 7.4 Startup remote-orphan reconciliation

`Integrator.reconcile_remote_orphans()` runs at orchestrator
startup. It walks `refs/heads/variant/*` on Forgejo via `ls-remote`,
recovers the `variant_id` from each commit's
`.eden/variants/<variant_id>/evaluation.json` tree path
(spec-authoritative — chapter 6 §3.2), and for each calls
`Store.read_variant(variant_id)`. If the variant has no
`variant_commit_sha`, the integrator deletes the remote ref.

The `variant_id` recovery deliberately does NOT parse ref names —
chapter 2 §1.3 treats `variant_id` as opaque, and branch names
following `variant/<variant_id>-<slug>` are a reference-impl
convention conforming alternatives may not match.

This is the backstop for the §7.3 "step 4a failed" case and the
"push transport-fails AND ls-remote also transport-fails" case.

### 7.5 Crash recovery

Each worker host's startup:

1. If `/var/lib/eden/repo` is not yet a git repo, `clone --bare`
   from Forgejo via `--forgejo-url`.
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

## 9. Substrate read-access for agent role implementations

Phase 12a-1f opens three read-side substrates to the ideator,
executor, and evaluator subprocesses (executor coverage added
post-12a-1f by issue #154) so a user-supplied agentic role
implementation (typically driven by an LLM) can explore
experiment state without rounding through the wire one read
at a time:

| Substrate | Env var(s) | Read shape |
|---|---|---|
| Git (local bare clone) | `EDEN_REPO_DIR` | `git log` / `git show` / tree walk against the worker host's bare clone of the central repo (Phase 10d follow-up B Forgejo-as-remote). Sees `refs/heads/work/*` and `refs/heads/variant/*` plus the evaluation manifest at the variant tip. |
| Artifact server (HTTP) | `EDEN_ARTIFACT_URL` + `EDEN_ARTIFACT_PATH_ROOT` | `GET ${EDEN_ARTIFACT_URL}<relative-path>` with the §13.1 bearer (`${EDEN_WORKER_ID}:${EDEN_WORKER_CREDENTIAL}`) against the task-store-server's reference-only `/_reference/experiments/<id>/artifacts/<path>` route. Returns bytes ≤ 1 MiB; larger files 413. |
| Postgres event log | `EDEN_READONLY_STORE_URL` | Direct Postgres connection as the `eden_readonly` role. SELECT on `experiment`, `task`, `submission`, `idea`, `variant`, `event`, `worker_group`, `group_membership`, `schema_version`; column-projection SELECT on `worker(worker_id, data)` (the JSON `data` payload carries `labels`, `registered_at`, `registered_by`, etc.). `SELECT * FROM worker` fails because the `credential_hash` column is intentionally excluded. |

### 9.1 The three substrates are independent

Opening one without the others is a valid deployment. Each
env var is optional and only set when the host has been
configured with the corresponding flag. Cross-substrate
joins are the agent's responsibility; the protocol does not
mediate them.

### 9.2 On-host vs off-host

The compose-internal defaults
(`task-store-server:8080`, `postgres:5432`,
`/var/lib/eden/repo`) only resolve inside the worker-host
container. An agent running off-host (e.g. on a developer
laptop driving an LLM against a server-hosted compose stack)
substitutes its own values:

- `EDEN_REPO_DIR` is replaced by an operator-controlled
  clone of the Forgejo repo (today's shared `eden` HTTP-Basic
  password; Phase 13e moves to per-worker tokens with branch
  ACLs).
- `EDEN_ARTIFACT_URL` is substituted with the operator's
  reverse-proxy hostname for the task-store-server (the
  bearer is still the same per-worker `<worker_id>:<token>`
  from `EDEN_WORKER_*`).
- `EDEN_READONLY_STORE_URL` is substituted with the
  operator's externally-reachable Postgres hostname (the
  `eden_readonly` role and password are unchanged).

The binding's `EDEN_ARTIFACT_PATH_ROOT` MUST equal the
task-store-server's `--artifacts-dir` so the agent's URI
translation is correct. In compose, both default to
`/var/lib/eden/artifacts`. Off-host operators are
responsible for choosing consistent paths.

### 9.3 Trust-boundary caveats

- **The readonly Postgres role can see worker_ids
  (attribution).** The granted tables include `event`,
  `task`, `idea`, and `variant`, whose `data` JSON
  payloads carry `submitted_by` / `executed_by` /
  `evaluated_by` / `created_by` per chapter 02 §3.1 / §5.1
  / §9. An agent with the readonly DSN can enumerate all
  workers in the experiment by SELECTing `worker_id` from
  the worker table or by aggregating attribution fields
  across the artifact tables. This is by design —
  exploratory reads need attribution — but operators who
  don't want this surface should NOT enable
  `EDEN_READONLY_STORE_URL`.
- **The readonly role's password is separate from the §13
  per-worker bearer.** Rotating one does not invalidate
  the other. The 12a-1f reference deployment generates
  `EDEN_READONLY_PASSWORD` once in setup-experiment.sh
  and preserves it across re-runs; rotation requires
  deleting the line from `.env` and re-running.
- **The artifact server enforces a 1 MiB cap.** Files
  larger than 1 MiB return 413 with no partial body.
  Operators with legitimately-larger artifacts should
  wait for Phase 13d's `Backend` abstraction (the cap
  pairs with a fixed-bytes response model that's a TOCTOU
  countermeasure — `StreamingResponse` re-opens the path
  at body-write time and breaks the descriptor-walk
  guarantee).
- **`--exec-mode docker` (DooD) suppresses the substrate
  env vars unless `--exec-network` opts in.** By default,
  sibling containers started by the host docker daemon
  attach to the bridge network, so `task-store-server:8080`
  / `postgres:5432` do not resolve. The ideator, executor,
  and evaluator host CLIs detect `exec_mode == "docker"`
  with no `--exec-network` set and drop the four substrate
  keys from the spawned child's env (the host logs a WARN
  line at startup with a hint pointing at `--exec-network`).
  Passing `--exec-network <compose-network>` (issue #155)
  attaches the spawned sibling to a reachable network so
  the substrate URLs resolve; the host then forwards the
  substrate keys normally. The reference compose stack
  defaults to `eden-reference_default`; off-host operators
  override via `${EDEN_EXEC_NETWORK}` or substitute their
  own compose project network name.

### 9.4 The substrates are reference-impl details

Chapter 8 §5 (artifact store, deferred) defers the
normative shape of the artifact substrate; chapter 2 §1.5
defers the URI scheme. The 12a-1f substrate route is a
**reference-only** extension under chapter 7 §11's
`/_reference/` namespace, NOT an implementation of the
deferred §5 contract. Conforming alternative
implementations may serve artifacts through a different
mechanism (cloud-storage SDKs, an HTTP file server, etc.)
or omit the route entirely; the env vars above are part of
this informative binding only.

The Postgres readonly substrate is similarly informative —
chapter 8 §3 specifies the durability and event-log
contracts but does not mandate Postgres. Alternative
implementations may expose a different read substrate
(e.g. a SQLite database file, a gRPC stream) or none at
all. The env var contract is what user code targets; the
backing technology is operator-chosen.

## 10. Reference artifact layout (issue #168)

The reference deployment groups artifacts under `<artifacts_dir>`
(surfaced as `/var/lib/eden/artifacts/` inside containers) by the
durable entity that owns them and the role that produced them:

```text
artifacts/
  ideas/<idea_id>/                    # ideator-produced
    content.md                        #   text-only idea
    <sanitized-upload>                #   single uploaded file
    bundle.tar.gz                     #   text + uploads / multi-file
  variants/<variant_id>/
    executor/                         # executor-produced
      exec-<uuid>.{md,<ext>,tar.gz}
    evaluator/                        # evaluator-produced
      eval-<uuid>.{md,<ext>,tar.gz}
```

The top-level directories use the **artifact noun** (`ideas` /
`variants`); the variant sub-directories use the **producing-role
noun** (`executor` / `evaluator`) because a variant aggregates
artifacts from two sources. The ideator's `ideas/<idea_id>/` directory
is write-once, so its leaf files take clean fixed names (`content.md`,
the upload's own name, or `bundle.tar.gz`). The `executor/` and
`evaluator/` directories are keyed only by the stable `variant_id` and
**accumulate** across (re)submissions, so each submission mints a fresh
`exec-<uuid>` / `eval-<uuid>` stem — no two submissions for one variant
ever target the same path (chapter 8 §5.4 no-overwrite).

Within a `.tar.gz` bundle, the text headline entry is role-coherent
(`content.md` for ideas, `evaluation.md` for evaluations, `variant.md`
for executor artifacts) regardless of the tarball's own stem-derived
filename.

In subprocess mode the **only** bytes the host itself writes are the
ideator's `ideas/<idea_id>/content.md` (§2.3); the per-task executor and
evaluator subprocesses supply their own `artifacts_uri` (§3, §4) and
choose where to write. The `variants/<variant_id>/{executor,evaluator}/`
convention is what the reference web-UI upload writers target and what a
future subprocess byte-writer SHOULD follow.

This layout is a **reference-binding detail**, not a normative protocol
requirement: chapter 8 §5.1 keeps the artifact-store naming scheme
"implementation-defined" and chapter 2 §1.5 keeps `artifacts_uri` an
opaque deployment-local URI. Conforming alternative implementations may
lay out artifacts however they like.

> *Wire-transfer migration (issue #166).* Issue #166 added the
> wire-level `deposit_artifact` / `fetch_artifact` endpoints
> ([chapter 7 §16](../07-wire-protocol.md)) + a server-private blob
> backend keyed by an opaque `eden://artifacts/<opaque-id>` URI. Under
> the deferred hard cutover ([#290](https://github.com/ealt/eden/issues/290))
> the reference hosts + web-UI **build the artifact blob in memory and
> deposit it over the wire** instead of writing this `file://` layout,
> the bundle viewer reads entries from a fetched blob in memory, and the
> physical layout becomes server-internal. The `file://` layout above is
> the current reference-host behavior until that cutover lands.
