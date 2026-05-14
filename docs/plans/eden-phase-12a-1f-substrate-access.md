# Phase 12a chunk 1f — Substrate read access for ideator + evaluator agents

**Status.** Draft.

**Predecessor.** [`docs/plans/eden-phase-12a-1-worker-identity.md`](eden-phase-12a-1-worker-identity.md)
shipped per-worker bearer auth, the per-experiment worker registry,
RBAC at claim time, and the
[`bootstrap_worker_credential`](../../reference/services/_common/src/eden_service_common/auth.py)
helper. Phase 10d follow-up B
([`docs/archive/eden-phase-10d-followup-b-gitea-remote.md`](../archive/eden-phase-10d-followup-b-gitea-remote.md))
shipped per-host Gitea-as-remote clones for the orchestrator,
executor, evaluator, and web-ui — but **not** the ideator host
(the scripted ideator doesn't need git access; an agentic ideator
does).

**Roadmap.** [`docs/roadmap.md`](../roadmap.md) §"Phase 12 — Worker
identity & lifecycle" lists 12a-1 as the foundation; this chunk
(12a-1f) is a tactical follow-on that opens the **read-side
substrates** — git, artifact store, Postgres event log — to the
user-supplied `*_command` subprocesses spawned by the
**ideator** and **evaluator** hosts so an agentic role
implementation can explore experiment state instead of issuing
hundreds of fine-grained wire calls. The chunk is sequenced
ahead of the longer-horizon Phase-13 substrate plans
([13c](eden-phase-13c-managed-postgres.md) /
[13d](eden-phase-13d-blob-backend.md) /
[13e](eden-phase-13e-gitea-hardening.md)); those plans will
replace the tactical pieces this chunk lands as they mature.

**Naming.** Pre-draft check against
[`docs/glossary.md`](../glossary.md) and AGENTS.md
"Naming discipline":

- "substrate" is the user's term for "the underlying service /
  storage tier exposed beneath the wire" (git remote, blob store,
  Postgres event log). It is not in the glossary; the glossary
  uses **"artifact store"** for chapter-8 §5's normative concept
  and treats git + Postgres as implementation substrates. This
  plan uses "substrate" only in prose / file names — no
  protocol-level identifier is renamed.
- The role / verb / kind / submission / artifact alignment from
  [`docs/glossary.md`](../glossary.md) is unchanged: this chunk
  adds no new role, verb, kind, submission, or artifact.
- New environment-variable names (`EDEN_REPO_DIR`,
  `EDEN_ARTIFACT_URL`, `EDEN_ARTIFACT_PATH_ROOT`,
  `EDEN_READONLY_STORE_URL`) follow the existing
  [`spec/v0/reference-bindings/worker-host-subprocess.md`](../../spec/v0/reference-bindings/worker-host-subprocess.md)
  §1.1 `EDEN_*` convention.
- New HTTP path
  (`/_reference/experiments/{experiment_id}/artifacts/{path:path}`)
  uses the existing reference-helper convention
  ([`reference/packages/eden-wire/src/eden_wire/server.py`](../../reference/packages/eden-wire/src/eden_wire/server.py)
  line ~853 — `ref_base = "/_reference/experiments/{experiment_id}"`).
  Scoping by `experiment_id` matches the rest of the
  `/_reference/` surface and gives the route's handler an
  experiment-id-mismatch guard parallel to the chapter-7
  §1.3 invariant on the normative endpoints.
- Pre-submit `scripts/check-rename-discipline.py` clean.

## 1. Context

### 1.1 What 12a-1 shipped

Phase 12a-1 (commit `84adb50`) made **identity** a first-class
concept: each worker is registered against the experiment's
store, holds a per-worker bearer credential, and authenticates
into the wire as `Authorization: Bearer <worker_id>:<token>`
(chapter 7 §13). Subprocess hosts (Phase 10d) thread
`EDEN_WORKER_ID` + `EDEN_WORKER_CREDENTIAL` into the spawned
`*_command` children so user-written role logic can issue its
own wire calls with the host's identity (binding
[`spec/v0/reference-bindings/worker-host-subprocess.md`](../../spec/v0/reference-bindings/worker-host-subprocess.md)
§1.1 already documents both env vars).

### 1.2 The ergonomic gap

The wire surface is **transactional**: claim, submit, accept,
reject — one round-trip per state transition, one Variant /
Idea / Event read per HTTP call. That is correct for the
protocol but ergonomically wrong for the **exploratory** access
patterns a programmatic ideator or evaluator agent needs:

- An ideator wants "what's been tried, what scored well, what
  did the evaluators say about the failures" — that's a join
  across many Variants, the evaluation manifest at
  `.eden/variants/<id>/evaluation.json` on each
  `variant/*` ref, the ideas in the parent chain, and any
  evaluator-supplied artifacts. Doing it through the wire is
  N+1 round-trips per question.
- An evaluator wants to read the **idea content** that led to
  the variant being evaluated, the variant's git tree (without
  a worktree of just that one commit), and possibly the
  evaluator-side artifacts of *other* recent evaluations
  (calibration / drift checks). Again, exploration shape.
- Both want to filter / aggregate / project across the event
  log — "all variants completed in the last hour", "average
  score per ideator", etc. — patterns that map naturally to
  SQL `SELECT` against the event table and awkwardly to
  chapter-7 §6 polling.

### 1.3 The architectural pattern

Give the agent **read-only access to the same substrates the
internal services use, scoped per role.** Concretely, three
substrates:

| Substrate | What it backs | Read shape for the agent |
|---|---|---|
| Git (Gitea) | All branches + the evaluation manifest at the variant tip | `git log` / `git show` against a local bare clone |
| Artifact store | Inline content: idea content markdown, evaluator artifacts (`file://` URIs today) | HTTP GET against a thin route on the task-store-server |
| Postgres event log | Tasks, ideas, variants, events, workers, groups | SELECT against a read-only Postgres role |

All three are network-reachable so the agent can run cross-machine
(e.g. an LLM client on a developer laptop calling into a
Compose stack on a server). No `file://` mount-based access.

This chunk is the **smallest set of changes** that opens all
three substrates to the **ideator + evaluator subprocesses**.
The executor subprocess is intentionally **out of scope** (it
already has read-side git access via its own clone + cwd-set-to-
worktree; if an agentic executor needs broader access later it
would be a separate chunk).

### 1.4 Out-of-scope (deferred)

- **Executor-agent substrate access.** Operator decision.
- **Per-worker Gitea tokens with branch ACLs.** Today the
  Gitea repo has a single shared `eden` HTTP-Basic credential
  (Phase 10d-followup-B); per-worker identities + branch
  protection live in
  [Phase 13e](eden-phase-13e-gitea-hardening.md).
- **Managed Postgres.** Phase 10b-10c's in-stack Postgres is
  the read substrate here; [Phase 13c](eden-phase-13c-managed-postgres.md)
  switches to an operator-managed instance and ports the
  readonly-role concept cleanly.
- **Real artifact-backend abstraction (`LocalFsBackend` /
  `S3Backend` / `GcsBackend`).** This chunk ships a tactical
  ~50-100 LOC HTTP route on the task-store-server; [Phase 13d](eden-phase-13d-blob-backend.md)
  replaces it with the full `Backend` Protocol + cloud-storage
  backends. The 12a-1f route is **expected to be thrown
  away** by 13d.
- **Spec amendment.** No normative chapter is changed. The
  binding doc at
  [`spec/v0/reference-bindings/worker-host-subprocess.md`](../../spec/v0/reference-bindings/worker-host-subprocess.md)
  is informative; 12a-1f extends it with the new env vars
  and a new §-on-substrate-access. The wire-protocol chapter 7
  §11's reference-only `/_reference/` namespace already
  documents the bypass mechanism this chunk's route mounts
  under.

## 2. Decisions captured before drafting

Six load-bearing decisions were settled with the operator
before this plan was drafted. They are recorded here so
codex-review and future readers don't re-litigate them.

1. **Cross-machine assumption.** Workers / agent subprocesses
   may run on a different machine than the experiment stack.
   All substrate-access mechanisms are HTTP / network-reachable;
   no mechanism depends on a `file://` mount that only resolves
   on a single host.
2. **Scope: ideator + evaluator only.** Executor-agent access
   is out of scope. The executor already has cwd-anchored
   git access (its worktree is `git worktree add` against
   the bare clone) — agent-shaped broader access (full repo,
   readonly Postgres, artifact server) is a follow-on chunk
   when there's a concrete agentic-executor design.
3. **Artifact server: tiny route on `task-store-server`, NOT a
   separate service.** Adds the route to the existing FastAPI
   app under `/_reference/experiments/{experiment_id}/artifacts/{path:path}` (the chapter-7 §11
   reference-only namespace). Minimum deployment-shape change;
   the route is ~50-100 LOC and explicitly expected to be
   thrown away by Phase 13d's `Backend` abstraction.
4. **Auth: same `<principal>:<secret>` bearer model, either-gated
   on reads.** Reuses
   [`eden_wire.auth.authenticate`](../../reference/packages/eden-wire/src/eden_wire/auth.py)
   (12a-1's normative auth scheme). Reads accept admin OR
   worker bearers. No new auth scheme; no new credential type.
5. **`artifacts_uri` stays `file://` in the protocol /
   event payloads.** The wire shape is unchanged. The new
   route provides the host-side translation:
   `file:///var/lib/eden/artifacts/foo.md` →
   `http://task-store-server:8080/_reference/artifacts/foo.md`
   for the agent. Phase 13d sorts out URI schemes properly.
6. **Path-traversal protection: standard `Path.resolve()` +
   containment check.** Reject `..` / absolute paths /
   symlinks escaping the artifacts root. Mirrors the existing
   `_read_inline_artifact` helper at
   [`reference/services/web-ui/src/eden_web_ui/routes/_helpers.py`](../../reference/services/web-ui/src/eden_web_ui/routes/_helpers.py).

## 3. Design

### D.0 Substrate boundary (read this first)

The three substrates are deliberately **independent** —
opening one without the others is a valid deployment.
Operationally:

| Substrate | What it exposes | Who reads it |
|---|---|---|
| Git (Gitea HTTP + local bare clone) | All refs (`refs/heads/variant/*`, `refs/heads/work/*`), commit tree, evaluation manifest | Any subprocess that calls `git log` / `git show` against `$EDEN_REPO_DIR` |
| Artifact server (HTTP route) | Files under `<artifacts-dir>` (idea content, evaluator-supplied bytes) | Any subprocess that GETs `${EDEN_ARTIFACT_URL}<relative-path>` with the §13.1 bearer |
| Postgres readonly role | SELECT on the eden schema's tables (`experiment`, `task`, `submission`, `idea`, `variant`, `event`, `worker_group`, `group_membership`, `schema_version`) plus column-projection on `worker(worker_id, data)` | Any subprocess that connects to `$EDEN_READONLY_STORE_URL` |

There are **no cross-substrate joins**; the agent does the
join on its side. That's intentional — each substrate is a
separate trust + scope concern, and the protocol does NOT
mediate the join.

### D.1 Git substrate: ideator gains a local clone

Today (post-10d-followup-B):

| Host | `--repo-path` | `--gitea-url` | `--credential-helper` | Per-host volume |
|---|---|---|---|---|
| orchestrator | yes | yes | yes | `eden-orchestrator-repo` |
| executor | yes | yes | yes | `eden-executor-repo` |
| evaluator | yes | yes | yes | `eden-evaluator-repo` |
| web-ui | yes | yes | yes | `eden-web-ui-repo` |
| **ideator** | **no** | **no** | **no** | **none** |

12a-1f adds the same three flags + a new per-host volume
`eden-ideator-repo` to the ideator host. The startup logic
mirrors the executor/evaluator pattern:

1. If `--repo-path` is not yet a git repo, `clone --bare`
   from `--gitea-url` using `--credential-helper`.
2. Otherwise, `git fetch --prune origin
   '+refs/heads/*:refs/heads/*'` to refresh ALL local heads.
3. Enter the normal ideator poll loop.

The bare clone is sufficient for `git log` / `git show` /
tree walks. Worktrees are not needed (the ideator never
operates on a worktree today either).

**Subprocess env-var threading.** The ideator + evaluator
hosts gain a new env var passed to every spawned `*_command`:

```text
EDEN_REPO_DIR=/var/lib/eden/repo
```

This is the **host-side** path to the worker's local bare
clone. The subprocess can read it with `git -C $EDEN_REPO_DIR
log` etc. Cross-host concern: the path is meaningful only
inside the worker-host container; the bind-mount from compose
makes it stable across `compose up` cycles. Off-host operators
(agent on a developer laptop) get their own gitea credentials
plus URL via 13e and clone independently — the binding doc
notes this explicitly.

**Why not also add `EDEN_REPO_DIR` to the executor subprocess?**
Out of scope per Decision 2. The executor subprocess today
runs with `cwd=$EDEN_WORKTREE` (a per-task git worktree
backed by the bare clone), so a `git log` / `git show` inside
the worktree dir resolves against the bare repo transparently
via the `.git` gitlink — no `EDEN_REPO_DIR` is strictly
needed for the executor's current shape. If a future
agentic-executor needs broader access (e.g. cross-variant
history walks), a follow-on chunk wires it.

### D.2 Artifact server: tactical HTTP route on the task-store-server

#### D.2.a Route shape

```text
GET /_reference/experiments/{experiment_id}/artifacts/{path:path}
Authorization: Bearer <principal>:<secret>
```

| Param | Source | Constraint |
|---|---|---|
| `experiment_id` | URL path component | MUST equal `store.experiment_id` — mismatch → 400 (parallel to chapter-7 §1.3 on the normative endpoints) |
| `path` | URL path component | URL-decoded; non-empty; relative |
| principal | Bearer header | `admin` OR registered worker_id |
| secret | Bearer header | constant-time-compared per [§13.1](../../spec/v0/07-wire-protocol.md) |

Response shape:

| HTTP | Body | When |
|---|---|---|
| `200 OK` | file bytes (≤ 1 MiB) + safe-delivery headers (see below) | Path resolves to a regular file under `artifacts_dir`, file size ≤ 1 MiB |
| `400 Bad Request` | RFC 7807 `eden://error/experiment-id-mismatch` | The URL's `experiment_id` does not equal `store.experiment_id` |
| `400 Bad Request` | RFC 7807 `eden://reference-error/invalid-path` | Path contains a NUL byte, exceeds OS path limits, or otherwise fails to resolve (`ValueError` from `Path.resolve` / `OSError` from `stat`). Includes symlink-loop `ELOOP`. |
| `401 Unauthorized` | RFC 7807 `eden://error/unauthorized` | Missing / malformed bearer; bad token |
| `403 Forbidden` | RFC 7807 `eden://error/forbidden` | Resolved path is OUTSIDE `artifacts_dir` (traversal); OR an open-time check confirms the opened inode is OUTSIDE the root (TOCTOU mitigation) |
| `404 Not Found` | RFC 7807 `eden://error/not-found` | Resolved path is inside `artifacts_dir` but doesn't exist or is not a regular file |
| `413 Payload Too Large` | RFC 7807 `eden://reference-error/artifact-too-large` | File is a regular file under root but its size exceeds 1 MiB |
| `503 Service Unavailable` | RFC 7807 `eden://reference-error/artifact-serving-disabled` | task-store-server was started without `--artifacts-dir`; the deployment opted out of artifact serving |

**Error-vocabulary discipline.** The new artifact route is
reference-only (`/_reference/...`), so its three new error
URIs use the **reference-only** `eden://reference-error/...`
namespace (pre-existing — see chapter 7 §12's
`eden://reference-error/unauthorized`). The normative
chapter-7 error vocabulary (§7 closed set) is **unchanged**:
no new `eden://error/...` types are added. Errors the route
reuses (`unauthorized`, `forbidden`, `not-found`,
`experiment-id-mismatch`) are pre-existing normative types
the chapter already defines.

**Safe browser-delivery headers.** Every 200 response from
this route includes:

| Header | Value | Why |
|---|---|---|
| `Content-Disposition` | `attachment` (with optional `filename="<basename>"`) | Forces the user-agent to treat the response as a download, NOT to render inline. Defeats stored-XSS via attacker-controlled `.html` / `.svg` artifacts (an ideator subprocess writes the body; an evaluator on the same deployment reads it). |
| `X-Content-Type-Options` | `nosniff` | Prevents the user-agent from second-guessing the declared `Content-Type` based on body sniffing. |
| `Content-Type` | `application/octet-stream` | Generic; the agent decodes via the `artifacts_uri`-domain knowledge it already has. No `text/html`, no `image/svg+xml`, no other browser-executable types are ever sent. |

This route is agent-facing, not human-facing — the existing
web-ui `/artifacts?uri=…` route at
[`reference/services/web-ui/src/eden_web_ui/routes/artifacts.py`](../../reference/services/web-ui/src/eden_web_ui/routes/artifacts.py)
keeps its mime-guessing posture for human-browser usability;
the 12a-1f route trades human ergonomics for hostile-content
safety.

**Always-mounted, conditionally-enabled.** The route is
mounted on every task-store-server (regardless of
`--artifacts-dir`); when `--artifacts-dir` is `None` it
returns 503 with a closed-vocabulary error. Conditional
mounting would force operators to read the OpenAPI surface
to discover the route's existence; the 503 signal is more
discoverable.

**1 MiB cap.** Mirrors the existing
[`_read_inline_artifact`](../../reference/services/web-ui/src/eden_web_ui/routes/_helpers.py)
helper's cap. Files larger than 1 MiB return 413 without
serving any bytes (no partial response). Phase 13d's
`Backend` abstraction handles streaming + range requests
properly.

The route lives under `/_reference/experiments/{experiment_id}/`
(NOT `/v0/...`) because chapter 7 §11 already documents
`/_reference/` as the **reference-only**, non-normative
namespace and the existing reference helpers in
[`server.py`](../../reference/packages/eden-wire/src/eden_wire/server.py)
already use the `experiment_id`-scoped form. The
chapter-7-normative URL space is preserved.

#### D.2.b Mount location

The route lives in
[`reference/packages/eden-wire/src/eden_wire/server.py`](../../reference/packages/eden-wire/src/eden_wire/server.py)
alongside the existing reference helpers (which already use
the same `ref_base = "/_reference/experiments/{experiment_id}"`
template). The existing auth middleware skips `/_reference/`
paths by default (auth.py line ~160), so this route does its
own auth check by calling
[`eden_wire.auth.authenticate(request.headers.get("authorization"), admin_token=admin_token, store=store)`](../../reference/packages/eden-wire/src/eden_wire/auth.py)
inside the handler. This keeps the middleware oblivious and
makes the route's auth posture self-contained.

#### D.2.c Path resolution + containment

```python
import os
import errno
import stat

ARTIFACT_ROOT = artifacts_dir.resolve() if artifacts_dir else None
MAX_ARTIFACT_BYTES = 1 * 1024 * 1024  # 1 MiB (mirrors _read_inline_artifact)

# Path components that are NEVER valid in an artifact path —
# rejected before any filesystem call so the component-walk
# below sees only well-formed segments.
_REJECT_COMPONENTS = {"", ".", ".."}


def _open_artifact_fd(root: Path, rel_path: str) -> int:
    """Open `rel_path` BENEATH `root` and return the fd.

    Walks each path component descriptor-relative from the
    opened root dirfd. Every intermediate component opens
    `O_PATH | O_DIRECTORY | O_NOFOLLOW`; the terminal opens
    `O_RDONLY | O_NOFOLLOW`. A symlink at ANY component
    raises `OSError(errno=ELOOP)` (mapped to 403 by the
    caller). This is the descriptor-relative equivalent of
    Linux 5.6+ ``openat2(RESOLVE_BENEATH)`` and is the only
    pattern that closes the intermediate-component TOCTOU
    window noted by Codex round 2: a concurrent renamer
    cannot swap an intermediate dir to a symlink while we
    walk because each step is anchored by the prior step's
    fd, not by a path string.

    Raises:
      OSError on FileNotFoundError / ELOOP / EACCES / etc.
      ValueError on malformed components (NUL byte, `..`,
        empty segment after split).
    """
    parts = rel_path.split("/")
    if any(p in _REJECT_COMPONENTS for p in parts):
        raise ValueError(f"invalid path component in {rel_path!r}")
    if any("\0" in p for p in parts):
        raise ValueError(f"NUL byte in path {rel_path!r}")

    root_fd = os.open(
        root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    try:
        # Walk intermediate dirs.
        current_fd = root_fd
        for intermediate in parts[:-1]:
            try:
                next_fd = os.open(
                    intermediate,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=current_fd,
                )
            finally:
                if current_fd != root_fd:
                    os.close(current_fd)
            current_fd = next_fd
        # Open terminal as regular file (O_NOFOLLOW rejects
        # a symlink at the terminal).
        try:
            terminal_fd = os.open(
                parts[-1],
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=current_fd,
            )
        finally:
            if current_fd != root_fd:
                os.close(current_fd)
        return terminal_fd
    finally:
        os.close(root_fd)


@app.get(
    "/_reference/experiments/{experiment_id}/artifacts/{path:path}",
)
async def serve_artifact(
    experiment_id: str, path: str, request: Request
) -> Response:
    # 1. Auth-first (NEVER touch the filesystem before auth):
    if admin_token is not None:
        authenticate(
            request.headers.get("authorization"),
            admin_token=admin_token,
            store=store,
        )

    # 2. Experiment-id-mismatch guard (chapter-7 §1.3 parity):
    if experiment_id != store.experiment_id:
        raise ExperimentIdMismatch(
            url=experiment_id, header=store.experiment_id,
        )

    # 3. Disabled-deployment guard:
    if ARTIFACT_ROOT is None:
        raise ServiceUnavailable(
            "artifact-serving-disabled",
            "task-store-server started without --artifacts-dir",
        )

    # 4. Descriptor-relative walk beneath the root. The
    #    component-walk closes the intermediate-component
    #    TOCTOU gap (an attacker swapping an intermediate
    #    dir to a symlink can't sneak the walk out of the
    #    root because each step is anchored by the prior
    #    step's fd, not by a re-resolved path string).
    try:
        fd = _open_artifact_fd(ARTIFACT_ROOT, path)
    except ValueError as exc:
        # NUL byte, empty component, `..`, etc.
        raise BadRequest(
            "invalid-path", f"invalid path: {exc!r}",
        ) from exc
    except FileNotFoundError:
        raise NotFound("artifact not found")
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            # Symlink at ANY path component (intermediate or
            # terminal). NEVER follow.
            raise Forbidden("symlink not allowed")
        # EACCES / ENOTDIR / ENAMETOOLONG / etc. — collapse
        # to 404 to avoid leaking filesystem layout.
        raise NotFound("artifact not found") from exc

    try:
        st = os.fstat(fd)

        # 5. Regular-file check ON THE OPEN FD:
        if not stat.S_ISREG(st.st_mode):
            raise NotFound("artifact not found")

        # 6. Size cap on the open fd:
        if st.st_size > MAX_ARTIFACT_BYTES:
            raise PayloadTooLarge(
                "artifact-too-large",
                f"artifact exceeds {MAX_ARTIFACT_BYTES}-byte cap",
            )

        # 7. Read all bytes (1 MiB cap means in-memory is fine).
        bytes_ = os.read(fd, MAX_ARTIFACT_BYTES + 1)
        if len(bytes_) > MAX_ARTIFACT_BYTES:
            # Race: file grew between fstat and read.
            raise PayloadTooLarge(
                "artifact-too-large",
                "artifact size grew during read",
            )

        # 8. Return with safe-delivery headers (§D.2.a).
        filename = path.rsplit("/", 1)[-1] or "artifact"
        return Response(
            content=bytes_,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{filename}"'
                ),
                "X-Content-Type-Options": "nosniff",
            },
        )
    finally:
        os.close(fd)
```

**Why a descriptor-relative component walk (not
`Path.resolve()` + a single `os.open`)?** Codex round 2
finding: a `resolve()`-then-`open()` pattern only protects
the terminal component from being a symlink. An attacker
who can swap an intermediate directory to a symlink
between `resolve()` and `open()` can sneak the walk out
of the root and back before any post-open re-check sees
the malicious state. The component walk anchors every
step by the prior step's fd, so once a real-directory
inode is locked, no rename at its path can affect what
the next `os.open(..., dir_fd=current_fd)` resolves to.
This is the same property Linux 5.6+ exposes as
`openat2(RESOLVE_BENEATH)`; Python's stdlib doesn't bind
that syscall but the manual walk gives the same
guarantees.

**Why `Response(content=…)` instead of `FileResponse`?**
`FileResponse` re-opens the path from scratch at body-
write time — re-opening the TOCTOU window the component
walk just closed. For the 1 MiB cap a fixed-bytes
`Response` is the right shape; for >1 MiB Phase 13d's
`Backend` abstraction owns streaming via cloud SDKs that
don't go through the filesystem.

Three small but load-bearing posture choices:

1. **Auth-first, before any path inspection.** Resolving
   the path before auth would leak existence-of-files via
   timing / response-code differences on unauth requests. The
   route MUST run auth before any filesystem call.
2. **Traversal → 403, not 404.** The existing web-ui route at
   [`reference/services/web-ui/src/eden_web_ui/routes/artifacts.py`](../../reference/services/web-ui/src/eden_web_ui/routes/artifacts.py)
   uses 404 to avoid leaking the artifacts-dir layout to an
   unauthenticated browser. This route is bearer-auth-gated;
   distinguishing 403 (caller is authenticated but the request
   shape was illegal) from 404 (legitimate request, no such
   file) is more useful for operator diagnostics + future
   audit tooling. Both response bodies are the same generic
   RFC-7807 envelope; no path is echoed.
3. **`is_file()`, not `exists()`.** Symlinks to a directory,
   FIFOs, sockets — all 404. The host filesystem doesn't have
   those today, but the guard is cheap and matches the
   existing `_read_inline_artifact` posture.

#### D.2.d Compose plumbing

The task-store-server's `compose.yaml` block gains the
artifacts volume as a **read-only** bind:

```yaml
volumes:
  - eden-artifacts-data:/var/lib/eden/artifacts:ro
```

The named volume `eden-artifacts-data` already exists
(declared at the bottom of compose.yaml with explicit
`name: eden-artifacts-data` for the chunk-10d-followup-A
docker-exec wrap). Today only `web-ui` mounts it; 12a-1f
adds a `:ro` mount on `task-store-server`.

The task-store-server CLI gains a `--artifacts-dir` flag
(default `None`). The route is **always mounted** regardless;
when `--artifacts-dir` is `None` it returns 503 (§D.2.a).
Compose passes `--artifacts-dir /var/lib/eden/artifacts`.

#### D.2.e Subprocess env vars

The ideator + evaluator hosts thread two new env vars into
every spawned `*_command`:

| Variable | Value (in-network compose) | Meaning |
|---|---|---|
| `EDEN_ARTIFACT_URL` | `http://task-store-server:8080/_reference/experiments/<experiment-id>/artifacts/` | HTTP base URL ending in `/`, with the deployment's `experiment_id` already interpolated. Concatenate `<relative-path>` to GET. |
| `EDEN_ARTIFACT_PATH_ROOT` | `/var/lib/eden/artifacts` | Host-side filesystem root the URL is rooted at. Used by the subprocess to translate a `file:///var/lib/eden/artifacts/foo.md` URI from the wire into a relative path `foo.md` → URL `${EDEN_ARTIFACT_URL}foo.md`. |

The bearer the subprocess uses is the existing 12a-1
`f"{EDEN_WORKER_ID}:{EDEN_WORKER_CREDENTIAL}"` per
[binding §1.1](../../spec/v0/reference-bindings/worker-host-subprocess.md).
No new bearer is issued.

**Why two env vars instead of one?** A single
`EDEN_ARTIFACT_URL` would force the subprocess to hard-code
the `/var/lib/eden/artifacts` prefix, OR the subprocess
would have to send the full `file://` URI as a query
parameter and the server would have to accept query-param
URIs (which complicates the trust boundary). Two env vars
keep the translation mechanical and the server's input
surface narrow (pure path component, no `file://` parsing).

### D.3 Postgres readonly substrate

#### D.3.a Role provisioning

The deployment's Postgres instance gets a second role,
`eden_readonly`, with SELECT access to the public schema —
**explicitly EXCLUDING the `worker.credential_hash`
column**.

The real schema (per
[`reference/packages/eden-storage/src/eden_storage/_postgres_schema.py`](../../reference/packages/eden-storage/src/eden_storage/_postgres_schema.py))
uses the `data text NOT NULL` pattern across all
artifact-bearing tables (each row's payload is a JSON
document stored as text); the only structured columns are
primary keys, foreign keys, indexed sort keys, and the
`worker.credential_hash` argon2id hash. So the
column-level exclusion is targeted at exactly one column,
not a whole-table view layer:

```sql
CREATE ROLE eden_readonly WITH LOGIN PASSWORD '<random-32-byte-hex>';
GRANT CONNECT ON DATABASE eden TO eden_readonly;
GRANT USAGE ON SCHEMA public TO eden_readonly;

-- Tables with no credential material: full-table SELECT.
GRANT SELECT ON
    experiment, task, submission, idea, variant, event,
    worker_group, group_membership, schema_version
    TO eden_readonly;

-- The `worker` table is (worker_id text, data text,
-- credential_hash text). Column-level SELECT exposes
-- worker_id + data (the JSON payload with labels,
-- registered_at, registered_by, etc.) but NOT
-- credential_hash.
GRANT SELECT (worker_id, data) ON worker TO eden_readonly;
```

**Why exclude `credential_hash`?** Codex round 1 finding
on credential surface: argon2id hashes are meaningfully
sensitive (weak credentials can be brute-forced offline).
The agent's legitimate need is exploratory reads — the
worker's `data` JSON column carries labels, registration
timestamps, and the public attribution fields. The hash
column is never part of that read shape.

**The column-level GRANT does NOT silently elide the
excluded column from broad query forms — it makes those
forms fail.** Codex round 2 finding: PostgreSQL's
permission check on `SELECT *` is against the full set of
columns the parser expanded into; if any one column lacks
`SELECT`, the whole statement fails with permission
denied. Same for `COPY <table> TO …` and `pg_dump`-shape
table reads. The operator-visible posture is:

- `SELECT worker_id, labels FROM worker` — works.
- `SELECT * FROM worker` — fails ("permission denied for
  column credential_hash").
- `COPY worker TO STDOUT` — fails (same reason).
- `pg_dump --table=worker` — fails.

Operators querying the readonly substrate MUST project
columns explicitly. The operator doc
(`docs/operations/agent-readonly-db.md`) documents this
posture with worked examples. Tests in §6.5 lock the
behavior on both the `SELECT *` and the
`SELECT credential_hash` paths.

**Default privileges (`ALTER DEFAULT PRIVILEGES`) is
intentionally OMITTED.** A blanket
`GRANT SELECT ON TABLES` for any future table would
silently re-expose credential-bearing columns if a future
schema migration adds one. The deliberately-explicit
table-level GRANT means every schema bump that adds a new
table needs a parallel hand-written GRANT — annoying, but
the cost is the price of the safety property. The 12a-1f
implementation provisions exactly the tables enumerated
above; any future schema-bump chunk must extend the GRANT
list in `ensure_readonly_role` and add a test asserting
the new table is reachable from `eden_readonly`.

#### D.3.b Where the provisioning runs

**Option taken: `task-store-server` provisions the role on
startup, idempotently.** The existing `PostgresStore`
already runs `ensure_schema(conn)` at startup; the readonly
provisioning is a sibling step keyed off a new CLI flag.

```python
# Pseudocode, in build_store / task-store-server startup:
if isinstance(store, PostgresStore) and readonly_password is not None:
    ensure_readonly_role(
        store._conn,  # or a dedicated connection
        username="eden_readonly",
        password=readonly_password,
    )
```

The `ensure_readonly_role` helper runs an explicit
**REVOKE-then-GRANT** sequence — it MUST NOT trust any
pre-existing grants on the role (e.g. a legacy deployment
with `GRANT SELECT ON ALL TABLES` from an earlier draft):

1. `CREATE ROLE eden_readonly WITH LOGIN PASSWORD '…'` if absent.
2. `ALTER ROLE eden_readonly WITH PASSWORD '…'` if present
   (handles password rotation across re-runs of
   setup-experiment with a fresh `EDEN_READONLY_PASSWORD`).
3. **REVOKE ALL** privileges from the role on every table,
   schema, and database — bulk-revoke first so leftover
   over-grants from a prior schema cannot persist:

   ```sql
   REVOKE ALL ON ALL TABLES IN SCHEMA public FROM eden_readonly;
   REVOKE ALL ON SCHEMA public FROM eden_readonly;
   REVOKE ALL ON DATABASE eden FROM eden_readonly;
   -- Also revoke any default privileges previously installed
   -- by an earlier provisioning that may have used
   -- ALTER DEFAULT PRIVILEGES:
   ALTER DEFAULT PRIVILEGES IN SCHEMA public
       REVOKE SELECT ON TABLES FROM eden_readonly;
   ```

4. GRANT exactly the safe-set defined in §D.3.a
   (table-level on the non-worker tables; column-level on
   `worker` excluding `credential_hash`). The
   per-column GRANT is the authoritative artifact —
   `ALTER DEFAULT PRIVILEGES` is **NOT used** because it
   would re-expose any future credential-bearing column
   added to a new table by a schema bump.

In-memory / SQLite backends: the flag is a no-op with a
warning (the readonly substrate is Postgres-specific). The
operator's docs note the limitation.

In-memory / SQLite backends: the flag is a no-op with a
warning (the readonly substrate is Postgres-specific). The
operator's docs note the limitation.

**Why not provision via setup-experiment.sh directly?** The
setup-experiment script does NOT currently bring postgres
up; it brings only gitea up synchronously. Adding a
postgres-up + provision step would mean either (a)
duplicating the postgres-bring-up logic in shell, or (b)
running an additional `compose run --rm psql` after a
postgres-up step. The task-store-server already opens a
Postgres connection on startup and is the natural place to
own schema-shaped concerns; the role-provisioning step
fits the existing posture. setup-experiment's job stays
"generate secrets, write `.env`, seed the bare repo".

#### D.3.c Subprocess env var

The ideator + evaluator hosts thread one new env var into
every spawned `*_command`:

```text
EDEN_READONLY_STORE_URL=postgresql://eden_readonly:<pwd>@postgres:5432/eden
```

In-network compose: `postgres:5432`. Cross-host: the operator
substitutes a reachable hostname. The subprocess connects
via any Postgres client library; the protocol does NOT
specify a client shape.

#### D.3.d Why a separate password env var?

The task-store-server connects as the `eden` superuser to
provision the readonly role (it needs CREATE ROLE
privilege). The readonly role's password is **separate**
from `POSTGRES_PASSWORD`:

- `POSTGRES_PASSWORD` → `eden` superuser → owns + writes
  everything.
- `EDEN_READONLY_PASSWORD` → `eden_readonly` role → SELECT
  only.

setup-experiment generates `EDEN_READONLY_PASSWORD`
(preserved across re-runs, same posture as the other
generated secrets), writes it to `.env`, and the rendered
`EDEN_READONLY_STORE_URL` interpolates it.

### D.4 Cross-machine posture

The chunk's load-bearing decision is that an agent may run
**off-host** — e.g., an LLM client on a developer laptop
calling into a Compose stack running on a server. The three
substrates' off-host stories:

| Substrate | On-host (compose-internal) | Off-host (cross-machine) |
|---|---|---|
| Git | bind-mount path `/var/lib/eden/repo`, no auth | `git clone http://gitea:3000/eden/<id>.git` with shared `eden` Basic auth (today; Phase 13e adds per-worker tokens). Operator-supplied gitea URL + creds. |
| Artifact server | `http://task-store-server:8080/_reference/artifacts/` with `EDEN_WORKER_CREDENTIAL` bearer | Same URL (if operator exposes the port externally) or operator's chosen reverse-proxy hostname; same bearer. |
| Postgres readonly | `postgresql://eden_readonly:…@postgres:5432/eden` | Same DSN but operator-substituted hostname; same readonly role + password. |

The binding doc explicitly notes: **the env vars are
on-host-default; cross-host operators bring their own
substitutions.** This chunk's surface is the on-host shape;
cross-host integration is operator policy.

### D.5 Spec posture

**No normative spec amendment.** This chunk extends only the
**informative**
[`spec/v0/reference-bindings/worker-host-subprocess.md`](../../spec/v0/reference-bindings/worker-host-subprocess.md)
binding doc. Three changes to that doc:

1. §1.1 environment table gains three new rows:
   `EDEN_REPO_DIR`, `EDEN_ARTIFACT_URL`,
   `EDEN_ARTIFACT_PATH_ROOT`, `EDEN_READONLY_STORE_URL`.
2. A new §9 "Substrate read-access for agent role
   implementations" documents the three substrates, the
   on-host vs off-host posture, and the trust-boundary
   caveats (the readonly Postgres role can see attribution
   fields including worker_ids; subprocesses with the
   readonly DSN can list all workers in the experiment).
3. The §-on-§1.2 cwd table is unchanged (the new env vars
   are passed regardless of cwd).

The normative chapters 02 / 03 / 04 / 07 / 08 are
unchanged. Specifically:

- Chapter 8 §5 (artifact store, deferred) is unchanged —
  this chunk's HTTP route is a **reference-impl extension
  to chapter 7 §11's `/_reference/` namespace**, not an
  implementation of the deferred §5 contract.
- Chapter 7 §13 (auth) is unchanged — the new route
  reuses the existing bearer.
- Chapter 7 §7 normative error vocabulary (the closed
  `eden://error/...` set) is **unchanged**. The new route
  emits the pre-existing normative types
  (`unauthorized`, `forbidden`, `not-found`,
  `experiment-id-mismatch`) for cases those already cover,
  and introduces three new reference-only types under
  the existing `eden://reference-error/...` namespace
  (chapter 7 §12 reference-only extension surface):
  `eden://reference-error/invalid-path`,
  `eden://reference-error/artifact-too-large`,
  `eden://reference-error/artifact-serving-disabled`.
  None of these are normative; conforming alternative
  implementations may emit different reference-error types
  or omit the route entirely.

## 4. Scope

In:

- `eden_ideator_host` gains `--repo-path`, `--gitea-url`,
  `--credential-helper` CLI flags + clone-on-startup logic
  mirroring executor/evaluator.
- `eden-wire` gains the `/_reference/experiments/{experiment_id}/artifacts/{path:path}`
  route + its own auth dispatch + path containment.
- `eden-task-store-server` gains `--artifacts-dir` +
  `--readonly-password` CLI flags.
- `eden-storage.postgres` gains `ensure_readonly_role`.
- `eden_ideator_host` + `eden_evaluator_host` subprocess
  modes thread `EDEN_REPO_DIR`, `EDEN_ARTIFACT_URL`,
  `EDEN_ARTIFACT_PATH_ROOT`, `EDEN_READONLY_STORE_URL` into
  the child env.
- `compose.yaml` mounts `eden-artifacts-data:ro` on
  task-store-server + declares a new `eden-ideator-repo`
  volume + wires `EDEN_READONLY_PASSWORD` /
  `EDEN_READONLY_STORE_URL` env vars where appropriate.
- `setup-experiment.sh` generates + preserves
  `EDEN_READONLY_PASSWORD`, writes the rendered
  `EDEN_READONLY_STORE_URL` into `.env`.
- Tests for the route (auth, traversal, missing, streaming),
  for the ideator clone-on-startup wiring, for
  `ensure_readonly_role` idempotency, for the
  subprocess-env-var threading, for the binding doc.
- New operator docs:
  `docs/operations/agent-substrate-access.md` (how-to) +
  `docs/operations/agent-readonly-db.md` (readonly-DSN
  schema reference).
- AGENTS.md "Current phase" entry.

Out:

- See §1.4 above (executor-agent access; per-worker Gitea
  tokens; managed Postgres; full blob backend; spec
  amendments).
- Conformance scenarios. The new surface is reference-only
  (`/_reference/`); chapter 9 §6 specifies the IUT contract
  is the chapter-7 binding, which this chunk does NOT touch.
- The web-ui's existing `/artifacts?uri=…` route is
  unchanged (browser-visible, session-auth-gated; different
  trust boundary than the bearer-gated agent route).

## 5. Files to touch

### 5.1 Spec / docs

| File | Change |
|---|---|
| `spec/v0/reference-bindings/worker-host-subprocess.md` | Extend §1.1 env table; add §9 substrate-access section. |
| `docs/glossary.md` | (Audit) — if "substrate" needs an entry, add it under "Operational concepts". Likely no change. |
| `docs/operations/agent-substrate-access.md` | **NEW**: operator how-to for writing an agentic ideator/evaluator that uses the three substrates. Cross-machine setup section. |
| `docs/operations/agent-readonly-db.md` | **NEW**: schema reference for the readonly Postgres role — full granted-table list (`experiment`, `task`, `submission`, `idea`, `variant`, `event`, `worker_group`, `group_membership`, `schema_version`) plus the column-projection on `worker(worker_id, data)`; JSON column shapes (each table's `data` column is `text` of JSON); worked example queries including the explicit-projection requirement for the `worker` table (`SELECT *` fails — see §D.3.a). |
| `AGENTS.md` | New "Phase 12a chunk 1f complete" paragraph; new entry to "Commands" if applicable. |
| `docs/roadmap.md` | Mark 12a-1f shipped (one-line delta). |

### 5.2 `eden-wire`

| File | Change |
|---|---|
| `reference/packages/eden-wire/src/eden_wire/server.py` | `make_app` gains an `artifacts_dir: Path \| None = None` parameter. **Always mounts** `GET /_reference/experiments/{experiment_id}/artifacts/{path:path}` with route-level auth + descriptor-relative component walk + safe-delivery headers (§D.2.a/c). When `artifacts_dir is None` the route returns 503 `eden://reference-error/artifact-serving-disabled`. |
| `reference/packages/eden-wire/tests/test_artifact_route.py` | **NEW**: unit tests for the route — auth-required, admin OR worker accepted, traversal, missing, streaming. |

### 5.3 `eden-task-store-server`

| File | Change |
|---|---|
| `reference/services/task-store-server/src/eden_task_store_server/cli.py` | New `--artifacts-dir` (default None) + `--readonly-password` (default None; reads `EDEN_READONLY_PASSWORD` env) flags. |
| `reference/services/task-store-server/src/eden_task_store_server/app.py` | `build_app` forwards `artifacts_dir` to `make_app`. Startup wires `ensure_readonly_role` when both backend is Postgres and `--readonly-password` is set. |
| `reference/services/task-store-server/tests/test_artifacts_cli.py` | **NEW**: `--artifacts-dir` flag → app exposes route; default `None` → route returns 503; basename collision check. |
| `reference/services/task-store-server/tests/test_readonly_provisioning.py` | **NEW**: `ensure_readonly_role` is idempotent; password rotation; SQLite/in-memory no-op warns. |

### 5.4 `eden-storage`

| File | Change |
|---|---|
| `reference/packages/eden-storage/src/eden_storage/postgres.py` | New module-level `ensure_readonly_role(conn, *, username, password)` function. Idempotent: creates or rotates the role; **REVOKEs all prior privileges** (including any prior `ALTER DEFAULT PRIVILEGES`-installed grants); then re-GRANTs the exact safe-set per §D.3.a (table-level on `experiment`, `task`, `submission`, `idea`, `variant`, `event`, `worker_group`, `group_membership`, `schema_version`; column-level on `worker` exposing `worker_id` + `data` but excluding `credential_hash`). No `ALTER DEFAULT PRIVILEGES` is installed — future schema bumps that add a new table must extend the GRANT list explicitly. |
| `reference/packages/eden-storage/tests/test_postgres_readonly.py` | **NEW**: requires `EDEN_TEST_POSTGRES_DSN`; provisions the role; asserts SELECT works, INSERT/UPDATE/DELETE fail; default-privileges applied to a freshly-created table. |

### 5.5 `eden-ideator-host`

| File | Change |
|---|---|
| `reference/services/ideator/src/eden_ideator_host/cli.py` | New `--repo-path`, `--gitea-url`, `--credential-helper` flags. When all three are set: clone-on-startup + fetch-on-restart wiring (mirror executor/evaluator). |
| `reference/services/ideator/src/eden_ideator_host/host.py` | Pass `repo_dir` into `build_subprocess_config`. |
| `reference/services/ideator/src/eden_ideator_host/subprocess_mode.py` | Subprocess env-var threading for the four new vars (`EDEN_REPO_DIR`, `EDEN_ARTIFACT_URL`, `EDEN_ARTIFACT_PATH_ROOT`, `EDEN_READONLY_STORE_URL`). |
| `reference/services/ideator/tests/test_ideator_repo_init.py` | **NEW**: clone-on-startup + fetch-on-restart, mirroring evaluator's test if one exists. |
| `reference/services/ideator/tests/test_ideator_subprocess_env.py` | **NEW**: subprocess gets the four env vars when CLI flags are set; absent when flags omitted. |

### 5.6 `eden-evaluator-host`

| File | Change |
|---|---|
| `reference/services/evaluator/src/eden_evaluator_host/cli.py` | New `--artifact-url`, `--artifact-path-root`, `--readonly-store-url` flags (mirroring the ideator additions). |
| `reference/services/evaluator/src/eden_evaluator_host/subprocess_mode.py` | Subprocess env-var threading for the four new vars (`EDEN_REPO_DIR` is already conceptually adjacent via the existing `--repo-path`; this chunk threads it through). |
| `reference/services/evaluator/tests/test_evaluator_subprocess_env.py` | **NEW**: subprocess gets the four env vars when CLI flags are set. |

### 5.7 `eden-service-common`

| File | Change |
|---|---|
| `reference/services/_common/src/eden_service_common/cli.py` | New `add_substrate_arguments(parser)` helper (parallels `add_exec_arguments`) that registers `--artifact-url`, `--artifact-path-root`, `--readonly-store-url`. Reused by ideator + evaluator. |
| `reference/services/_common/tests/test_substrate_arguments.py` | **NEW**: defaults + env-var fall-through (`EDEN_ARTIFACT_URL`, `EDEN_READONLY_STORE_URL`, `EDEN_ARTIFACT_PATH_ROOT`). |

### 5.8 Compose / setup

| File | Change |
|---|---|
| `reference/compose/compose.yaml` | (a) Add `EDEN_READONLY_PASSWORD` env var to task-store-server. (b) Mount `eden-artifacts-data:/var/lib/eden/artifacts:ro` on task-store-server. (c) Add `--artifacts-dir /var/lib/eden/artifacts` to task-store-server CLI. (d) Add `--repo-path /var/lib/eden/repo --gitea-url ${GITEA_REMOTE_URL} --credential-helper /etc/eden/credential-helper.sh` + new volume mounts to ideator-host. (e) Pass `EDEN_ARTIFACT_URL`, `EDEN_ARTIFACT_PATH_ROOT`, `EDEN_READONLY_STORE_URL` to ideator + evaluator. (f) Declare new `eden-ideator-repo` volume. |
| `reference/compose/compose.subprocess.yaml` | Mirror the ideator + evaluator additions for the subprocess overlay; the env vars + repo mount are needed only when running the agent-shaped subprocess workers, but the overlay layered on top is the right home. |
| `reference/compose/compose.docker-exec.yaml` | If forwarding `EDEN_REPO_DIR` to spawned children through `--exec-volume`, declare the new repo-volume forward for ideator + evaluator. Audit needed. |
| `reference/scripts/setup-experiment/setup-experiment.sh` | (a) Generate + preserve `EDEN_READONLY_PASSWORD`. (b) Compute + write `EDEN_READONLY_STORE_URL` to `.env`. (c) Write `EDEN_ARTIFACT_URL` (in-network compose default) + `EDEN_ARTIFACT_PATH_ROOT` to `.env`. |
| `reference/compose/healthcheck/smoke.sh` | **Unchanged** — see §6.6: the scripted ideator the base smoke runs doesn't write real artifacts, so the route-smoke would have nothing to fetch. |
| `reference/compose/healthcheck/smoke-subprocess.sh` | Add three assertions per §6.6: authenticated GET against `/_reference/experiments/<id>/artifacts/<path>` after quiescence; `psql` smoke against `EDEN_READONLY_STORE_URL` confirming SELECT works + INSERT fails; `docker compose exec eden-ideator-host git -C /var/lib/eden/repo log` non-empty. |

### 5.9 Conformance suite

**No changes.** The new surface is reference-only
(`/_reference/`); chapter 9 §6 specifies the IUT contract is
the chapter-7 binding, which this chunk does NOT touch.

## 6. Test design

### 6.1 Artifact-route correctness (load-bearing)

The route is the highest-risk piece — it serves arbitrary
file bytes from a path the network can supply. The
trust-boundary tests are the gate:

1. **Auth-first.** Request without `Authorization` header
   → 401 BEFORE any filesystem call. Verified by
   monkey-patching the actual filesystem entry points the
   §D.2.c handler uses (`_open_artifact_fd`, `os.open`,
   `os.fstat`) to raise, AND asserting NONE of the patches
   are ever invoked on an unauthenticated request. The
   post-round-2 component-walk implementation no longer
   calls `Path.resolve`, so a `Path.resolve`-only sentinel
   would silently pass even if the handler regressed to a
   pre-auth `os.open`; sentineling the actual entry points
   closes that gap.
2. **Admin OR worker accepted.** `Bearer admin:<token>`
   succeeds; `Bearer <worker_id>:<credential>` succeeds.
3. **Bad bearer.** Malformed scheme, missing colon, wrong
   secret, unknown worker → 401.
4. **Traversal taxonomy.** Two distinct shapes:
   - **Malformed-path** (rejected by the component-walk's
     `_REJECT_COMPONENTS` guard BEFORE any filesystem
     call): path containing `..` (`..%2Ffoo`), path with
     empty leading component (URL-encoded leading slash —
     `%2Fetc%2Fpasswd`), path with empty trailing
     component, NUL byte. ALL → 400
     `eden://reference-error/invalid-path`.
   - **Symlink-out-of-root**: a real symlink in the root
     pointing at `/etc` → 403 `eden://error/forbidden`
     (ELOOP from `os.open(... O_NOFOLLOW)`).
   Codex round-3 finding: the two shapes use DIFFERENT
   status codes; the round-2 plan conflated them. The
   handler is consistent — malformed path components are
   pre-FS-call 400s, symlink hits are post-open 403s.
5. **Missing.** Path under root that doesn't exist → 404.
6. **Non-file.** Path is a directory, FIFO, socket → 404
   (not 403; the inode shape isn't a security concern, it's
   a "this isn't what you asked for" concern).
7. **Body delivery for files at the cap boundary.** File
   of exactly 1 MiB → 200; assert the response is
   delivered as a single contiguous body and bytes match.
8. **1 MiB cap.** File of 1 MiB + 1 byte → 413
   `eden://reference-error/artifact-too-large`; assert NO response
   bytes from the file are written (the cap check runs
   before the `FileResponse`). Verified by inspecting the
   response content-length / body.
9. **Auth-disabled posture.** When task-store-server is
   started without `--admin-token` (test/in-process
   posture), the route serves without auth. Same posture
   the rest of the wire takes (auth-disabled mode is a
   test convenience; spec-conformant deployments enable
   auth).
10. **Route returns 503 when `--artifacts-dir` not set.**
    The route is always mounted; without `--artifacts-dir`
    every request returns 503
    `eden://reference-error/artifact-serving-disabled`. Verifies the
    deployment-opt-in story.
11. **Experiment-id mismatch.** URL `experiment_id` ≠
    `store.experiment_id` → 400
    `eden://error/experiment-id-mismatch`. Parallel to the
    chapter-7 §1.3 invariant on the normative endpoints.
12. **NUL-byte in path.** Path contains `%00` after URL
    decoding → 400 `eden://reference-error/invalid-path`.
    Codex round-1 (§D.2.c try/except on `ValueError`).
13. **Symlink loop.** Create symlinks inside `artifacts_dir`
    that form a loop (`a → b`, `b → a`). Request the path
    `a` → 403 `eden://error/forbidden` (the
    `O_NOFOLLOW` on `os.open(a, ...)` returns ELOOP
    because `a` IS a symlink — same code path as any
    other symlink hit, regardless of whether following
    the link would cycle or just escape). Codex round-3
    finding: all `O_NOFOLLOW`-blocked cases collapse into
    one status (403); the round-2 plan's "400 from
    resolve / 403 from O_NOFOLLOW" branching was a
    leftover from the pre-component-walk design.
14. **Permission-denied intermediate.** Create a subdir
    under the root with mode `0` (no traverse permission)
    plus a file inside it. Request the file's path → 404
    (the user-agent can't distinguish "permission denied
    intermediate" from "not found"; we collapse both to
    avoid leaking filesystem layout).
15. **TOCTOU terminal-component swap.** Pre-create the
    target file (in-root, 100 bytes). In a forked thread,
    keep `rename()`-ing the file in-place to a same-path
    decoy of size 2 MiB. In the main thread, fire 50
    concurrent GETs. Each response MUST EITHER be
    200-with-100-bytes-and-safe-headers (the inode the
    component walk locked) OR one of {404, 413} (the
    rename made the terminal component miss / become an
    oversized file). It MUST NEVER be a 200 carrying
    2-MiB-bytes — the open-fd-and-fstat-the-fd posture
    prevents the size cap from being bypassed by a
    swap-after-check race. The response MUST NEVER be 403
    on a benign rename; 403 is reserved for the symlink
    case alone.
16. **Symlink-out-of-root rejected by `O_NOFOLLOW`.**
    Create a symlink inside `artifacts_dir` pointing at
    `/etc/passwd`. Request that path. `os.open` with
    `O_NOFOLLOW` raises `OSError(errno=ELOOP)` because the
    trailing component IS a symlink. Handler maps ELOOP →
    403 `eden://error/forbidden`. The response body MUST
    NOT include any bytes from `/etc/passwd`.
17. **Intermediate-component symlink swap (TOCTOU).**
    Pre-create `<root>/dir1/file.md`. In a forked thread,
    keep swapping `<root>/dir1` between a real directory
    containing `file.md` and a symlink to `/etc`; in the
    main thread, fire 50 concurrent GETs to `dir1/file.md`.
    Each response MUST EITHER be 200-with-`file.md`-bytes
    OR a 4xx (404 / 403 / 400). It MUST NEVER serve any
    bytes from outside `<root>`. The component-walk-with-
    dir_fd posture in §D.2.c rejects the symlink form via
    `O_NOFOLLOW` on every intermediate component.
18. **Safe-delivery headers on 200.** For each of (a) a
    typical artifact (~10 KB markdown), (b) the cap-
    boundary file (exactly 1 MiB), (c) an artifact with a
    `.html` extension, (d) an artifact with a `.svg`
    extension: assert the 200 response carries
    `Content-Type: application/octet-stream`,
    `Content-Disposition: attachment; filename="<basename>"`,
    and `X-Content-Type-Options: nosniff`. Codex round-2
    finding: the safe-delivery headers are the load-bearing
    stored-XSS defense; they MUST be locked by test on
    every 200 path.

### 6.2 Path-resolution edge cases

- Empty path component (`GET /_reference/artifacts/`) → 404
  (not a file).
- URL-encoded slashes (`%2F`) — FastAPI's `{path:path}`
  treats these as literal slashes; the `.resolve()` collapses
  them; containment check catches escapes.
- Trailing slash (`foo.md/`) → 404.
- Multiple consecutive slashes (`foo//bar`) — collapsed by
  `.resolve()`; if the resolved path is under root and a
  file, served.
- Symlink to a directory inside the root → 404 (not a file).

### 6.3 Ideator clone-on-startup

- First-run: no `--repo-path` dir → host clones bare from
  `--gitea-url` using `--credential-helper`. Bare repo
  exists after startup; has the seed commit.
- Restart: `--repo-path` already exists → host runs
  `git fetch --prune origin '+refs/heads/*:refs/heads/*'`.
  Verified by pre-creating a stale local branch the remote
  doesn't have; after startup the stale branch is gone.
- `--gitea-url` unreachable at startup → host exits
  non-zero (mirrors executor/evaluator posture so compose's
  `restart: on-failure` retries).

### 6.4 Subprocess env-var threading

For each of ideator + evaluator subprocess mode:

- All four new env vars (`EDEN_REPO_DIR`, `EDEN_ARTIFACT_URL`,
  `EDEN_ARTIFACT_PATH_ROOT`, `EDEN_READONLY_STORE_URL`) are
  present in the spawned subprocess's `os.environ` when the
  CLI flags are set.
- Vars are absent when the corresponding flags are omitted
  (so a deployment that doesn't opt in doesn't get the env
  var set to an empty string — which would be different
  from "unset" for some Python `os.environ.get` callers).
- DooD mode (chunk-10d-followup-A `--exec-mode docker`):
  **deferred — see §8.10**. The DooD wrap forwards env
  KEYS only (the values come from the worker host's env at
  spawn time), but a sibling container started by the host
  docker daemon is NOT attached to the compose project
  network by default, so the in-network hostnames
  (`task-store-server:8080`, `postgres:5432`) do not
  resolve. The substrate env vars are wired for **host-mode
  subprocess only** in this chunk; DooD-mode forwarding
  needs `--network` plumbing through `wrap_command`, which
  is its own design + test surface and is explicitly
  out-of-scope here.

### 6.5 Readonly role provisioning (Postgres-only)

Gated on `EDEN_TEST_POSTGRES_DSN`:

- **First-run.** Role doesn't exist → `ensure_readonly_role`
  creates it + REVOKEs + GRANTs the safe-set.
  `eden_readonly` can connect and SELECT projected columns
  from `worker`.
- **Idempotent re-run.** Same password → no-op (no error).
- **Password rotation.** Different password → ALTER ROLE
  updates; new password authenticates; old password fails
  401.
- **No write privilege.** INSERT / UPDATE / DELETE on every
  granted table (`experiment`, `task`, `submission`, `idea`,
  `variant`, `event`, `worker`, `worker_group`,
  `group_membership`, `schema_version`) MUST fail with
  permission denied.
- **No schema-DDL privilege.** CREATE TABLE / DROP TABLE /
  ALTER TABLE MUST fail.
- **Column exclusion (load-bearing).** Three explicit
  query-shape tests against the `worker` table:
  - `SELECT credential_hash FROM worker LIMIT 1` —
    MUST fail (permission denied for column).
  - `SELECT * FROM worker LIMIT 1` — MUST fail (parser
    expands `*` to every column including the excluded
    one). Codex round-2 finding: this is the realistic
    operator-error shape; the test pins the failure.
  - `SELECT worker_id, labels FROM worker LIMIT 1` —
    MUST succeed (explicit column projection).
- **Hardening against legacy over-grant.** Start a fresh
  Postgres database; manually pre-create `eden_readonly`
  with `GRANT SELECT ON ALL TABLES IN SCHEMA public` +
  `ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT
  ON TABLES TO eden_readonly` (a hypothetical legacy
  installation's posture). Then run `ensure_readonly_role`.
  Post-condition: `SELECT credential_hash FROM
  worker` MUST fail (the prior table-wide grant has been
  REVOKEd before the column-level GRANT was applied).
  Codex round-2 finding: this is the migration test that
  proves the REVOKE-then-GRANT order works.
- **Default-privileges removal.** As `eden` superuser,
  create a new table in `public`. `eden_readonly` MUST NOT
  automatically have SELECT on it (no `ALTER DEFAULT
  PRIVILEGES` is installed). The test asserts the
  permission-denied response on `SELECT * FROM
  test_new_table` from `eden_readonly`. Codex round-2
  finding: this is the dual of the legacy-over-grant test;
  both must pass for the safety posture to hold.

### 6.6 End-to-end smoke

The compose smoke scripts gain three additions:

1. **Artifact-route smoke** (`smoke-subprocess.sh` ONLY).
   The base `smoke.sh` runs the **scripted** ideator,
   which emits fake `file:///tmp/...` URIs and never
   writes anything to the artifacts volume — the route
   would have no real artifact to return. The
   subprocess-mode ideator fixture at
   [`tests/fixtures/experiment/ideation.py`](../../tests/fixtures/experiment/ideation.py)
   writes real content to the artifacts dir, so the smoke
   assertion belongs there. After orchestrator
   quiescence: list `<artifacts-dir>/ideas/` from inside
   the task-store-server container to pick one known
   path, then `curl
   http://task-store-server:8080/_reference/experiments/<id>/artifacts/<path>`
   with the admin bearer + assert 200 + non-empty body.
2. **Readonly-DSN smoke** (`smoke-subprocess.sh`). Codex
   round-1 finding: `psql` is NOT installed in
   `eden-reference:dev` (only `git` / `ca-certificates` /
   `curl`). Run psql from inside the **postgres**
   container, which has it natively:

   ```bash
   docker compose --env-file "$ENV_FILE" exec -T postgres \
       psql "$EDEN_READONLY_STORE_URL" \
       -c 'SELECT COUNT(*) FROM event' \
       # ≥ N after quiescence
   docker compose --env-file "$ENV_FILE" exec -T postgres \
       psql "$EDEN_READONLY_STORE_URL" \
       -c "INSERT INTO event(data) VALUES ('{}')" \
       # MUST fail with permission denied
   docker compose --env-file "$ENV_FILE" exec -T postgres \
       psql "$EDEN_READONLY_STORE_URL" \
       -c 'SELECT credential_hash FROM worker LIMIT 1' \
       # MUST fail (column-level grant excludes it)
   ```

   Third assertion is load-bearing: it verifies the
   column-level GRANT (§D.3.a) is actually in effect, not
   just declared. The DSN is read from the host-side
   `.env` and passed in; `psql` resolves the in-network
   `postgres:5432` from inside the postgres container the
   same way the other services do.
3. **Ideator git-substrate smoke** (`smoke-subprocess.sh`):
   post-quiescence, exec into `eden-ideator-host` and run
   `git -C /var/lib/eden/repo log --oneline | wc -l` —
   assert ≥ N. (Subprocess-only because the agentic
   ideator that uses the substrate is the subprocess
   fixture; the scripted ideator never reads git.)

Each addition is one extra `curl` / `docker compose exec`
call; minimal smoke-time increase. The base `smoke.sh`
covers only the existing post-quiescence event-log /
variant-count invariants.

### 6.7 Binding-doc test

A docs-side test (mirroring the existing spec-xref-check
posture if applicable) confirms the four new env vars are
documented in the binding's §1.1 table. Mechanical, but
catches the "added env var, forgot to document it" footgun.

## 7. Verification gates

The chunk is mergeable when all of the following pass
(literal canonical commands from AGENTS.md "Commands"):

1. `uv sync` succeeds.
2. `uv run ruff check .` clean.
3. `uv run pyright` clean.
4. `uv run pytest -q` (full suite) green.
5. `uv run pytest -q conformance/` green (no new
   scenarios, no regressions).
6. `uv run python conformance/src/conformance/tools/check_citations.py` clean.
7. `npx --yes markdownlint-cli2@0.14.0 "**/*.md" "#node_modules" "#.venv" "#docs/archive/**" "#docs/plans/review/**"` clean.
8. `pipx run 'check-jsonschema==0.29.4' --check-metaschema spec/v0/schemas/*.schema.json` clean.
9. `python3 scripts/spec-xref-check.py` clean.
10. `python3 scripts/check-rename-discipline.py` clean.
11. `bash reference/compose/healthcheck/smoke.sh` green.
12. `bash reference/compose/healthcheck/smoke-subprocess.sh` green.
13. `bash reference/compose/healthcheck/e2e.sh` green.
14. `EDEN_TEST_POSTGRES_DSN=postgresql://… uv run pytest -q reference/packages/eden-storage/tests/test_postgres_readonly.py` green (locally; CI's `python-test-postgres` job picks this up automatically).

The smokes will need updating to include the three new
substrate-route assertions (§6.6); those aren't gates beyond
the smokes' existing pass/fail.

## 8. Tricky areas

### 8.1 `/_reference/` route auth bypass

The middleware at
[`reference/packages/eden-wire/src/eden_wire/auth.py`](../../reference/packages/eden-wire/src/eden_wire/auth.py)
line ~160 short-circuits all `/_reference/` paths to
**unauthenticated** by default. The new artifact route
explicitly opts back in — calling `authenticate(...)` from
inside the handler. The implementation MUST do the auth
call BEFORE any path inspection (per §D.2.c) so an unauth
caller can't extract existence-of-files via timing.

A subtle alternative — modifying the middleware to take a
list of "force-auth" reference paths — was considered and
rejected: it pushes the auth posture out of the route's
own module, making future maintainers more likely to miss
the requirement.

### 8.2 FileResponse vs StreamingResponse — why the 1 MiB cap

The 1 MiB cap (§D.2.a) mirrors the existing
[`_read_inline_artifact`](../../reference/services/web-ui/src/eden_web_ui/routes/_helpers.py)
helper. The cap is **mandatory in this chunk** because:

- `FileResponse` does not stream by default in FastAPI; it
  loads the full file into memory.
- Today's idea content markdown is at most a few KB; no
  legitimate artifact is anywhere near 1 MiB. The cap is a
  conservative safety bound, not a performance limit.
- Adding `StreamingResponse` + chunked-read here would
  bloat the ~50-100 LOC budget and duplicate logic Phase
  13d's `Backend` abstraction is going to ship anyway.

If an operator's artifact production legitimately generates
files larger than 1 MiB, the route returns 413 (§D.2.a) and
the answer is to wait for 13d (or stand up the operator's
own static file server alongside; the agent's
`EDEN_ARTIFACT_URL` can point anywhere).

### 8.3 `EDEN_ARTIFACT_PATH_ROOT` divergence between worker host and task-store-server

In compose, the worker-host containers and the task-store-server
container BOTH mount `eden-artifacts-data` at
`/var/lib/eden/artifacts`. The path is stable across
containers. But there's no protocol-level guarantee that an
operator's chosen deployment uses the same mount path; if
the worker host mounts at `/var/lib/eden/artifacts` and the
task-store-server mounts at `/srv/eden-data`, the agent's
`EDEN_ARTIFACT_PATH_ROOT=/var/lib/eden/artifacts` will mean
the agent strips the wrong prefix when translating a
`file://` URI received from the wire.

For 12a-1f the resolution is: **the worker host's
`EDEN_ARTIFACT_PATH_ROOT` must equal the
task-store-server's `--artifacts-dir`**. Compose enforces
this by construction (both are bind-mounted from the same
volume, at the same path). The binding doc documents the
invariant. Off-host operators are responsible for choosing
consistent paths.

### 8.4 Cross-machine off-host posture

The chunk's load-bearing decision is cross-machine
support. The compose-internal defaults (`postgres:5432`,
`task-store-server:8080`) only resolve inside the compose
network. Off-host operators need to:

- Substitute `EDEN_ARTIFACT_URL` to a hostname the agent's
  network can reach.
- Substitute `EDEN_READONLY_STORE_URL` similarly.
- Provide their own Gitea credentials + URL for off-host
  git clones (Phase 13e formalizes per-worker tokens).

The binding doc has a "Cross-machine setup" §; the
operator how-to doc gives a worked example. **No code in
this chunk substitutes for off-host hostname resolution**
— the env-var threading is the entire mechanism.

### 8.5 `EDEN_READONLY_PASSWORD` rotation semantics

Re-running setup-experiment.sh PRESERVES `EDEN_READONLY_PASSWORD`
across runs (same posture as the other secrets). The
operator can force rotation by deleting the line from
`.env` and re-running. The task-store-server's
`ensure_readonly_role` runs `ALTER ROLE … WITH PASSWORD …`
on every startup; if `EDEN_READONLY_PASSWORD` was rotated
but the existing subprocesses are still using the old DSN,
they will start failing on connect.

Recovery: restart the worker hosts (`docker compose restart
ideator-host evaluator-host`) so the subprocesses pick up
the new DSN from `.env`.

**Note this is a known operational sharp edge**, not a
bug. The 13c managed-Postgres plan handles password
rotation via a chart-managed Secret with a single source
of truth.

### 8.6 `--db-path` deprecation interaction

[`reference/services/task-store-server/src/eden_task_store_server/cli.py`](../../reference/services/task-store-server/src/eden_task_store_server/cli.py)
still carries a deprecated `--db-path` alias for
`--store-url`. The new `--readonly-password` flag is
Postgres-specific; if an operator runs with `--db-path
<sqlite-file>` AND `--readonly-password <pwd>`, the CLI
MUST warn (the readonly role is meaningless against
SQLite) and continue.

### 8.7 Test-file basename uniqueness

Per the AGENTS.md guidance on tests-dir layout, new test
file basenames MUST be unique across the whole `testpaths`
set. Before merge, run `find reference -name 'test_*.py'
-exec basename {} \;` and confirm no collision. The
candidate names in §5 (`test_artifact_route.py`,
`test_artifacts_cli.py`, `test_readonly_provisioning.py`,
`test_postgres_readonly.py`, `test_ideator_repo_init.py`,
`test_ideator_subprocess_env.py`,
`test_evaluator_subprocess_env.py`,
`test_substrate_arguments.py`) all look novel; verify
during impl.

### 8.8 Interaction with in-flight PRs

- **PR #79 (12a-1b worker+group admin UI plan)** and
  **PR #81 (12a-1d rationale→content rename impl)** both
  merged into `main` while this plan was being drafted.
  This plan reflects the post-rename vocabulary (`content`
  not `rationale`) and the worker+group admin UI plan's
  existence is acknowledged but not depended on (that
  chunk is plan-only).
- **PR #80 (12a-2 orchestrator-as-role impl).** Touches
  orchestrator + admin-UI surfaces; may touch the auth
  middleware. 12a-1f's `/_reference/artifacts/` route
  lives in `eden_wire.server` and uses route-level auth
  (not the middleware); merge conflict surface is small.
  Rebase before push.

When starting impl: `git fetch origin && git rebase
origin/main` first; identify any newly-landed PRs and
adapt.

### 8.9 DooD-mode substrate env-var forwarding is deferred

The chunk-10d-followup-A DooD path
([`reference/services/_common/src/eden_service_common/container_exec.py`](../../reference/services/_common/src/eden_service_common/container_exec.py))
spawns each `*_command` inside a sibling docker container
that the host docker daemon starts. That sibling container
is **not attached to the compose project network by
default**, so the substrate URLs the host bakes into env
(`http://task-store-server:8080/...`,
`postgresql://...@postgres:5432/eden`) do not resolve from
inside the sibling.

This chunk **deliberately scopes** DooD-mode substrate
access **out**:

- Host-mode subprocess (the default for the subprocess
  overlay) gets all four new env vars wired through.
- `--exec-mode docker` keeps the existing env-key
  forwarding (the keys list is unchanged), but a sibling
  container won't be able to reach the substrate URLs
  without additional `--network` plumbing in
  `wrap_command`.

The right shape for the DooD networking design is a
separate sub-chunk (call it 12a-1f-followup-A) that:

- Adds a `--network` parameter to `wrap_command` (or
  reads `EDEN_COMPOSE_NETWORK` from setup-experiment),
- Defaults it to the compose project network name
  (`eden-reference_default` in the reference deployment),
- Adds an integration test that spawns a sibling
  container and confirms `task-store-server:8080`
  resolves.

For 12a-1f, the AGENTS.md "Current phase" entry and the
binding doc explicitly note the host-mode-only scope.
Operators who need DooD-mode substrate access wait for
the follow-up.

### 8.10 Cross-machine agent connecting via the gitea remote URL

The on-host agent uses `EDEN_REPO_DIR` (local bare clone).
The off-host agent connects to Gitea over HTTP via the
operator's gitea hostname. Today's shared `eden` HTTP-Basic
credential (Phase 10d-followup-B) works for both: the
in-stack helper script and the off-host operator-supplied
credentials both speak the same auth. Phase 13e replaces
this with per-worker tokens.

The binding doc's §9 mentions this explicitly and points
at 13e for the hardened path.

## 9. Risks / things to watch

- **Bypass of bearer-auth via timing-based existence-leak.**
  The route MUST run auth FIRST. Verified by the §6.1
  test #1.
- **Path traversal via URL encoding.** FastAPI's
  `{path:path}` handles URL-decoded paths correctly, and
  `Path.resolve()` normalizes `..`. The containment check
  via `relative_to` (raising `ValueError` on escape) is
  the canonical Python idiom. The §6.1 test #4 covers all
  three escape shapes.
- **Symlink escapes.** A symlink inside the artifacts root
  pointing OUTSIDE the root → `.resolve()` follows it → the
  resolved path is outside the root → containment check
  rejects. The §6.1 test #4 covers this.
- **Race between readonly-role rotation and live
  subprocesses.** §8.5 above; operational, not a bug.
- **Off-host operator misconfigures `EDEN_ARTIFACT_PATH_ROOT`.**
  Soft failure: the subprocess sends a wrong path component
  → 404 from the route. The operator notices in their
  agent's diagnostic output. No data leakage.
- **Postgres readonly role can see attribution worker_ids.**
  By design — the readonly role serves the exploratory-read
  use case which needs `submitted_by` / `executed_by` /
  `evaluated_by` (chapter 02 §3.1 / §5.1 / §9). Operators
  who don't want this surface should NOT enable the readonly
  substrate. The binding doc documents this explicitly.
- **Artifact-server bloat across many small files.** The
  ~50-100 LOC budget assumes basic FileResponse-shaped
  serving. If 12a-1f grows additional features (caching,
  range requests, ETags) it's a sign 13d's `Backend`
  abstraction should land first.

## 10. Sequence within the chunk

Single PR. Internal ordering:

1. **Spec / binding doc first.** Extend
   [`spec/v0/reference-bindings/worker-host-subprocess.md`](../../spec/v0/reference-bindings/worker-host-subprocess.md)
   §1.1 + new §9. Get the contract right before code.
2. **`eden_wire` route + tests.** Auth dispatch is the
   load-bearing piece; nail it with unit tests in
   isolation (no compose).
3. **`eden-storage` `ensure_readonly_role` + tests.**
   Idempotency + GRANT correctness; Postgres-only.
4. **`eden-task-store-server` CLI wiring.** New flags
   thread through; route mounts conditionally; readonly
   provisioning runs conditionally.
5. **`eden-ideator-host` git substrate.** Clone-on-startup
   wiring; mirror executor/evaluator.
6. **`eden-service-common` shared substrate flags.**
   `add_substrate_arguments` + tests.
7. **Subprocess env-var threading.** Ideator + evaluator
   subprocess modes; DooD path.
8. **Compose + setup-experiment.** Volume mounts, env-var
   plumbing, secret generation, helper script paths.
9. **Smokes.** Add the three substrate assertions to
   smoke.sh / smoke-subprocess.sh.
10. **Operator docs.** `agent-substrate-access.md` +
    `agent-readonly-db.md`. AGENTS.md "Current phase".

An agent running this chunk should expect tests to go red
around step 2 and come back green around step 8.

## 11. Out of scope (followups)

- **Executor-agent substrate access.** Operator decision;
  separate chunk if needed.
- **DooD-mode substrate env-var forwarding.** §8.9.
  Sub-chunk after 12a-1f lands (the wrap needs
  `--network` plumbing and its own integration test).
- **Per-worker Gitea tokens with branch ACLs.** Phase 13e.
- **Managed Postgres + readonly role through chart Secret.**
  Phase 13c.
- **`Backend` Protocol + `LocalFsBackend` / `S3Backend` /
  `GcsBackend` replacing the tactical route.** Phase 13d
  (the 12a-1f route is **throwaway** by design).
- **SSE / WebSocket push for the event log (a
  Postgres-side alternative to `subscribe`).** Out — the
  readonly DSN serves polling/aggregation; push semantics
  are chapter 7 §8's concern.
- **Caching / ETags / range requests on the artifact
  route.** Out — see §9 above.
- **Spec amendment introducing a normative substrate
  contract.** Out — the `/_reference/` namespace's
  informative posture is correct for 12a-1f.

## 12. Estimated effort

- **Spec / binding-doc prose**: ~0.25 day. Two new sections,
  one new env-var table.
- **`eden-wire` route + tests**: ~0.5 day. Trust-boundary
  tests are the bulk.
- **`eden-storage` readonly + tests**: ~0.5 day.
  Postgres-specific.
- **`eden-task-store-server` CLI + wiring**: ~0.25 day.
  Conditional mounting.
- **Ideator git substrate**: ~0.5 day. Mirror existing
  pattern.
- **Subprocess env-var threading**: ~0.25 day. Mechanical.
- **Compose + setup-experiment**: ~0.5 day. Secret
  generation + volume wiring + new env vars.
- **Smokes**: ~0.25 day. Three small `curl` / `psql` calls.
- **Operator docs**: ~0.5 day. Both new markdown files.
- **Codex-review iterations** (plan + impl, ~3 rounds
  each): ~0.5 day.

**Realistic total: ~3.5–4 working days** of focused work,
plus codex iteration. The chunk plan itself takes the
standard ~half-day; this document is that.
