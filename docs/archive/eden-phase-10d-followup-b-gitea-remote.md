# Phase 10d follow-up B — Gitea as the workers' git remote

## Context

Chunks 10b/10c shipped a Compose stack where Gitea runs but is
idle. Workers (orchestrator's integrator + the implementer host
across both subprocess-mode wrappers) all read and write through
the **shared `eden-bare-repo` named volume**. That works for a
single-host reference deployment but doesn't reflect the
multi-host topology spec chapter 06 contemplates: each worker
should own a private working copy and exchange refs with a
**central git remote** that's the sole source of truth.

This sub-chunk closes that delta. Workers stop touching the shared
bare-repo volume; they `git fetch` / `git push` against Gitea
(plain HTTP — see §D.3 for the soft-boundary rationale) instead.
The orchestrator's integrator does the same for `trial/*`
promotion. setup-experiment provisions Gitea (admin, HTTP Basic
credentials, repo creation, seed push).

Direction confirmed with the user before drafting:

- **A. Full cutover** — no shared bare-repo fallback. Cleaner
  blast radius; the volume gets removed.
- **B. HTTP Basic auth** via per-experiment credential helper —
  matches the existing reference soft-isolation posture (Gitea
  runs in the compose internal network with no TLS, same
  trust model as the existing `EDEN_SHARED_TOKEN` HTTP between
  workers and the task-store-server).
- **C. Extend `eden_git.GitRepo` with a remote-aware variant** —
  `GitRepo` itself stays additive; existing `create_ref` and
  related local ops keep working for in-process tests, plus
  new `clone_from`, `push_ref`, `fetch_ref`, and
  `delete_remote_ref` operations on top.

## Design

### D.1 Topology after cutover

Before:

```text
            ┌──────────────────────┐
            │  eden-bare-repo vol  │ ←── orchestrator/integrator
            │  (shared, writable)  │ ←── implementer-host
            └──────────────────────┘ ←── evaluator-host (read-only)
```

After:

```text
                    ┌─────────────┐
                    │   Gitea     │
                    │   HTTP/3000 │   <─── source of truth
                    └─────────────┘
                  fetch │ ▲ push
            ┌───────────┘ │ └───────────┐
       ┌────▼────┐  ┌─────▼────┐  ┌─────▼────┐
       │ implem. │  │ integ.   │  │ eval.    │
       │ (clone) │  │ (clone)  │  │ (clone)  │
       └─────────┘  └──────────┘  └──────────┘
```

Each role-host container holds a **private clone** under
`/var/lib/eden/repo` (still bind-mounted for persistence, but the
volume is per-service, not shared). The clone is initialized at
host startup from Gitea: clean state on first boot, recovered
on restart.

### D.2 `eden_git.GitRepo` extensions

Three new public methods (all dispatch through the existing
`subprocess.run(["git", ...])` plumbing — no shellouts to a new
binary, no new dependency):

- `clone_from(*, url: str, dest: Path, credential_helper: str | None) -> GitRepo`
  — `git clone --bare` (for the integrator) or `git clone`
  (for implementer/evaluator). When `credential_helper` is set,
  threads it through `git -c credential.helper=<…>` so HTTP
  Basic creds don't have to land in `~/.git-credentials`.
- `push_ref(ref: str, expected_old_sha: str | None) -> None` —
  `git push origin <ref>` with `--force-with-lease=<ref>:<sha>`
  when `expected_old_sha` is supplied. Maps daemon-side rejections
  to the same `RefRefused` exception `create_ref` raises today,
  so call sites stay structurally identical.
- `fetch_ref(ref: str) -> str | None` — `git fetch origin
  <ref>:<ref>` with `--prune=false`; returns the fetched SHA.
  Used by the integrator to re-read `work/*` and `trial/*` it
  doesn't have locally.

`create_ref` / `update_ref` / `delete_ref` stay on the local repo;
the integrator and implementer call `push_ref` immediately after
the local ref-write to publish. This keeps the existing local
discipline (CAS via zero-oid on create, expected-old-sha on update)
intact, and adds publication as a strictly-additive step.

### D.3 Auth flow — HTTP Basic via per-experiment credential helper

The reference Gitea is exposed on plain HTTP inside the compose
network (no TLS). That matches the existing soft-isolation
posture documented in §7 of
[`spec/v0/reference-bindings/worker-host-subprocess.md`](../../spec/v0/reference-bindings/worker-host-subprocess.md):
the reference deployment trades isolation for operational
simplicity. A hardened deployment terminates TLS at a sidecar or
Gitea-side reverse proxy, swapping the `http://gitea:3000/...`
URL for `https://gitea/...`; the workers' `--gitea-url` flag is
opaque to the rest of the design. We document this caveat
alongside the existing DooD soft boundary; the secret-handling
boundary widens by exactly one channel (now the eden user's
Gitea password is also visible to anyone with daemon access).

setup-experiment.sh, after starting Gitea + waiting for healthy:

1. Generate a per-experiment password `GITEA_REMOTE_PASSWORD`
   (32 hex bytes; same shape as other reference secrets).
2. Provision the eden user (idempotent — first checks via
   `gitea admin user list`):

   ```bash
   docker compose exec -T gitea gitea admin user create \
       --username eden \
       --password "${GITEA_REMOTE_PASSWORD}" \
       --email eden@invalid \
       --admin
   ```

   Re-running with a different password updates the existing
   user (`gitea admin user change-password`) so re-runs of
   setup-experiment with new env files don't desync.
3. Generate a per-experiment access token (used only by
   setup-experiment for repo creation, NOT by workers; workers
   use Basic auth):

   ```bash
   curl -fsS -u "eden:${GITEA_REMOTE_PASSWORD}" \
       -X POST http://localhost:${GITEA_HOST_PORT}/api/v1/users/eden/tokens \
       -d '{"name":"setup","scopes":["write:repository"]}'
   ```

4. Create the repo `eden/<EXPERIMENT_ID>.git` via
   `POST /api/v1/user/repos` (idempotent — handle 409).
5. Write the **credential-helper script** to a host-side path
   `${COMPOSE_DIR}/.gitea-creds-${EXPERIMENT_ID}/credential-helper.sh`,
   substituting the password into a template. The script:

   ```sh
   #!/bin/sh
   case "$1" in
     get)
       cat <<EOF
   username=eden
   password=__GITEA_REMOTE_PASSWORD__
   EOF
       ;;
   esac
   ```

   Mode 0755. Mounted read-only into every worker host container
   at `/etc/eden/credential-helper.sh`.

Workers configure git to use the helper via repo-local config
written at clone time (NOT global config — keeps the secret out
of the worker's home dir):

```bash
git -c credential.helper="/etc/eden/credential-helper.sh" \
    -c http.extraHeader="" \
    clone http://gitea:3000/eden/<EXPERIMENT_ID>.git \
    /var/lib/eden/repo
git -C /var/lib/eden/repo config credential.helper \
    "/etc/eden/credential-helper.sh"
```

Subsequent `git fetch` / `git push` invocations from that repo
pick up the helper automatically. The chunk-10d-followup-A
soft-boundary caveat (§7) is extended to cover this: the eden
user's Gitea password is on the host filesystem at
`${COMPOSE_DIR}/.gitea-creds-<id>/credential-helper.sh` mode 0755,
readable by anyone on the host and (under DooD) by any
concurrent `*_command`.

### D.4 setup-experiment.sh sequencing — option (a) selected

`setup-experiment.sh` already runs the bare-repo seed step
synchronously via `compose run --rm --no-deps eden-repo-init`.
This sub-chunk extends that pattern: setup-experiment now also
**brings Gitea up** synchronously (before the rest of the stack)
and provisions it before writing the operator's next-step
instructions. No deferred work in the operator's own
`compose up`.

Concrete sequence (replaces the current "seed bare repo, write
.env, exit" flow):

1. Generate / preserve all secrets (existing flow), including
   the new `GITEA_REMOTE_PASSWORD`.
2. Write the partial `.env` (without `EDEN_BASE_COMMIT_SHA` —
   filled in step 7).
3. **Bring Gitea up:**
   `docker compose --env-file "$ENV_FILE" up -d --wait gitea`
   (uses Gitea's existing healthcheck).
4. **Provision Gitea** per §D.3 (eden user, access token, repo).
5. **Write the credential-helper script** under
   `${COMPOSE_DIR}/.gitea-creds-${EXPERIMENT_ID}/`.
6. **Seed + push** via `compose run --rm --no-deps eden-repo-init`,
   which now takes a `--push-to <url>` flag and pushes the seed
   commit to Gitea after creating the local bare repo.
7. Capture the seed SHA, replace the `.env` placeholder
   (existing pattern).
8. Print operator next-steps (existing — `compose up -d --wait`).

The shared `eden-bare-repo` volume is removed from `compose.yaml`
in the cutover; `eden-repo-init` keeps writing to it locally only
so the seed-and-push step is self-contained, then the volume is
discarded by the existing `compose down -v` posture.

**Idempotency:** all four provisioning operations (user create,
token mint, repo create, ref push) are idempotent. Re-running
setup-experiment with the same env file is a no-op for Gitea
state. Re-running with a NEW env file (different
`GITEA_REMOTE_PASSWORD`) updates the user's password via
`gitea admin user change-password`.

### D.5 Per-service compose changes

Each worker host gains:

- A *private* named volume for its clone:
  `eden-implementer-repo`, `eden-evaluator-repo`,
  `eden-orchestrator-repo` (no shared `eden-bare-repo` anymore).
- The credential-helper script bind-mounted RO at
  `/etc/eden/credential-helper.sh`.
- A new `--gitea-url <http-url>` CLI flag on each host.

Each worker host's startup logic:

1. If `/var/lib/eden/repo` is not yet a git repo, `clone_from`
   from Gitea via `clone --bare` (per §D.8a — all worker hosts
   are bare clones).
2. Otherwise, `git fetch --prune origin '+refs/heads/*:refs/heads/*'`
   to refresh local heads and prune any local heads that no
   longer exist on the remote (one-shot orphan cleanup as a
   fetch side effect).
3. **Integrator-only: reconcile orphan REMOTE refs.** The
   orchestrator's startup additionally walks the remote
   `refs/heads/trial/*` via `git ls-remote origin
   'refs/heads/trial/*'`. For each ref the spec gives us a
   **machine-parseable artifact** at the commit tip — chapter 6
   §3.2 mandates the squash commit's tree contains
   `.eden/trials/<trial_id>/eval.json`. The reconciliation
   reads `<commit_sha>:.eden/trials/` via `git ls-tree --name-only
   <commit_sha> .eden/trials/` to recover the trial_id (a
   single subdir name; reject if zero or more than one — that
   commit isn't a valid integrator squash). Then the
   integrator calls `Store.read_trial(trial_id)`:
   - If the trial doesn't exist OR has no `trial_commit_sha`
     (i.e., `trial.integrated` was never committed), issue
     `delete_remote_ref` against Gitea to clean up.
   - Otherwise the trial is properly integrated; leave the ref
     alone.

   This is the recovery rule for the §D.6 step-4a failure mode
   ("remote publish succeeded, store write failed, remote
   compensating delete failed") AND for transport-indeterminate
   step-2 failures whose post-failure read-back also failed (the
   only case §D.7d's branch-decision-tree leaves to the sweep).
   The trial_id-from-tree path is spec-authoritative; ref-name
   parsing is not, since the spec treats trial_id as opaque.
   Branch names happen to follow `trial/<trial_id>-<slug>` as a
   reference-impl convention, but the integrator's reconciliation
   does NOT depend on that convention.
4. Enter the normal poll loop.

**Per-operation freshness for long-lived clones.** The orchestrator
loop and the web-ui's admin/implementer modules are long-lived;
remote refs change under their feet between the startup fetch and
the next operation. Two policies:

- **Read-before-write:** any service path that's about to write a
  ref via `repo.create_ref` first runs `git fetch origin <ref>`
  to refresh its local view. This keeps `--force-with-lease`'s
  expected-old-sha aligned with the remote.
- **Read-before-display:** the web-ui admin work-refs GC page
  (chunk-9e) runs `git fetch origin '+refs/heads/work/*:refs/heads/work/*'`
  on every GET so the operator's view matches the remote. The
  per-op fetch budget here is one HTTP roundtrip + a small ref
  list, well under 100ms in the reference deployment.

Failure mode: if Gitea is unreachable at startup, the host exits
non-zero and compose's `restart: on-failure` (existing posture)
retries until healthy. Mid-operation Gitea unreachability raises
`GitTransportError` from `eden_git`; service-level handlers catch
it role-specifically per [chapter 3](../../spec/v0/03-roles.md):

- **Implementer** (chapter 3 §3.3): infrastructure faults during
  worker-side execution map to `ImplementSubmission(status="error",
  trial_id=..., commit_sha=None)`. The trial composite-commits
  to `error` atomically with the task transition. Same posture
  as today's chunk-10d "subprocess timeout" path.
- **Evaluator** (chapter 3 §4.4): infrastructure faults map to
  `EvaluateSubmission(status="eval_error", trial_id=..., metrics=None)`.
  The trial stays at its prior status (`success` from the
  implementer) so the trial can be re-evaluated; this is the
  spec-mandated distinction between `error` (worker-determined
  bad outcome) and `eval_error` (infrastructure failure).
- **Integrator** (chapter 6 §3.4): two distinct cases.
  - A **definite remote rejection** (`RefRefused`) on `push_ref`
    means the remote did not accept the ref — only step 1's
    local create exists, and step 4b's local rollback alone is
    sufficient.
  - A **transport-indeterminate failure** (`GitTransportError`)
    on `push_ref` means we don't know if the remote accepted —
    the ack was lost. We MUST disambiguate by immediately
    running `git ls-remote origin <ref>`:
    - If the remote ref is absent → rollback step 4b (local) only.
    - If the remote ref equals our pushed SHA → the push DID
      land, so run BOTH 4a (remote delete) and 4b (local).
    - If the remote ref exists at a different SHA → another
      integrator integrated a different commit on the same
      trial id; this is `RefRefused` semantics, run 4b only.
    - If `ls-remote` itself transport-fails (Gitea still down)
      → log critical-severity and rely on §D.7c's startup sweep
      to clean up. This is the only case where an orphan can
      persist across the integrator restart, and the sweep is
      its backstop.
  - A transport failure during `delete_remote_ref` (step 4a)
    falls through to §D.7c likewise.

### D.6 Integrator (orchestrator) changes — explicit four-step flow

The current integrator at
[`reference/packages/eden-git/src/eden_git/integrator.py`](../../reference/packages/eden-git/src/eden_git/integrator.py)
does (chapter 6 §3.2 / §3.4):

```text
1. local create_ref     # CAS-guarded
2. store.integrate_trial
3. on store failure: local delete_ref (compensating)
```

After this sub-chunk, the flow becomes a literal four-step
publish-then-commit-then-rollback ladder:

```text
1. local create_ref           # CAS-guarded local
2. remote push_ref            # publish to Gitea (force-with-lease)
3. store.integrate_trial      # writes trial_commit_sha + trial.integrated
4. on store failure:
   4a. remote delete_ref      # compensating remote delete
   4b. local delete_ref       # compensating local delete
```

The new step 4a — **remote compensating delete** — is load-bearing
under chapter 6 §3.4: leaving `trial/*` on Gitea without the
matching `trial.integrated` event in the store is a protocol
violation, because the next replay of the event log would see a
ref that no event explains. We MUST run step 4a before step 4b
so a re-tried integration sees a clean Gitea slate.

`GitRepo` therefore needs a fourth new method:

- `delete_remote_ref(ref: str, expected_sha: str | None) -> None`
  — `git push origin --delete <ref>` with a CAS guard via
  `--force-with-lease=<ref>:<sha>`. If the remote ref has
  diverged (someone else integrated a different commit on the
  same trial id), raise `RefRefused` and let the caller decide.

If step 2 fails (push rejected — concurrent integrator won
the race), only step 1's local ref exists; we run step 4b
(local delete) and skip 4a. If step 3 fails after step 2
succeeded, we run BOTH 4a and 4b. If step 4a itself fails (Gitea
unreachable), we log a critical-severity diagnostic and leave
the local ref in place — the next integrator startup's
fetch-and-reconcile pass (§D.7c below) cleans up.

**Important:** `trial.integrated` is emitted by step 3 (atomically
with the trial's `trial_commit_sha` write). Step 1 alone does NOT
emit anything — only the *committed-and-published* ref counts.
This preserves chapter 5 §2.2's "events emit only on committed
state changes."

### D.7 Implementer changes

The implementer's existing flow per [chapter 03 §3.2](../../spec/v0/03-roles.md):

```text
1. Store.create_trial(status="starting")
2. git worktree add <path> <parent>
3. <user command writes commits>
4. repo.create_ref("refs/heads/work/<…>", commit_sha)
5. Store.submit(...)
```

becomes:

```text
1. Store.create_trial(status="starting")
2. git worktree add <path> <parent>      # local
3. <user command writes commits>
4. repo.create_ref("refs/heads/work/<…>")  # local
5. repo.push_ref("refs/heads/work/<…>")    # publish to Gitea
6. Store.submit(...)
```

The submit's `commit_sha` is the local SHA that we just pushed.
If push fails, we roll back the local ref (so the trial doesn't
linger as a "starting" trial that references a non-published
commit) and submit `status="error"`.

### D.8 Evaluator changes

The evaluator runs **before** integration, so the commit it
must check out lives on the implementer's `work/*` branch
(stored as `trial.branch` and `trial.commit_sha` per chapter
3 §3.2 step 3) — NOT on `trial/*`, which is the post-integration
namespace.

After cutover, the evaluator's pre-worktree-add step is:

```python
repo.fetch_ref(trial.branch)        # work/<slug>-<trial_id>
# fetch_ref returns the remote SHA; assert it equals
# trial.commit_sha or surface eval_error
repo.task_worktree_add(commit=trial.commit_sha)
```

The chapter-3 contract is unchanged; only the local fetch is
added so the worker's clone sees the implementer's freshly-
pushed commit. Evaluator never pushes.

### D.8a Bare-clone topology for ALL worker hosts

The current implementer/evaluator hosts use `git worktree add`
against a bare repo (`TaskWorktree` in
[`reference/services/_common/src/eden_service_common/worktrees.py`](../../reference/services/_common/src/eden_service_common/worktrees.py)
takes a bare-repo path). Bare-clone topology is preserved
post-cutover for ALL worker hosts:

- orchestrator / integrator: bare clone
- implementer host: bare clone
- evaluator host: bare clone
- web-ui (chunk-9c implementer module + chunk-9e admin
  work-refs GC): bare clone

`git worktree add` works fine against a bare repo; that's how
chunk-10d already runs. The `clone_from` operation passes
`--bare` unconditionally for worker hosts.

The startup-refresh fetch becomes:

```bash
git fetch --prune origin '+refs/heads/*:refs/heads/*'
```

`--prune` removes local heads that no longer exist on remote
(orphan-cleanup as a side effect of fetch). This works on bare
repos because there's no checked-out branch to refuse-update.

### D.9 What does NOT change

- Spec chapters 03 / 04 / 06 — the role contracts and integrator
  invariants are silent on whether the workers share storage or
  use a remote. Both fit the existing prose.
- The `Store` Protocol — entirely separate from git.
- The chunk-10d JSON-line `*_command` protocol — unchanged.
- `eden-runtime:dev` and the experiment image — git is already
  installed.

## Implementation surface

Files added:

- `reference/scripts/setup-experiment/provision-gitea.sh` —
  idempotent Gitea admin + repo + token provisioning. Called
  by `setup-experiment.sh` when Gitea is healthy.
- `reference/scripts/setup-experiment/credential-helper.sh.tmpl`
  — template; setup-experiment substitutes the password and
  writes it under `${COMPOSE_DIR}/.gitea-creds-${EXPERIMENT_ID}/`.
- `reference/packages/eden-git/tests/test_remote_ops.py` —
  unit-level tests for `clone_from` / `push_ref` / `fetch_ref`
  against a local file URL (no Gitea daemon needed for unit).
- `reference/compose/healthcheck/smoke-gitea-remote.sh` (or
  extend an existing smoke — TBD at impl time, see §F).

Files modified:

- `reference/packages/eden-git/src/eden_git/repo.py` — three
  new methods + a small `RemoteConfig` helper.
- `reference/packages/eden-git/src/eden_git/integrator.py` —
  fold `push_ref` into the integrator flow per §D.6.
- `reference/services/_common/src/eden_service_common/repo_init.py`
  — `--push-to <url>` flag for the seed step.
- `reference/services/{implementer,evaluator}/src/eden_*_host/`
  — startup-time clone/fetch + per-task fetch/push as per §D.7,
  §D.8.
- `reference/services/orchestrator/src/eden_orchestrator/cli.py`
  — same startup-time clone/fetch.
- `reference/services/web-ui/src/eden_web_ui/cli.py` — same
  (the chunk-9c web-ui implementer module also writes refs).
- `reference/scripts/setup-experiment/setup-experiment.sh` —
  call provision-gitea after healthcheck; wire env vars; mount
  credential helper.
- `reference/compose/compose.yaml` — add per-service repo
  volumes; remove `eden-bare-repo` from the shared services
  list; mount credential-helper into each worker.
- `reference/compose/compose.subprocess.yaml` and
  `compose.docker-exec.yaml` — propagate the credential-helper
  mount to spawned children **only** in the implementer/evaluator
  case where the user `*_command` may run git commands. (For
  the planner, no git ops happen in the user command.)
- Test fixtures: see §F-T (Cutover blast-radius inventory)
  below for the concrete file count.

## Tests

Three tiers, each driving the **public entry point** the
production code calls (per the AGENTS.md "test the actual code
path" pitfall) — no helper-only assertions.

### Unit (`reference/packages/eden-git/tests/test_remote_ops.py`)

Run against a local file-URL git remote (`file:///path/to/bare`)
— no Gitea daemon, no http. Sufficient because git's transport
abstraction makes file:// share the same ref-update code path as
http:// for our four new operations.

- `GitRepo.clone_from(url=file://…, dest=…)` produces a working
  clone at `dest`.
- `GitRepo.push_ref(ref, expected_old_sha)` succeeds when the
  remote ref matches `expected_old_sha`; raises `RefRefused`
  when the remote has diverged.
- `GitRepo.fetch_ref(ref)` updates the local ref to match
  remote; returns the fetched SHA.
- `GitRepo.delete_remote_ref(ref, expected_sha)` succeeds when
  the remote matches; raises `RefRefused` on divergence; raises
  `RefRefused` (not silent success) when the remote ref is
  already gone (idempotency caveat: callers handle the
  already-gone case explicitly because chapter-6 §3.4
  rollback semantics distinguish "no-op" from "won the race").
- Credential-helper roundtrip: drive a real `git http-backend`
  via Python's `http.server` + the helper script generated by
  setup-experiment's template. Assert that without the helper
  the push fails 401, with the helper the push succeeds. This
  proves the helper is wired correctly end-to-end at unit
  speed; argv-only inspection would NOT.

### Integration (`reference/packages/eden-git/tests/test_remote_integrator.py`)

Drive the **public `Integrator.integrate(trial_id)` method**
through each failure mode, using a real local-file remote +
a monkey-patched Store:

- Happy path: `integrate` succeeds → local ref + remote ref +
  `trial.integrated` event all present.
- Push race (step 2 fails): set the remote `trial/<trial_id>-<slug>` ref to
  a different SHA before `integrate` runs. Assert: local ref
  rolled back, remote untouched, no `trial.integrated` event.
- Store failure after push (step 3 fails): monkeypatch
  `Store.integrate_trial` to raise `DispatchError`. Assert:
  remote ref deleted (compensating push), local ref deleted,
  no `trial.integrated` event.
- Compensating delete itself fails (step 4a fails): monkeypatch
  `delete_remote_ref` to raise `GitTransportError`. Assert:
  the integrator logs a critical-severity diagnostic AND the
  next-startup reconciliation pass (§D.7c) cleans up the orphan
  on Gitea. (Tested via a second `Integrator()` instance after
  the first's failure.)

### Smoke (`compose-smoke-*` extensions)

Extends both `compose-smoke` and `compose-smoke-subprocess(-docker)`
with one new post-quiescence assertion:

```bash
git ls-remote http://eden:${GITEA_REMOTE_PASSWORD}@gitea:3000/eden/${EXPERIMENT_ID}.git \
    | grep -E 'refs/heads/trial/' | wc -l
```

≥3 (matches the existing `trial.integrated` event count
assertion). Proves the remote actually has the integrated trial
refs, not just the local clones.

A new `compose-e2e` extension covers the chunk-9e admin
work-refs GC page driving a `delete_remote_ref` against Gitea
(asserts the remote ref is gone after the operator-clicks-delete
flow).

### `eden_git` testing scope: file-URL is enough

The new unit + integration tests use `file://` URLs for the
remote. We do NOT add a `pytest.mark.gitea` marker. Justification:

- `git push --force-with-lease` and `git push --delete` use the
  same wire protocol over file:// as over http://. The CAS
  semantics are server-side (the remote's reflog), not transport-
  dependent.
- HTTP transport is exercised end-to-end by the smoke scripts
  against the real Gitea container — that's where any
  http-specific issue (auth, redirect, content-length, etc.)
  surfaces.
- A separate marker would split coverage and require yet another
  CI service container (Gitea), with no proof that http exposes
  bugs the file:// path doesn't already catch.

The credential-helper unit test is the one exception: it spins
up a tiny http server in-process so the auth round-trip is real,
no external dependency needed.

## F-T. Cutover blast-radius inventory

Concrete file count from `grep -l "init_bare\|GitRepo.*bare"`
across the repo (excluding READMEs):

- **`reference/packages/eden-git/tests/`** (4 files —
  `test_repo_branches_worktrees.py`, `test_repo_plumbing.py`,
  `test_environment_isolation.py`, `conftest.py`) — these
  exercise local-only `GitRepo` ops; they STAY on local-only
  paths because the new ops are tested separately in
  `test_remote_ops.py`.
- **`reference/packages/eden-git/src/eden_git/repo.py`** — the
  module under modification. New methods land here.
- **Service tests that init a local bare repo:**
  - `reference/services/_common/tests/test_worktrees.py` — pure
    worktree behavior, no remote — STAYS local.
  - `reference/services/planner/tests/test_planner_subprocess.py`
    — planner doesn't touch git refs (only proposals) — STAYS
    local.
  - `reference/services/implementer/tests/test_implementer_subprocess.py`,
    `test_host.py` — these exercise `_run_subprocess` and
    submit flows. The implementer change in §D.7 adds a single
    `repo.push_ref` call after the existing `repo.create_ref`.
    Tests gain a tiny bare-repo `init --bare` as the local
    "remote" + assertion that `push_ref` was invoked. Net
    change per test: ~5 lines.
  - `reference/services/evaluator/tests/test_evaluator_subprocess.py`
    — evaluator gains `fetch_ref` before worktree-add. Same
    pattern: local "remote" + one-line assertion.
  - `reference/services/orchestrator/tests/test_e2e.py`,
    `test_subprocess_e2e.py` — these are the heavyweights.
    Each spawns the full 5-process stack against an in-memory /
    sqlite store. Adding a real local file-URL remote here
    means: setUp creates `tmp_path/remote.git` as a bare repo,
    the test's `seed_bare_repo` pushes to it, each spawned
    service gets `--gitea-url file://<path>` instead of
    `--repo-path <path>`. Net change per file: ~30 lines, no
    new processes. Both tests are already `pytest.mark.e2e`,
    skipped on Windows.

**Total file changes for the cutover:** ~14 source files modified,
plus ~3 new files. Tests that don't touch a real bare repo
(everything in `_common/tests/test_common.py`, web-ui non-flow
tests, contracts tests, storage tests) are entirely unaffected.

The "most existing tests can stay on local-only paths" claim is
defensible because the **only** tests that previously exercised
the implementer/evaluator/integrator flows did so against an
in-process bare repo on disk; switching that bare repo to be
the remote (via `file://`) and adding a tiny local working clone
is mechanical — no new architecture surfaces.

## Verification

1. `uv run pytest -q` — full suite + new unit tests + new
   `test_remote_integrator.py` integration tests pass. (No
   `pytest.mark.gitea` marker — see §F-Tests for why
   `file://` is sufficient at unit/integration speed; HTTP
   transport is exercised by the smokes against the real
   Gitea container.)
2. `bash healthcheck/smoke.sh` — host-mode smoke passes,
   workers actually push to Gitea, integrator promotes via
   Gitea (asserted via `git ls-remote`).
3. `bash healthcheck/smoke-subprocess.sh` and
   `bash healthcheck/smoke-subprocess-docker.sh` — same.
4. `bash healthcheck/e2e.sh` — Web UI flow still passes; Web UI
   implementer module now pushes to Gitea on submit.
5. CI: all 11 jobs green; no new required jobs in this chunk
   (per the chunk-10d / 10e posture).

## Out of scope

- **Gitea HA / multi-instance.** Single Gitea, single bare-repo
  there. HA is a Phase 12+ control-plane concern.
- **SSH or HTTPS-with-TLS instead of plain HTTP.** SSH key
  management and TLS termination are their own follow-ups; plain
  HTTP Basic is enough for the reference (Gitea is on the
  compose internal network only; the same soft-boundary as the
  existing reference posture).
- **Gitea webhooks.** The orchestrator polls `Store` directly
  for state; Gitea doesn't need to notify anyone.
- **Multi-experiment Gitea.** This chunk creates ONE repo per
  experiment, named after the experiment id. Coexistence of
  multiple experiments on the same Gitea works naturally
  (different repo names) but no special wiring.
- **Branch protection inside Gitea.** The reference relies on
  CAS at the git layer (`--force-with-lease`); a Gitea-side
  branch protection rule would be redundant.
- **Updating branch protection on the GitHub side** to require
  any new CI job. Same posture as 10c/10d/10e/follow-up-A.

## Risks

- **Local ref + remote push atomicity.** Between the local
  `create_ref` and the remote `push_ref` there's a window
  where the local ref exists but the remote doesn't. If the
  worker host crashes there, the next startup's `git fetch
  --prune` removes the local-only orphan as a side effect
  (covers `work/*`). For local-only orphan `trial/*` refs in
  the orchestrator's clone, the same prune-on-fetch removes
  them. For orphan `trial/*` refs that ended up REMOTELY
  published (the §D.6 step-4a-failed case), §D.7c's
  store-authoritative reconciliation pass handles cleanup at
  integrator startup.
- **Credential exposure.** The credential-helper script lives
  on the host filesystem with mode 0755 (executable; mounted
  ro into the worker). Any process on the host with read
  access can grab the password. This matches the existing
  soft-isolation posture; documented in §7 of the
  reference-binding chapter alongside the existing DooD
  caveats. Hardened deployments substitute Docker secrets,
  Vault, or sysbox-isolated workers; the wrap shape is
  unchanged in those deployments.
- **Push-rejected during integration.** If two integrators race
  and one's push gets `RefRefused`, the loser runs §D.6 step
  4b (local rollback). No `trial.integrated` was emitted yet
  (only step 3 emits it), so the event log stays consistent.
- **Store-write-after-push failure.** The §D.6 step-4a flow
  ensures the remote ref is deleted before the local one if
  the store write fails. If 4a itself fails, the §D.7c
  reconciliation pass at the next integrator startup catches
  the orphaned remote ref.
- **Gitea downtime.** Workers exit on Gitea-unreachable;
  compose restarts them. Brief Gitea downtime should not lose
  data (workers just re-fetch when Gitea comes back); a
  mid-task Gitea outage maps to the role-specific failure
  contract per §D.7d above.
- **Plain HTTP transport.** All git traffic is unencrypted on
  the compose internal network. This is identical posture to
  the existing reference's `EDEN_SHARED_TOKEN` plain-HTTP
  traffic between workers and the task-store-server; the
  trust boundary is the compose network, not the wire.
  Hardened deployments terminate TLS at a sidecar or
  Gitea-side reverse proxy.
