# Phase 12a-1h — Port `eden-manual` CLI to post-12a-1 auth + land no-op variant rejection design

## 1. Context

Phase 12a-1 (PR #78, commit `84adb50`) replaced the deployment-wide
`EDEN_SHARED_TOKEN` shared-token scheme with a normative per-worker
bearer (`<principal>:<secret>`) plus an `admin` bearer for
admin-gated endpoints (`spec/v0/07-wire-protocol.md` §13). The
per-claim opaque `token` was retired; claim ownership is now
identity-keyed by `task.claim.worker_id` (`spec/v0/04-task-protocol.md`
§3.3 / §4.1).

Phase 12a-1d (PR #81) then renamed the idea body field `rationale → content`
across the spec, contracts, web-ui, and the manual-UI CLI.

The reference Compose stack, the web-ui, and every service were
ported in the 12a-1 wave; the **manual-UI CLI helper** at
[`reference/scripts/manual-ui/eden-manual`](../../reference/scripts/manual-ui/eden-manual)
was **deliberately left** as a known gap. PR #67's last commit message
flags it explicitly:

> "The eden-manual CLI script under reference/scripts/manual-ui/
> has not yet been renamed; each role skill now carries a 'CLI rename
> gap' callout."

That callout was about vocabulary (chunk 12a-1d closed the
`rationale → content` half of it). The **auth-port half** is still
open: today the CLI sends `Authorization: Bearer $EDEN_SHARED_TOKEN`
(which doesn't exist post-12a-1) and submits include the retired
per-claim `"token"` field, so every CLI-driven claim/submit fails
against a current stack. The skill docs (`.claude/skills/eden-manual-*`)
that operators read on session start still reference the same broken
env var.

A separate manual-demo session ported the CLI off pre-12a-1d main
(`/tmp/eden-handoff/demo-cli-port.diff`); that diff still uses
`rationale` and would **revert** the 12a-1d rename if applied. We
re-apply the auth-port + no-op-guard semantic changes onto current
main without touching the rename.

This chunk also lands a design doc for an executor protocol property
that the demo session surfaced and the operator wants tracked:
**a `status=success` variant submission whose tree state equals the
parent's tree is a no-op and SHOULD be rejected at the role
contract.** Issue #83 tracks the spec amendment + server-side
enforcement; this chunk lands the design doc (`docs/design/`) and a
defensive client-side guard in the CLI.

### What this chunk delivers

1. **CLI port** — `reference/scripts/manual-ui/eden-manual` works
   against a post-12a-1 stack: reads `EDEN_ADMIN_TOKEN`, manages
   per-worker credentials at `/tmp/eden-manual/.credentials.json`,
   drops the retired `"token"` body field, passes worker bearers on
   claim / submit / create_variant / mark-ready / create_idea calls.
2. **Defensive no-op variant guard** — `execution-submit` rejects
   variants whose tree equals `idea.parent_commits[0]`'s tree, with
   an inline comment pointing at the design doc.
3. **Design doc** — `docs/design/executor-no-op-variant-rejection.md`
   captures the rationale + proposed normative rule + enforcement
   options. References issue #83.
4. **Skill-doc fixes** — three skill files (`eden-manual-ideator`,
   `eden-manual-executor`, `eden-manual-evaluator`) updated to match
   the new auth shape (removing stale "token + variant_id are
   persisted" / `EDEN_SHARED_TOKEN` references).

### What this chunk does NOT do

- **No spec changes.** The no-op normative rule + closed
  error-vocabulary addition + `Store.submit` enforcement are tracked
  in issue #83 and will land in a separate chunk that also adds the
  v1+roles conformance scenario.
- **No README / CONTRIBUTING updates.** Those are touched by 12a-2
  wave 8.
- **No `MANUAL_DEMO_HANDOFF.md`.** Per operator guidance, kept out
  of main.
- **No other CLI changes.** Only `eden-manual` is in scope;
  `eden-experiment` is unaffected by 12a-1 auth (it shells through
  Compose, not the wire).

## 2. Decisions captured before drafting

1. **Don't reverse the 12a-1d rename.** Main's CLI is at `content`
   (line 372 of `reference/scripts/manual-ui/eden-manual` reads
   `raw.get("content", "")`). The demo diff was authored off
   pre-12a-1d main and would re-introduce `rationale`. We
   semantically port the demo diff, NOT textually apply it.

2. **Defense-in-depth no-op guard, not gated on spec.** The CLI
   guard lands now. The spec amendment + server-side enforcement
   lands later under issue #83. Once the store enforces the rule
   the CLI guard can be removed (one-round-trip earlier error vs.
   two-round-trip is the only difference); keeping it until then
   matches the design doc's recommendation (§Migration).

3. **Per-worker credentials at `/tmp/eden-manual/.credentials.json`,
   mode 0600.** The demo's choice. The path lives alongside the
   existing `.claims.json` under `WORK_ROOT`. We honor the
   `EDEN_MANUAL_WORK_ROOT` env override (already used for
   `.claims.json`) for symmetry, even though the demo diff hardcoded
   `/tmp/eden-manual/`. See §D.3 for the path-construction rule.

4. **Bearer is a per-call `_wire()` kwarg, default
   `admin:<EDEN_ADMIN_TOKEN>`.** Read endpoints (`list-tasks`,
   `show`, `list-commits`) keep the admin default. Mutating worker
   endpoints (`claim`, `submit`, `create_variant`, `create_idea`,
   `mark_ready`) explicitly pass `bearer=<worker_id>:<credential>`.
   This is the minimum-diff shape; an alternative — split the CLI
   into "admin-context" and "worker-context" entry points — would be
   a larger refactor and gain nothing the per-call kwarg doesn't.

5. **`_worker_bearer(env, worker_id)` helper adopts a
   register-first-then-reissue ladder.** The reference services'
   [`bootstrap_worker_credential`](../../reference/services/_common/src/eden_service_common/auth.py)
   forbids fall-through-to-register on the stale branch per spec
   §8.2 (the registry row is authoritative for a long-running
   service worker that owns its identity). The manual CLI's
   posture is different: it registers `eden-manual` **lazily** on
   first use, and the operator's normal demo workflow includes
   `setup-experiment.sh` reruns that **rebuild the worker
   registry**. After a registry rebuild, the stored worker row
   doesn't exist anymore, so `reissue_credential` would fail with
   `UnknownWorker` (per
   [`test_workers.py::test_reissue_credential_unknown_worker`](../../reference/packages/eden-storage/tests/test_workers.py)).

   The manual CLI's ladder, accordingly:

   1. **Persisted token + verify via `/whoami`.** If
      `.credentials.json` has an entry for `worker_id` and the
      bearer authenticates back as the expected `worker_id` via
      `GET /v0/experiments/{E}/whoami`, use it. **Cost:** one
      extra wire call per CLI invocation — acceptable for a manual
      operator script. **Skipped** only if no entry exists
      (cold-start path falls directly to step 3).
   2. **Persisted but stale (401 or different worker_id) → register-then-reissue.**
      The CLI doesn't know whether the row still exists. Try
      `register_worker` first under the admin bearer. If the
      response carries `registration_token`, the row didn't exist
      (registry was rebuilt) — persist the new token. If the
      response lacks `registration_token` (idempotent re-register
      hit an existing row whose token we don't have), fall through
      to `reissue_credential` and persist that.
   3. **No persisted token → register-then-reissue.** Same shape as
      step 2's recovery — register, fall through to reissue on
      idempotent-no-token.

   Steps 2 and 3 thus collapse to the same recovery helper. This
   handles the codex-flagged "rerun setup-experiment.sh →
   registry rebuilt → 401" case correctly: the whoami in step 1
   fails, step 2 registers fresh, the new token gets persisted, and
   the operator's first worker-gated call goes through.

   The deviation from the service-host §8.2 ladder is intentional
   and load-bearing: long-running services don't experience their
   own registry getting wiped under them, but a manual CLI run
   against a freshly-rebuilt stack does. The CLI follows the
   register-first posture for both no-persisted-token and stale
   branches to handle this uniformly.

   We do NOT namespace credentials by `EDEN_EXPERIMENT_ID` — the
   ladder handles cross-experiment staleness automatically and the
   credentials file stays at `WORK_ROOT/.credentials.json`.

6. **No backwards-compat shim for `EDEN_SHARED_TOKEN`.** Per the
   project-lifecycle rule ("No backwards-compatibility shims in
   greenfield / pre-external-user projects"), the env-var rename is
   a hard cut. The CLI raises a `required` error referencing
   `EDEN_ADMIN_TOKEN`; operators with stale `.env` files re-run
   `setup-experiment.sh` (which writes `EDEN_ADMIN_TOKEN` in 12a-1).

7. **No-op guard fires only on `status=success`.** A `status=error`
   submission is a failed-attempt declaration, not a candidate; the
   variant terminalizes as `error` regardless of tree state. Matches
   the design doc's §"Open questions" #2.

8. **Single-parent only in the CLI guard.** The CLI checks
   `head_tree == parent_commits[0]^{tree}`. The design doc proposes
   the normative rule cover the multi-parent case (Option A: "differ
   from at least one parent"); however, the CLI's purpose is
   defense-in-depth for the manual-demo workflow where multi-parent
   ideas are rare. The CLI does NOT iterate `parent_commits[1:]`.
   The design doc + spec amendment carry the multi-parent semantics;
   the CLI guard's behavior on a multi-parent idea is "rejects if
   the variant tree equals the first parent's tree, accepts
   otherwise" — strictly less strict than the proposed normative
   rule, which is fine for a defensive belt.

9. **Skill-doc update scope: three files.** All three role-skill
   docs contain stale per-claim-token references that this chunk
   updates:
   - `eden-manual-evaluator/SKILL.md` line 116 — `EDEN_SHARED_TOKEN`
     in the curl example. Rewrite to `admin:$EDEN_ADMIN_TOKEN`.
   - `eden-manual-ideator/SKILL.md` line 63 — "The token is persisted
     in `.claims.json`". Rewrite to reflect identity-keyed claim
     ownership.
   - `eden-manual-executor/SKILL.md` line 58 — "Token + variant_id
     are persisted to `.claims.json`". Rewrite to drop the
     per-claim-token language; `variant_id` is still persisted, only
     the token reference is wrong.

   Do NOT touch `eden-manual-experiment/SKILL.md` — it doesn't
   reference the broken surface. Do NOT reverse the 12a-1d rename
   in any skill file (main's skill files already use the canonical
   vocab).

10. **One PR per stage.** Plan PR → operator merges → reset to fresh
    main → impl PR. Standard 12a-1f / 12a-1g posture.

## 3. Design

### D.1 Auth model summary (from chapter 07 §13)

Every `/v0/` endpoint MUST carry `Authorization: Bearer
<principal>:<secret>`. The principal is either `admin` (deployment-
singleton, secret = `EDEN_ADMIN_TOKEN`) or a registered `<worker_id>`
(secret = the argon2id-hashed-then-presented `registration_token`).

Endpoint classification:

- **admin-gated**: `register_worker`, `reissue_credential`, group
  registry mutations.
- **worker-gated**: `claim`, `submit`, `whoami`, `create_idea`,
  `mark_idea_ready`, `create_variant`, plus the declare/integrate
  variant mutations. Admin bearers on these endpoints get 403
  `eden://error/forbidden` (per
  [`spec/v0/07-wire-protocol.md`](../../spec/v0/07-wire-protocol.md)
  §3 / §4, verified by
  [`test_auth.py`](../../reference/packages/eden-wire/tests/test_auth.py)).
- **either**: reads (`list_tasks`, `read_task`, `list_ideas`,
  `read_idea`, `read_variant`, `list_variants`, `list_workers`,
  `read_worker`, `read_group`, `list_groups`).

The CLI's bearer choices per endpoint:

| Endpoint | CLI usage | Bearer |
| --- | --- | --- |
| `GET /v0/experiments/{E}/tasks` (and singular reads) | `list-tasks`, `show` | admin (default) |
| `GET /v0/experiments/{E}/variants` | `list-commits` | admin (default) |
| `GET /v0/experiments/{E}/ideas/{I}` | `show`, `execution-submit` | admin (default) |
| `POST /v0/experiments/{E}/workers` | first-use registration | admin |
| `POST /v0/experiments/{E}/workers/{W}/reissue-credential` | cred-recovery | admin |
| `POST /v0/experiments/{E}/tasks/{T}/claim` | `claim` | worker |
| `POST /v0/experiments/{E}/tasks/{T}/submit` | `*-submit` | worker |
| `POST /v0/experiments/{E}/ideas` | `ideation-submit` (Phase 1) | worker |
| `POST /v0/experiments/{E}/ideas/{I}/mark-ready` | `ideation-submit` (Phase 2) | worker |
| `POST /v0/experiments/{E}/variants` | `execution-submit` (Phase 1) | worker |

The worker bearer used is always the bearer for the
`worker_id` recorded in the claim record (`_get_claim(task_id)["worker_id"]`).
For the initial `claim` call there's no recorded claim yet, so the
bearer is for the `--worker-id` argument (default `eden-manual`).

### D.2 `_wire()` signature

```python
def _wire(
    env: dict[str, str],
    path: str,
    *,
    method: str = "GET",
    body: object = None,
    bearer: str | None = None,
    swallow_401: bool = False,
):
    """Call the task-store wire API.

    The bearer defaults to `admin:<EDEN_ADMIN_TOKEN>` (spec
    chapter 07 §13). Worker-gated endpoints (claim / submit) MUST
    use a worker bearer instead — pass
    `bearer="<worker_id>:<credential>"`.

    `swallow_401`: when True, return None on HTTP 401 instead of
    sys.exit. Used by the `/whoami` verify path in
    `_worker_bearer` to fall through to the register-or-reissue
    recovery branch. All other non-2xx responses still trigger
    sys.exit.
    """
    port = env.get("TASK_STORE_HOST_PORT", "8080")
    url = f"http://localhost:{port}{path}"
    if bearer is None:
        bearer = f"admin:{env['EDEN_ADMIN_TOKEN']}"
    headers = {
        "Authorization": f"Bearer {bearer}",
        "X-Eden-Experiment-Id": env["EDEN_EXPERIMENT_ID"],
    }
    # ... body handling unchanged ...
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        if swallow_401 and e.code == 401:
            return None
        sys.exit(f"wire error {e.code} on {method} {path}: {e.read().decode()[:400]}")
    # ... rest unchanged ...
```

The `body` parameter no longer carries the retired `"token"` field
for submit calls; the submit body is now just `{"payload": {...}}`
(matches chapter 07 §4.2 post-12a-1).

### D.3 Credentials file

```python
CREDENTIALS_FILE = WORK_ROOT / ".credentials.json"  # /tmp/eden-manual/.credentials.json by default
```

Reused `WORK_ROOT = Path(os.environ.get("EDEN_MANUAL_WORK_ROOT", "/tmp/eden-manual"))`
(already defined at module top) so an operator override propagates
to the credentials file too. Mode 0600 on write. Format:

```json
{
  "eden-manual": "<registration_token_hex>"
}
```

Helpers:

```python
def _read_credentials() -> dict[str, str]:
    if not CREDENTIALS_FILE.exists():
        return {}
    return json.loads(CREDENTIALS_FILE.read_text())


def _save_credentials(creds: dict[str, str]) -> None:
    CREDENTIALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    CREDENTIALS_FILE.write_text(json.dumps(creds, indent=2) + "\n")
    CREDENTIALS_FILE.chmod(0o600)


def _worker_bearer(env: dict[str, str], worker_id: str) -> str:
    """Return `<worker_id>:<credential>` for the manual CLI ladder.

    Differs from `eden_service_common.auth.bootstrap_worker_credential`
    on the stale-credential branch: where the service-host helper
    forbids fall-through to fresh-register per spec §8.2 (the
    long-running service owns its registry row), the manual CLI
    accepts that the row may have been wiped by a
    `setup-experiment.sh` rerun and uses the
    register-then-reissue recovery for both stale and cold-start.
    """
    creds = _read_credentials()
    persisted = creds.get(worker_id)

    if persisted is not None:
        # Step 1: verify via /whoami.
        bearer = f"{worker_id}:{persisted}"
        verified_id = _try_whoami(env, bearer=bearer)
        if verified_id == worker_id:
            return bearer
        # Step 2: stale — fall through to register-then-reissue.

    # Step 3 (and stale-fall-through from step 2): register, with
    # fall-through to reissue if the row already exists.
    new_token = _register_or_reissue(env, worker_id)
    creds[worker_id] = new_token
    _save_credentials(creds)
    return f"{worker_id}:{new_token}"


def _register_or_reissue(env: dict[str, str], worker_id: str) -> str:
    """Register a worker; reissue if the row already exists."""
    resp = _wire(
        env, f"/v0/experiments/{_exp(env)}/workers",
        method="POST", body={"worker_id": worker_id},
    )
    new_token = resp.get("registration_token")
    if new_token is None:
        # Idempotent re-register hit an existing row whose token
        # we don't have locally (either we never had it, or the
        # row pre-existed our local state). Reissue to mint a
        # fresh one.
        new_token = _reissue_worker(env, worker_id)
    return new_token
```

Two helpers wrap the new wire calls:

```python
def _try_whoami(env: dict[str, str], *, bearer: str) -> str | None:
    """Return the authenticated worker_id, or None on 401."""
    resp = _wire(
        env, f"/v0/experiments/{_exp(env)}/whoami",
        bearer=bearer, swallow_401=True,
    )
    if resp is None:
        return None
    return resp.get("worker_id")


def _reissue_worker(env: dict[str, str], worker_id: str) -> str:
    resp = _wire(
        env,
        f"/v0/experiments/{_exp(env)}/workers/{worker_id}/reissue-credential",
        method="POST", body={},
    )
    return resp["registration_token"]
```

The whoami probe is the only call site that uses `swallow_401`. The
default-False keeps every other call site sys.exit-on-error.

### D.4 Claim record

Post-12a-1, the claim wire response no longer carries a `token`
field. `_record_claim` drops the `token` argument:

```python
def _record_claim(
    task_id: str,
    *,
    worker_id: str,
    variant_id: str | None = None,
) -> None:
    claims = _read_claims()
    entry: dict[str, str] = {"worker_id": worker_id}
    if variant_id is not None:
        entry["variant_id"] = variant_id
    claims[task_id] = entry
    _write_claims(claims)
```

The `.claims.json` file shape changes from
`{"task_id": {"token": "...", "worker_id": "..."}}` to
`{"task_id": {"worker_id": "..."}}`. **No back-compat shim**: an
operator with a stale `.claims.json` from a pre-12a-1 session needs
to either re-claim (which is the natural thing to do against a
restarted stack anyway) or `rm /tmp/eden-manual/.claims.json`.
Reading a stale entry with a `"token"` field still works (we just
read `worker_id`); the unused field is harmless.

### D.5 Claim body

Post-12a-1, the claim wire body shape (chapter 07 §4.1) is

```json
{"expires_at": "<iso8601>"}   // expires_at OPTIONAL
```

`worker_id` comes from the bearer's principal — NOT from the body
(spec §3.3: "The Store Protocol takes `worker_id` as input on
`claim`... it trusts the binding to have already authenticated the
caller as that worker."). The CLI's `cmd_claim` builds:

```python
bearer = _worker_bearer(env, args.worker_id)
body: dict[str, object] = {}
if args.ttl_seconds:
    body["expires_at"] = ...
_wire(env, f".../tasks/{T}/claim", method="POST", body=body, bearer=bearer)
```

### D.6 Submit body

Post-12a-1, submit wire body is

```json
{"payload": {...role-specific submission...}}
```

The retired `"token"` field is dropped from every call site:

- `cmd_ideation_submit` — both the `status=error` short-circuit and
  the success-path Phase 3 submit.
- `cmd_execution_submit` — both the `status=error` short-circuit and
  the success-path Phase 3 submit.
- `cmd_evaluation_submit` — the single submit call.

### D.7 No-op variant guard

In `cmd_execution_submit`, after reachability validation
(merge-base --is-ancestor against `idea.parent_commits`) and before
`create_variant`:

```python
# No-op guard: refuse to submit a variant whose tree is identical
# to parent_commits[0]'s tree. Catches both `sha == parent[0]`
# (literal no-op) and empty commits on top of parent (same tree,
# new SHA). The CLI does this defensively; spec-level enforcement
# is RFC'd at docs/design/executor-no-op-variant-rejection.md.
head_tree = _git(
    "rev-parse", f"{sha}^{{tree}}",
    cwd=workdir, capture=True,
).stdout.strip()
parent_tree = _git(
    "rev-parse", f"{idea['parent_commits'][0]}^{{tree}}",
    cwd=workdir, capture=True,
).stdout.strip()
if head_tree == parent_tree:
    sys.exit(
        f"error: variant tree is identical to parent_commits[0] tree "
        f"({head_tree[:12]}) — refusing to submit a no-op variant"
    )
```

Both `git rev-parse <sha>^{tree}` calls run inside `workdir` (the
operator's `eden-manual checkout` working tree). The earlier
reachability check already established that both `sha` and every
entry in `parent_commits` are reachable in that workdir, so neither
rev-parse can fail at this point.

Edge cases:

- **Zero parents**: a `KeyError` / `IndexError` would surface. In
  practice this can't happen — the wire's `Idea` schema requires
  `parent_commits` non-empty per chapter 02 §1.3 (current schema
  allows `[]`; we don't rely on that here because the manual CLI's
  upstream ideator always populates parent_commits, but we add a
  defensive `if not idea["parent_commits"]: return` skip-guard
  before the rev-parse to be safe).
- **Multi-parent**: handled in §D.7-edge — only the first parent is
  checked; this is intentional per Decision 8.
- **Status=error**: short-circuits before this guard.

### D.8 `cmd_push` doesn't get the guard

`cmd_push` is the `git add -A && commit && push` operator helper.
It pushes whatever the operator has staged; the no-op detection
fires at `execution-submit` time. Reason: `cmd_push` is also used
by the executor flow for **iterating** — operator pushes, runs
evaluator, comes back, pushes again. A no-op push at the iteration
stage is fine; what we want to forbid is the final no-op
**submission**. Keep the guard at submit-time.

The demo's diff also adjusted `cmd_push` to tolerate a clean
working tree (skip the commit step instead of erroring out). That's
a separate quality-of-life fix; we'll port it as part of this
chunk since it's small and rides on the same code path the operator
exercises, but it is **not** the no-op-guard — it's a "we already
committed, just push HEAD" fall-through. The semantic: if
`git diff --cached --quiet` reports clean AND `--message` was
provided, warn but don't error; push existing HEAD.

### D.9 Design doc — `docs/design/executor-no-op-variant-rejection.md`

Verbatim copy of `/tmp/eden-handoff/demo-no-op-design-doc.md`, with
one edit: add a top-line reference to issue #83 so the
tracking-chain is clear. Specifically, replace the existing first
paragraph (the one beginning `**Status:**`) with:

```markdown
**Status:** design exploration; informs a future amendment to
`spec/v0/03-roles.md` §3.3 + `spec/v0/04-task-protocol.md` §4.2 + a
matching conformance scenario. Tracked in
[issue #83](https://github.com/ealt/eden/issues/83).
**Origin:** ... (unchanged)
```

The doc lands under `docs/design/` (per the project-lifecycle
taxonomy: "options we considered, tradeoffs" — not yet
executable). Promotion to `docs/plans/` happens when the spec
amendment + impl chunk is scoped (i.e. when issue #83 is picked up).

### D.10 Skill-doc edits

#### `.claude/skills/eden-manual-evaluator/SKILL.md`

Line 116 replacement:

```diff
-curl -fsS -H "Authorization: Bearer $(grep '^EDEN_SHARED_TOKEN=' \
+curl -fsS -H "Authorization: Bearer admin:$(grep '^EDEN_ADMIN_TOKEN=' \
```

Same pattern as the demo's diff.

#### `.claude/skills/eden-manual-ideator/SKILL.md`

Line 63 replacement: "The token is persisted in `/tmp/eden-manual/.claims.json` automatically."
→ "Post-12a-1: claim ownership is identity-keyed (no per-claim opaque token). The CLI persists `{worker_id}` per task in `/tmp/eden-manual/.claims.json` so the submit step picks the matching worker bearer."

#### `.claude/skills/eden-manual-executor/SKILL.md`

Line 58 replacement: "Token + variant_id are persisted to `/tmp/eden-manual/.claims.json`."
→ "The variant_id is persisted to `/tmp/eden-manual/.claims.json` (post-12a-1, claim ownership is identity-keyed — no per-claim opaque token; the CLI's worker bearer is picked up from `/tmp/eden-manual/.credentials.json`)."

#### `.env`-divergence troubleshooting note (all three role skills)

Each of the three skills above gains a one-line troubleshooting
addition at the bottom of its "Verify" / "Best practices" section
(whichever is the existing tail of operator-facing notes), worded
roughly:

> If `eden-manual` exits with `wire error 401 on ... /workers...`,
> the running stack's `EDEN_ADMIN_TOKEN` has diverged from the
> `.env` file the CLI is reading. Bounce the stack against the
> current `.env`, or re-checkout the worktree the stack was
> brought up against.

The exact placement is implementation discretion; the goal is one
copy-pastable hint per role skill so an operator hitting the §4.D
corner case sees it without leaving their current skill.

No other touches. Specifically:

- DO NOT touch any `rationale → content` reference; main is already
  correct (the rename landed in 12a-1d).
- DO NOT touch eden-manual-experiment SKILL.md — it doesn't
  reference the broken surface; the demo's diff bundled an
  unrelated phase-2a custom-config expansion that is out-of-scope
  for this chunk.

## 4. Scope

### 4.A In scope

- Edit `reference/scripts/manual-ui/eden-manual` — auth port + no-op
  guard + clean-working-tree-tolerant `cmd_push`.
- Add `docs/design/executor-no-op-variant-rejection.md` (with the
  issue-#83 reference inserted).
- Edit `.claude/skills/eden-manual-evaluator/SKILL.md` (1-line auth
  curl fix; plus a one-line `.env`-divergence troubleshooting note
  per §4.D).
- Edit `.claude/skills/eden-manual-ideator/SKILL.md` (1-line claim
  semantics note; plus the same one-line `.env`-divergence
  troubleshooting note).
- Edit `.claude/skills/eden-manual-executor/SKILL.md` (1-line
  `Token + variant_id are persisted` fix; plus the same one-line
  `.env`-divergence troubleshooting note).

### 4.B Out of scope

- Spec amendments (issue #83).
- Server-side `Store.submit` no-op enforcement (issue #83).
- Conformance scenario for no-op rejection (issue #83).
- `MANUAL_DEMO_HANDOFF.md` (per operator).
- README / CONTRIBUTING (12a-2 wave 8).
- Any other CLI / skill / template not enumerated above.
- Web-UI changes (already ported in 12a-1).
- Renaming `EDEN_SHARED_TOKEN` in `setup-experiment.sh` or `.env.example`
  (already done by 12a-1).

### 4.C Files touched

| File | Change |
| --- | --- |
| `reference/scripts/manual-ui/eden-manual` | Auth port + no-op guard + push-tolerance |
| `docs/design/executor-no-op-variant-rejection.md` | New file (with issue #83 ref) |
| `docs/plans/eden-phase-12a-1h-cli-port.md` | This plan |
| `.claude/skills/eden-manual-evaluator/SKILL.md` | `EDEN_SHARED_TOKEN` → admin curl fix + 1-line `.env`-divergence troubleshooting note |
| `.claude/skills/eden-manual-ideator/SKILL.md` | claim-token semantics update + 1-line `.env`-divergence troubleshooting note |
| `.claude/skills/eden-manual-executor/SKILL.md` | `Token + variant_id are persisted` fix + 1-line `.env`-divergence troubleshooting note |

No new tests. The CLI is not under pytest (it's an operator-facing
script driven against a live Compose stack); operator manual
verification is the gate. See §6.

### 4.D Recovery posture

With Decision 5's register-first-then-reissue ladder, the CLI
handles every common credential-staleness mode automatically:
rerun of `setup-experiment.sh` (worker registry rebuilt; step 2
falls through to register-new), an admin running
`reissue_credential` out-of-band (step 1's whoami fails, step 2
hits the existing-row idempotent path and reissues), or a
`/tmp/eden-manual/.credentials.json` that's been hand-edited or
wiped (step 3 register-or-reissue). No operator cleanup is
required.

The one remaining mode that DOES require operator intervention:
the operator's `.env` has diverged from the running stack's actual
`EDEN_ADMIN_TOKEN`. The CLI reads its admin token from `.env`; if
the running stack was brought up against an older `.env` value
(or vice versa), every admin-gated call surfaces a 401 wire error.
Recovery: ensure the CLI is run from the same checkout the stack
was brought up against, or bounce the stack against the current
`.env`. This corner case is documented in the impl PR body and as
a one-line "if you see 401-on-admin in the wire-error, your .env
is stale" note in the affected skill docs.

## 5. Test design

No new automated tests.

### 5.A Manual verification (operator-driven)

Operator runs against a fresh stack:

```bash
cd reference/compose
bash ../scripts/setup-experiment/setup-experiment.sh \
    tests/fixtures/experiment/.eden/config.yaml \
    --experiment-id manual-12a-1h-verify
docker compose --env-file .env up -d --wait
```

Then:

1. **Read endpoints work (admin bearer default):**

   ```bash
   ./reference/scripts/manual-ui/eden-manual list-tasks --kind ideation
   ./reference/scripts/manual-ui/eden-manual list-commits
   ```

   Expected: both succeed (no auth errors).

2. **Claim + ideation submit:**

   ```bash
   ./reference/scripts/manual-ui/eden-manual claim <ideation-task-id>
   # Inspect /tmp/eden-manual/.credentials.json — should contain {"eden-manual": "<token>"}, mode 0600
   # Inspect /tmp/eden-manual/.claims.json — should contain {"<task-id>": {"worker_id": "eden-manual"}}
   ./reference/scripts/manual-ui/eden-manual ideation-submit <task-id> --ideas-file /tmp/ideas.json
   ```

   Expected: claim succeeds; ideation submit produces a `task.submitted` event observable via `list-tasks --state submitted` then transitions to `completed` on orchestrator accept.

3. **Execution submit happy path:**
   Drive a full execution: claim, checkout, change a file, commit,
   push, `execution-submit`. Variant transitions to `success` and is
   integrated by the orchestrator.

4. **No-op guard fires:**
   Drive an execution where the operator pushes a commit whose tree
   equals `parent_commits[0]`'s tree (easiest: `git commit
   --allow-empty -m 'no-op'`, push, then `execution-submit --sha
   <new-sha>`). Expected: CLI exits with the "variant tree is
   identical to parent_commits[0] tree" error and no
   `create_variant` call hits the wire.

5. **Evaluation submit:**
   Standard claim + `evaluation-submit --field score=0.5`. Expected:
   variant terminalizes as `success` with metrics recorded.

6. **Cred recovery — wiped local cred (registry intact):**
   `rm /tmp/eden-manual/.credentials.json` between two CLI sessions.
   Next `claim` call goes to step 3 of the ladder: posts
   `/workers` (register), the server returns no token (idempotent
   re-register hit the existing row), the CLI falls through to
   `reissue_credential`, persists the new token. Verify the file
   is recreated with mode 0600.

7. **Cred recovery — registry rebuilt (codex-flagged case):**
   Run a full claim+submit, persisting a valid credential. Then
   tear down the stack (`docker compose down -v`), re-run
   `setup-experiment.sh` for a fresh experiment (which rebuilds
   the worker registry), bring the stack up. With the stale
   `.credentials.json` still on disk, run a fresh `claim`.
   Expected: `_worker_bearer` calls `/whoami` with the stale
   bearer, gets 401 (or a different `worker_id` if the registry's
   idempotent-replay edge case fires; either way the verify
   fails), falls through to `_register_or_reissue`, which posts
   `/workers` and receives a fresh `registration_token` because
   the row doesn't exist in the rebuilt registry. The new
   credential gets persisted and the claim proceeds normally.

### 5.B Automated checks (existing pipeline)

The standard validation gates run unchanged:

- `uv sync && uv run ruff check . && uv run pyright && uv run pytest -q`
  — the CLI script is not under pytest but `ruff` and `pyright` do
  walk `reference/scripts/manual-ui/eden-manual` (it has a `.py`
  shebang); type errors / style errors will surface.
- `npx markdownlint-cli2` — the new design doc + the plan must
  pass markdownlint.
- `python3 scripts/check-rename-discipline.py` — no legacy vocab in
  any touched file. The design doc and the skill edits use only
  canonical vocab (`idea`, `variant`, `evaluation`, `content`).

### 5.C Document the test plan in the impl PR body

The impl PR's body lists the manual steps above as the test plan so
the operator can verify in their next manual-UI session and check
each off.

## 6. Verification gates (run before commit AND before push)

```text
uv sync
uv run ruff check .
uv run pyright
uv run pytest -q
npx --yes markdownlint-cli2@0.14.0 "**/*.md" "#node_modules" "#.venv" "#docs/archive/**" "#docs/plans/review/**"
python3 scripts/check-rename-discipline.py
```

The smoke / e2e bash scripts under `reference/compose/healthcheck/`
do NOT exercise the manual-UI CLI; running them is not strictly
required for this chunk but is encouraged as a sanity check that
nothing else regressed (the CLI's `.py` edit shouldn't affect the
Compose pipeline, but a stray import error would still surface).

## 7. Tricky areas

### 7.A Verify-and-recover runs on every CLI invocation

Decision 5's ladder makes `_worker_bearer` issue a `/whoami`
probe on every CLI invocation (per worker_id, once) when a
persisted credential exists. For the single-operator manual-demo
workflow this is a sub-second overhead. The benefit is that the
codex-flagged "rerun setup-experiment.sh → registry rebuilt →
401 on next call" failure mode is handled automatically: the
whoami returns 401, the CLI falls through to register-new (since
the row no longer exists), the new token gets persisted, and the
operator's first worker-gated call goes through.

The CLI's ladder intentionally diverges from
`eden_service_common.auth.bootstrap_worker_credential` on the
stale-credential branch (it allows fall-through to register
where the service-host helper does not), because the manual CLI's
lazy-registration model means the worker row may legitimately not
exist anymore. See Decision 5 for the full reasoning.

### 7.B `_wire`'s default-arg evaluation

`bearer=None` is the standard "mutable-default-trap-avoidance"
pattern. The `if bearer is None:` branch reads `EDEN_ADMIN_TOKEN`
from the env dict on every call (NOT at function-definition time);
that's correct because env is per-call. No mutable-default
landmine.

### 7.C `_record_claim` signature change is a caller-visible diff

`_record_claim(task_id, *, token, worker_id, variant_id)` →
`_record_claim(task_id, *, worker_id, variant_id)`. Only one
caller (`cmd_claim`); update both in lockstep.

### 7.D No need to touch `cmd_show`'s read path

`cmd_show` already uses the admin-default bearer (the new default
of `_wire`) and reads from `/ideas/{I}` and `/variants/{V}` which
are `either`-gated reads. No change required.

### 7.E Empty `parent_commits` defensive check

Decision 8 mentions a guard. Implementation: in
`cmd_execution_submit`, after the reachability loop:

```python
if not idea["parent_commits"]:
    # No parent to compare against; no-op guard inapplicable.
    pass
else:
    head_tree = ...
    parent_tree = ...
    if head_tree == parent_tree:
        sys.exit(...)
```

In practice the fixture experiment always has a single parent. The
guard is for symmetry with the design doc's "zero parents" case.

### 7.F Skill-doc edits don't drift glossary

The replacement strings in §D.10 use canonical vocab:

- "claim ownership is identity-keyed" — chapter 04 §3.3 phrasing.
- "worker bearer" — chapter 07 §13.1 phrasing.
- No introduction of synonyms.

### 7.G The plan-writing pitfall on `description` doesn't apply here

The CLI's `--description` flag writes to `Variant.description`,
which is the same field 12a-1 left in place (chapter 02 §1.4
optional field). No drift.

## 8. Risks / things to watch

1. **A reviewer pushing to "just amend the spec now and skip the
   client guard"**: defer to issue #83. The CLI guard is the
   pragmatic-now belt; the spec amendment is the longer-term
   suspenders. Both have value; they live on different timelines.
   The CLI guard's existence does NOT block issue #83.

2. **Markdownlint on the design doc**: the demo doc has 200+ lines
   of prose with the project's standard wrap. Check `markdownlint-cli2`
   passes on the doc as-pasted before opening the PR — the demo's
   `wc -l` reports 214 lines, which is the prose density the
   markdownlint rules expect. No anticipated lint hits, but verify.

3. **`ruff` on the CLI script's added helpers**: the helpers use
   standard-library-only constructs (`pathlib.Path.write_text`,
   `dict`, `json`); no third-party imports. The existing CLI is
   already-ruff-clean per current CI; the new code should pass too.

4. **`pyright` on the CLI script**: the existing script type-checks
   without annotations on internal lambdas / etc. The new helpers
   return `dict[str, str]` and `str` consistently. Verify
   `pyright` runs clean on the file.

5. **Operator runs the CLI against a stack that hasn't run
   `setup-experiment.sh` post-12a-1**: their `.env` file lacks
   `EDEN_ADMIN_TOKEN`. The CLI's `_load_env()` raises a clear
   "missing keys" error citing `EDEN_ADMIN_TOKEN`. Document in the
   impl PR body that operators must `setup-experiment.sh` against
   a post-12a-1 stack.

## 9. Sequence within the chunk

Stage 1 (this plan):

1. Write the plan doc.
2. Run validation gates locally.
3. Commit + push `plan/phase-12a-1h-cli-port`.
4. Open the plan PR; iterate to codex-review convergence.
5. Operator merges.

Stage 2 (impl):

1. Reset to fresh `origin/main`; branch `impl/phase-12a-1h-cli-port`.
2. Apply CLI edits (§D.2 → §D.7), add design doc (§D.9), apply
   skill edits (§D.10).
3. Run validation gates.
4. Commit + push `impl/phase-12a-1h-cli-port`.
5. Open the impl PR; iterate to codex-review convergence.
6. Operator merges.

## 10. Out of scope / followups

Tracked elsewhere or deferred:

- **Issue #83**: spec amendment + `Store.submit` enforcement + v1+roles
  conformance scenario. Will land in its own chunk; on landing, the
  CLI guard becomes belt-and-suspenders (keep it; it costs nothing
  and surfaces the error one round-trip earlier).
- **CLI `--help`-style invocation hint** when `_load_env` fails to
  find `EDEN_ADMIN_TOKEN`: include a "did you re-run
  setup-experiment.sh after the 12a-1 stack update?" hint. Polish;
  not required for this chunk.

(The 401-on-stale-cred case is no longer deferred — the
register-first-then-reissue ladder handles it automatically; the
remaining `.env`-divergence corner case ships as a one-line note
in §D.10.)

## 11. Estimated effort

Plan stage: ~30 min to draft, ~2-3 codex-review rounds.

Impl stage: ~45 min to apply diffs + run validations, ~2-3
codex-review rounds. The scope is well-defined; codex's findings
will most likely be on prose clarity in the design doc and edge
cases in the no-op guard (parent_commits == [], multi-parent
handling, error-message wording).
