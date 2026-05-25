# Issue #145 — Per-route store swapping for the experiment switcher

**Status.** Draft.

**Predecessor.** Phase 12c (control plane + cross-experiment dashboard, [`docs/plans/eden-phase-12c-control-plane.md`](eden-phase-12c-control-plane.md)). 12c Wave 5 shipped the `/admin/experiments/` dashboard, the `Session.selected_experiment_id` cookie field, and the `POST /admin/experiments/{E}/select` endpoint that writes it — but every existing per-experiment route still reads `app.state.store` / `app.state.experiment_id` (bound at startup against `--experiment-id`), so "select experiment Y" updates the page label without redirecting any data fetch. This chunk closes that loop.

**Issue.** [#145](https://github.com/ealt/eden/issues/145) — backfill of the 12c CHANGELOG-narrated deferral ("Per-route store swapping for the experiment switcher (plan §3.6) deferred: the switcher requires every existing per-experiment route (ideator, executor, evaluator, /admin/tasks, /admin/variants, /admin/workers, /admin/groups, …) to look up the active experiment from session state. The refactor is ~12 route files + their tests; out of scope this chunk."). priority:2-planned, cluster:identity.

**Naming.** Pre-draft check against [`docs/glossary.md`](../glossary.md) and AGENTS.md "Naming discipline":

- "Experiment switcher" is the canonical operator-facing name (12c §3.6); "active experiment" / "selected experiment" both refer to the value in `Session.selected_experiment_id`.
- New helper terms (introduced here, not in the glossary today):
  - **`active_experiment_id`** — the per-request resolved experiment id (session field falling back to the deployment-default). The verb is "resolve the active experiment".
  - **`StoreFactory`** — the per-process object that vends per-experiment `StoreClient` views on demand (mirrors 12c plan §5.4's "StoreClientFactory" sketch; the 12c plan deferred materializing it).
  - **`active_store`** — the per-request `StoreClient` produced by `StoreFactory.for_active(request)`. Helper name; not a separate concept.
- No collision with 12c's `lease.holder` (a worker_id; per-experiment) or 12a-1's `worker_id`. The web-ui is a deployment-level service; its workers register per-experiment (12a-1 §D.1) — see §3.2 below.

## 1. Context

### 1.1 What 12c shipped

[`reference/services/web-ui/src/eden_web_ui/sessions.py`](../../reference/services/web-ui/src/eden_web_ui/sessions.py) `Session.selected_experiment_id: str | None` carries the operator's selection across requests. [`reference/services/web-ui/src/eden_web_ui/routes/admin_experiments.py`](../../reference/services/web-ui/src/eden_web_ui/routes/admin_experiments.py) `POST /admin/experiments/{experiment_id}/select` writes it. The cross-experiment dashboard reads it back for the "is_selected" column. Nothing else does.

[`reference/services/web-ui/src/eden_web_ui/app.py`](../../reference/services/web-ui/src/eden_web_ui/app.py) binds `app.state.store` (a `StoreClient(base_url, experiment_id=args.experiment_id, bearer=…)`) and `app.state.admin_store` (same shape, admin bearer) once at startup. 67 references to `request.app.state.store` / `experiment_id` / `experiment_config` / `admin_store` / `worker_id` across 11 route files (counted via `grep -c`).

### 1.2 What the deferral footnote means in practice

- The user clicks "Select" on experiment Y in `/admin/experiments/`. The session cookie's `selected_experiment_id` is now `Y`.
- The user clicks the top-nav "Ideator" link. The ideator route does `store = request.app.state.store` — the **startup-bound** `--experiment-id=X` store. Pending ideation tasks shown are X's, not Y's. The same posture holds for `/executor/`, `/evaluator/`, `/admin/tasks/`, `/admin/variants/`, `/admin/workers/`, `/admin/groups/`, `/admin/work-refs/`, `/admin/artifacts/`, etc.

The deferral footer on the dashboard (12c) explains this to the operator: the switcher records intent; v0 routes ignore it.

### 1.3 Topology constraint that makes this tractable

12c Decision 11 fixes one task-store-server URL **deployment-wide**: the wire layer's `/v0/experiments/{id}/...` path structure routes per-call, and the control plane's `config_uri` names the experiment-config resource (not a per-experiment task-store URL). So per-route store swapping does NOT require service-discovery of a different `--task-store-url`; the URL is the same, only the `experiment_id` segment changes. (The issue text speculates "must be discoverable per #141 deployment-level worker inventory or similar"; that turned out not to be needed after 12c's data-plane decision. The plan calls this out explicitly so future operators don't re-litigate it.)

What DOES vary per experiment: the **bearer**. 12a-1 §D.1 defines workers as per-experiment-scoped (each experiment has its own worker registry). The web-ui's startup bearer is registered into the `--experiment-id=X` registry; it has no credential in Y. §3.2 below is the load-bearing design call.

### 1.4 Dependencies enumerated in the issue, current state

| Issue | What it does | Relationship to this plan | Current state |
|---|---|---|---|
| [#128](https://github.com/ealt/eden/issues/128) | Rename `experiment_id`/`worker_id`/`group_id` to opaque system-minted ids with separate user-supplied `name` fields | Touches the same wire surface (per-experiment endpoints + bearer principals). If #128 lands BEFORE this, identifier shape changes (`exp_…` opaque ids in path segments; bearer principals still opaque worker_ids). If this plan ships first, #128's rename re-targets the same code paths. No functional collision; mostly mechanical. | Open. Plan-needed. |
| [#140](https://github.com/ealt/eden/issues/140) | Operator-as-worker (Model B): each human operator registers a per-experiment worker; web-ui sessions carry the operator's worker_id + bearer | Strong overlap. Under #140, the session's `worker_id` is per-operator (not the web-ui's deployment-default `web-ui-1`), AND it's per-experiment — so the operator naturally has a credential in the selected experiment if they registered there. Without #140, the web-ui has to mint / persist a service-worker credential per experiment (see §3.2 Option C). Both shapes work; the credential-bookkeeping shifts. | Open. Plan-needed. |
| [#141](https://github.com/ealt/eden/issues/141) | Worker registry as deployment-level infra; experiments opt in | If #141 lands, the per-experiment-worker constraint relaxes: one web-ui worker record covers all experiments. The credential-management work in §3.2 simplifies dramatically. Without #141, the plan ships under today's per-experiment-worker model. | Open. Plan-needed. |

**Sequencing call.** Don't wait for any of #128 / #140 / #141. All three are open, plan-needed, and the issue itself is the smallest of the four. The plan ships under today's identifier shape and today's per-experiment-worker model. §3.2 picks the credential-bootstrap shape that minimizes churn when #140 / #141 later land. The plan calls out per-section what changes when those amendments arrive.

### 1.5 What "the refactor is ~12 route files" actually means

Concrete inventory (each gets a per-handler rewrite):

| File | LOC | Active-experiment-bearing call sites |
|---|---|---|
| `routes/ideator.py` | 877 | `store`, `experiment_id`, `experiment_config`, `worker_id` (claim caller); ~10 handlers |
| `routes/executor.py` | 863 | `store`, `experiment_id`, `experiment_config`, `worker_id`, `repo` (per-experiment local clone); ~10 handlers |
| `routes/evaluator.py` | 692 | `store`, `experiment_id`, `experiment_config`, `worker_id`; ~9 handlers |
| `routes/admin/observability.py` | 575 | `store`, `admin_store`, `experiment_id`; ~9 handlers |
| `routes/admin/actions.py` | 443 | `store`, `admin_store`, `worker_id` (actor attribution); ~6 handlers |
| `routes/admin/work_refs.py` | 230 | `store`; ~3 handlers |
| `routes/admin/index.py` | 92 | `store`, `admin_store`; 1 handler |
| `routes/admin_workers.py` | 623 | `store`, `admin_store`, `experiment_id`; ~10 handlers |
| `routes/admin_groups.py` | 612 | `store`, `admin_store`, `experiment_id`; ~10 handlers |
| `routes/admin_artifacts.py` | 125 | `store`; ~2 handlers |
| `routes/artifacts.py` | 175 | `store`, `artifacts_dir`; ~2 handlers |
| `routes/index.py` | 27 | `experiment_id` (header rendering); 1 handler |
| `routes/_helpers.py` | 242 | none directly — but `_lineage.py` (468 LOC) and `_submit_readback.py` (210 LOC) take `store` as an argument and recur |
| `routes/admin_experiments.py` | 288 | DOES NOT need to change — it's deliberately deployment-scoped (cross-experiment dashboard); never reads `app.state.store` |
| `routes/auth.py` | 49 | session-only; no store |

**~12 route files** is right — the issue's count. Including the two helpers and the index, that's 14 modules; auth + admin_experiments are deployment-scoped and stay put.

## 2. Decisions

These are the load-bearing design calls; §3 unpacks each.

1. **Resolve the active experiment per-request via a request-scoped helper, not via middleware that mutates `request.state`.** A `resolve_active_experiment(request) -> str` helper reads `Session.selected_experiment_id`, falls back to `app.state.experiment_id` (the deployment default), and returns the resolved id. Each route handler calls it explicitly (one line at the top). The alternative — a FastAPI middleware that decorates `request.state.experiment_id` — is rejected: it hides the data dependency and makes the 14-module refactor harder to review chunk-by-chunk.

2. **One task-store-server URL deployment-wide; `StoreFactory.for_experiment(experiment_id)` returns a per-experiment `StoreClient` view against it.** Per 12c Decision 11 there is no service discovery. The factory caches `StoreClient` instances by `(experiment_id, bearer_role)` so each request reuses the same underlying `httpx.Client` (connection-pooling preserved). Cache eviction: TTL-less for the process lifetime; cache size bounded by `len(known_experiments) * 2` (worker + admin bearer per experiment). For a deployment with hundreds of experiments this would grow; today the registry holds units to tens, so we don't optimize further.

3. **Bearer plumbing: the web-ui mints a service-worker credential per target experiment on first access, using the deployment admin token.** Today's startup flow registers `web-ui-1` in the `--experiment-id=X` experiment only. When the operator switches to experiment Y, the next request that needs to talk to Y's task-store-server triggers a JIT `register_worker(worker_id=args.worker_id)` call against Y's registry, persists the returned bearer to `${XDG_STATE_HOME}/eden/web-ui/<experiment_id>/<worker_id>.cred`, and uses it for subsequent requests against Y. This is the smallest-blast-radius shape that works under today's per-experiment worker model. Once #140 (operator-as-worker) lands, this code path is removed in favor of the operator's session-carried bearer; once #141 (deployment-level workers) lands, only one registration is ever needed and the per-experiment dir collapses. §3.2 below.

4. **Routes operate on the active experiment; the deployment-default is a fallback only.** When the session has no `selected_experiment_id` (first sign-in, control plane disabled, switcher never used), routes use `app.state.experiment_id`. This preserves today's single-experiment behavior with zero operator-visible change. The fallback is for the no-control-plane posture (`control_plane=None`) and the very-first-request case.

5. **Switcher state is per-session, not per-tab.** A user with two tabs open shares one session cookie; selecting experiment Y in tab A changes tab B's data on next request. The alternative (per-tab via a query param) was rejected: it duplicates state, has to be threaded through every link, and breaks the "permalink to a specific page in experiment Y" use case operators ask for. A query-param-based override (`?experiment_id=…`) is a v1 affordance, NOT v0; see §7.5.

6. **`experiment_config` is loaded per-experiment from an on-disk config directory; no new wire endpoint.** Today the web-ui pins one `experiment_config` at startup (the YAML file path from `--experiment-config`). The task-store-server stores `experiment_config` text internally (set at import time per [`reference/packages/eden-storage/src/eden_storage/_checkpoint.py`](../../reference/packages/eden-storage/src/eden_storage/_checkpoint.py)) but does NOT expose it as a runtime wire read — only `read_experiment()` (runtime state) and `read_experiment_state()` are wire-exposed. Adding a `GET /v0/experiments/{E}/config` endpoint would be a spec change (Chapter 7), which this plan does not undertake (Decision 10).

The plan instead introduces a per-experiment YAML directory: a new `--experiment-config-dir <path>` flag. The web-ui's resolve helper loads `<dir>/<experiment_id>.yaml` lazily on first access (cached for the process lifetime; configs are immutable post-create). The `setup-experiment.sh` script writes each experiment's YAML to this dir as part of its existing flow (and the operator can populate it manually too). Single-experiment / no-control-plane mode still accepts `--experiment-config <single-yaml>` and routes all requests through it (preserves today's behavior). The dir-vs-single distinction is operator-facing in CLI help; one-of-each-not-both validated at startup.

**Why not add the wire endpoint instead.** Adding `GET .../config` is operationally cleaner (one source of truth, no filesystem coupling) but it's a normative wire amendment touching chapter 7 + JSON schemas + Pydantic models + the task-store-server's read path + conformance citation gating. That's a separate chunk; filed as a follow-up issue (§4.2). The on-disk approach is the smaller change that closes the immediate UX gap.

7. **Admin-store cache mirrors the worker-store cache.** `admin_store` is `StoreClient(…, bearer="admin:<token>")`. The factory vends per-experiment admin views the same way it vends worker views; no separate JIT registration (admin auth is one bearer deployment-wide).

8. **Three-state experiment-validity classification at resolve time; route accordingly.** 12c separates the control-plane registry from the task-store-server's per-experiment data (12c plan Decision 5; native creation is "control plane register first, then task-store ops" per 12c §3.5; the existing web-ui `POST /admin/experiments/register` at [`routes/admin_experiments.py`](../../reference/services/web-ui/src/eden_web_ui/routes/admin_experiments.py) calls only the control plane, NOT the task-store-server). The resolve helper therefore classifies the active experiment in three states, not two:

| State | Probe outcome | Resolve helper behavior |
|---|---|---|
| **registered + seeded** | Control plane returns the entry AND `read_experiment_state()` on the task-store-server returns 200 | Normal: return the active `experiment_id`. |
| **registered + unseeded** | Control plane returns the entry; `read_experiment_state()` returns 404 | Return the id with a per-request flag `experiment_unseeded=True`. Routes that read (ideator listing, admin tasks) render an "experiment exists but has no tasks yet — initialize via `setup-experiment` or a checkpoint import" empty-state. Routes that write (claim, submit, create_task) refuse with a clear "experiment is not yet seeded" banner. NOT a stale-selection redirect. |
| **unregistered** | Control plane returns 404 | Raise `StaleSelection`; redirect to `/admin/experiments/?error=stale-selection`; clear the session field. |

The unseeded state is observable in practice: an operator who runs `POST /admin/experiments/register` (a 12c-shipped admin form) creates a control-plane registry entry but no task-store-server experiment until `setup-experiment.sh` / checkpoint import runs. Pre-this-plan, the unseeded state is invisible to operators because no one navigates to the experiment via the switcher. Post-this-plan, operators can hit it; misclassifying it as stale would be a silent UX regression.

To minimize per-request latency, the resolve helper caches the two-probe state (control-plane present + task-store-server present) for 5 seconds (matches §3.7's `list_experiments` TTL). Cache invalidates on `POST /admin/experiments/{E}/select`. Stale cache → at-most-one wrong-state render before the next refresh; acceptable.

9. **Per-route `repo` (executor's local bare clone) is also per-experiment.** Today the web-ui's `--repo-path` flag is one bare clone per process. In the multi-experiment world each experiment has its own integrator repo (one bare clone per experiment in the deployment). The executor module's `_materialize_repo` becomes a per-experiment lazy materializer: clone-if-missing under `${args.repo_path}/<experiment_id>.git`, fetch on each request. This is the only piece of the plan that materially changes the filesystem layout the web-ui deployment expects.

10. **No spec amendment; no JSON-schema change; no Pydantic-model change.** This refactor is implementation-internal to the web-ui reference service. The wire surface, task-store-server, control plane, and other workers are unchanged. The only protocol-level adjustment is a one-line deletion from 12c plan §3.10 ("What 12c does NOT do") — that line is now "what 12c also does NOT do, but issue #145 closes". CHANGELOG records that closure.

11. **Conformance: no new scenarios.** Chapter 9 §6 (IUT contract) confines conformance to the chapter-7 HTTP binding. Per-route store swapping is web-ui behavior, not wire-binding behavior — there is no observable signal at the IUT contract level. The existing 12c `v1+multi-experiment` scenarios cover the wire side; this plan adds web-ui integration tests (in `reference/services/web-ui/tests/`) but no conformance scenarios. (See AGENTS.md "Conformance-plan MUSTs must be filtered through the IUT contract before drafting scenarios" pitfall.)

12. **Audit AND remove "Wave 5 surfaces the session field; per-route swap is a follow-up" deferral footer in `admin_experiments.py` + `admin_experiments.html`.** Once this lands, the v0-limitation note in the dashboard is wrong. Removing it is part of the final wave (§4).

## 3. Design

### 3.1 The `resolve_active_experiment` helper

A new function in `routes/_helpers.py`:

```python
def resolve_active_experiment(request: Request) -> str:
    """Return the experiment_id the current request operates against.

    Reads ``Session.selected_experiment_id`` (set via
    ``POST /admin/experiments/{E}/select``). Falls back to
    ``app.state.experiment_id`` (the deployment default).

    Validates that the experiment still exists in the control plane
    (when configured). Raises ``StaleSelection`` if the session
    references an experiment that has since been unregistered;
    callers catch it and redirect to the dashboard.

    When ``control_plane`` is ``None`` (single-experiment deployment),
    this always returns ``app.state.experiment_id`` — no validation
    call, no overhead. The single-experiment posture is unchanged.
    """
```

Caller shape (every refactored handler starts this way):

```python
@router.get("/")
async def page(request: Request) -> HTMLResponse:
    try:
        experiment_id = resolve_active_experiment(request)
    except StaleSelection:
        return _redirect_to_dashboard(stale=True)
    store = request.app.state.store_factory.for_experiment(experiment_id)
    config = await _resolve_active_config(request, experiment_id)
    ...
```

The two-line cost (resolve + factory lookup) is by design: the data dependency is visible at the top of every handler, so a reader knows immediately which experiment a page operates against.

### 3.2 Credential plumbing (the load-bearing piece)

Today: `cli.py` calls `resolve_worker_bearer(args, worker_id=args.worker_id, labels={"role": "web-ui"})` once at startup, which delegates to [`eden_service_common.auth.bootstrap_worker_credential`](../../reference/services/_common/src/eden_service_common/auth.py). That function already implements three load-bearing disciplines that the JIT path MUST also use, NOT reimplement:

1. **Per-`worker_id` exclusive lock** (`_bootstrap_lock`): `fcntl.flock` on `<credentials_dir>/<worker_id>.token.lock`. Serializes register / verify / reissue for two web-ui processes (or two coroutine tasks within one process) racing the same `(experiment_id, worker_id)`. Without it, a `register_worker` (idempotent — returns the existing record with NO new token on repeat per the wire client's contract at [`reference/packages/eden-wire/src/eden_wire/client.py`](../../reference/packages/eden-wire/src/eden_wire/client.py) `register_worker`) followed by a `reissue_credential` corrupts the persisted token.

2. **Idempotent-register → reissue branch**: if `register_worker` returns no new `registration_token` (because the record already exists with NO token field on the response — idempotent shape per 12a-1 §D.1), follow up with `reissue_credential` to mint a fresh credential. The shared helper handles both branches; the JIT path reuses it verbatim.

3. **Persisted-token verification via `/whoami`**: a persisted-but-stale token (admin reissued after our boot, etc.) gets caught by a `whoami()` probe on startup and triggers the reissue path. Same shape used per experiment in the JIT case.

After this plan: a `BearerCache` object backs the `StoreFactory`. On `StoreFactory.for_experiment(Y)`:

1. If the cache holds `(Y, worker_role)` → return cached StoreClient.
2. Else, **delegate to a per-experiment `bootstrap_worker_credential`** call (the SAME function 12a-1 uses, called with `experiment_id=Y` and `credentials_dir=<EDEN_CREDENTIAL_DIR>/<Y>`). That function does all three of: lock, read-persisted-or-register-or-reissue, verify-via-whoami. The plan reuses the helper rather than rewriting it. Cache the result; return.
3. **Routing for the four failure branches of `bootstrap_worker_credential`** (these are observable today in the startup-bootstrap path):
   - `NotFound` from the task-store-server (experiment Y doesn't exist there) → bubble up; the resolve helper classifies this per Decision 8 (registered-but-unseeded vs stale).
   - `Unauthorized` after register (admin token rejected) → raise `MissingAdminToken`-as-`AdminTokenRejected`; redirect with a clear error.
   - `RuntimeError("admin token required")` from the helper (no admin token at all AND no persisted credential AND no usable cached token) → raise `MissingAdminToken`; redirect to the dashboard with `error=cannot-bootstrap-credential&exp=<Y>`.
   - Transport error → raise `TaskStoreUnreachable`; redirect to the dashboard with `error=task-store-unreachable`.

**Control-plane authority for the switcher.** A second credential domain matters here that round 0 flagged: the web-ui's control-plane client today is constructed admin-token-only (see [`reference/services/web-ui/src/eden_web_ui/cli.py`](../../reference/services/web-ui/src/eden_web_ui/cli.py) `_build_control_plane_client`). Chapter 11 §6 endpoint authority (per 12c plan §5.1) lets `list_experiments` / `read_experiment_metadata` be called by EITHER an admin bearer OR a deployment-scoped worker bearer. To keep "no admin token at runtime, but switcher still works" viable, the plan adds a deployment-scoped web-ui worker credential persisted at `<credential-dir>/control-plane/web-ui.cred`:

- At startup, when `--control-plane-url` is set, the web-ui ALSO bootstraps a control-plane-scoped worker (`POST /v0/control/workers` with the admin token, `add_to_group` to a new `web-ui` group OR `dashboards`-shaped; specifics per 12c plan §5.1 chapter 11 §6 endpoint table).
- That credential is the long-lived deployment-scoped bearer the `ControlPlaneClient` uses thereafter for read calls. Admin token is required only at bootstrap time, NOT at runtime, matching the worker-host pattern.
- The control-plane `register_experiment` / `unregister_experiment` calls still require admin (they're admin-gated per 12c §5.1). The dashboard's admin form (12c-shipped) already handles this; this plan doesn't change it.

**Posture matrix** (updated to four postures, replacing the round-0 two-state model):

| Posture | Admin token | Control-plane web-ui credential | Per-experiment web-ui credentials | Switcher works? |
|---|---|---|---|---|
| **A (no control plane)** | optional | n/a | startup-only for the single `--experiment-id` | n/a — switcher hidden |
| **B (control plane, admin-token at runtime)** | present | bootstrapped at startup | JIT-bootstrapped on switch | yes |
| **C (control plane, admin-token bootstrap-only)** | bootstrap only; absent at runtime | bootstrapped at startup, persisted | persisted from a prior bootstrap; JIT path is unavailable | yes for already-credentialed experiments; redirect with `error=cannot-bootstrap-credential` for new ones |
| **D (control plane, no admin token ever)** | absent | unbootstrapped | unbootstrapped | dashboard read fails with banner; switcher hidden |

Posture B is the default Compose path. Posture C is the production posture (operators rotate the admin token out of the runtime env after first boot, matching the worker-host pattern). Posture D is the "control plane is configured but auth never set up correctly" failure mode — the plan surfaces it via banner rather than silently degrading.

`EDEN_CREDENTIAL_DIR` defaults to `${XDG_STATE_HOME:-$HOME/.local/state}/eden/web-ui/` and is operator-overridable via a new CLI flag `--credential-dir <path>`. In Compose, the dir is a bind-mount under `${EDEN_EXPERIMENT_DATA_ROOT}/web-ui-credentials/` (same posture as the per-host credentials volumes in 12a-1g §D.3). Layout:

```text
<credential-dir>/
  control-plane/web-ui.cred                 # posture B/C deployment-scoped
  control-plane/web-ui.token.lock           # bootstrap lock
  <experiment_id_1>/web-ui-1.cred           # per-experiment worker
  <experiment_id_1>/web-ui-1.token.lock     # per-experiment bootstrap lock
  <experiment_id_2>/web-ui-1.cred
  ...
```

**Why JIT bootstrap, not at-startup?** The web-ui starts before any experiment exists in a fresh deployment; the registry is empty. A startup loop "for each registered experiment, bootstrap our credential" requires reading `list_experiments` before serving any request, AND requires re-running on every new experiment registration. A JIT path is simpler and correctness-equivalent.

**Resolved internal contradiction (round 0 finding).** Earlier wording in §3.8 said startup `for_experiment(...)` would skip the JIT register. That was inconsistent with §3.2's "cache-miss IS JIT register". The corrected shape: startup does NOT call `for_experiment(...)` for any non-default experiment at boot. It bootstraps only:
- The default experiment's worker credential (if `--experiment-id` is provided — preserves today's startup shape).
- The control-plane-scoped web-ui credential (if `--control-plane-url` is provided).
Per-non-default experiment bootstrap is exclusively JIT, gated by the first cache miss.

**Failure-mode taxonomy:**

| Scenario | Resolve helper outcome | UI |
|---|---|---|
| Session has no `selected_experiment_id` | Fall back to deployment default | Page renders for default exp; switcher dropdown shows "Default (X)" |
| Session points to existing experiment, credential cached | Return cached StoreClient | Page renders |
| Session points to existing experiment, credential persisted on disk | Read from disk, cache, return | Page renders |
| Session points to existing experiment, no credential, admin token available | JIT register, persist, cache, return | Page renders (with a tiny one-time startup delay; ~50ms for one wire call) |
| Session points to existing experiment, no credential, no admin token | Raise `MissingAdminToken` | Redirect to `/admin/experiments/?error=cannot-bootstrap-credential&exp=<Y>` |
| Session points to NONEXISTENT experiment (unregistered concurrently) | Raise `StaleSelection` | Redirect to `/admin/experiments/?error=stale-selection`; session field cleared |
| Control plane unreachable on resolve | Raise `ControlPlaneUnreachable` | Redirect to `/admin/experiments/?error=control-plane-unreachable` (operator action: retry) |

**What changes when #140 lands.** The web-ui's `worker_id` is no longer a deployment-default service worker; it's the operator's session-carried worker_id. The bootstrap path moves from "web-ui registers itself" to "the operator registered themselves on the dashboard before clicking switch" — and the credential cache keys on `(experiment_id, session_worker_id)` instead of `(experiment_id, args.worker_id)`. The `BearerCache` shape and the `StoreFactory` interface are unchanged.

**What changes when #141 lands.** The per-experiment-worker registry collapses to a deployment-level one. The web-ui registers `web-ui` ONCE; the per-experiment-credential dir disappears. The `BearerCache` keys on `(experiment_id,)` and dispatches a single shared bearer for every experiment. Again, the `StoreFactory` interface is unchanged.

### 3.3 The `StoreFactory`

```python
class StoreFactory:
    """Vends per-experiment StoreClient views against one task-store URL.

    Caches by (experiment_id, role). Role is "worker" or "admin".
    Connection-pools by sharing one ``httpx.Client`` across all
    cached StoreClients (passed via the ``client=`` kwarg already
    accepted by ``StoreClient.__init__``).
    """

    def __init__(
        self,
        *,
        base_url: str,
        bearer_cache: BearerCache,
        admin_token: str | None,
        shared_client: httpx.Client,
    ) -> None: ...

    def for_experiment(
        self, experiment_id: str, *, role: Literal["worker", "admin"] = "worker"
    ) -> StoreClient: ...

    def close(self) -> None:
        """Closes the shared httpx.Client and clears caches."""
```

Constructed once in `app.py`'s `make_app(...)`; stored at `app.state.store_factory`. `app.state.store` / `app.state.admin_store` are removed (they were single-experiment-bound; now everything goes through the factory). The factory's `for_experiment(args.experiment_id, role="worker")` at startup-time fills the cache with the deployment-default's StoreClient — preserving existing behavior for the no-control-plane / no-selection case.

Lifecycle: the factory owns the shared `httpx.Client`. `make_app` registers a FastAPI shutdown handler that calls `factory.close()`.

### 3.4 Experiment-config fetching

Today `experiment_config: ExperimentConfig` is read from `--experiment-config <path>` at startup and shoved into `app.state.experiment_config`. Routes read it directly (e.g., `ideator.py` reads `objective`, `evaluator.py` reads `evaluation_schema`).

The task-store-server already persists experiment configs ([`spec/v0/08-storage.md`](../../spec/v0/08-storage.md) §4.1) and exposes them via the wire — `StoreClient.read_experiment_config()` (already implemented; used by evaluator host for schema validation). The plan adds a tiny in-memory cache on the factory (keyed by `experiment_id`, no TTL — configs are immutable after experiment creation):

```python
def active_config(request: Request, experiment_id: str) -> ExperimentConfig: ...
```

Behavior:

- Control-plane mode: read from `store_factory.for_experiment(experiment_id).read_experiment_config()`, cache.
- No-control-plane mode: return `app.state.experiment_config` (the YAML loaded at startup).

This decouples the web-ui from needing `--experiment-config` to point at a single specific YAML in the multi-experiment posture. For the single-experiment posture, the flag is still required (the cache cold-start needs *something* for the default experiment before the first wire round-trip).

### 3.5 Per-experiment integrator repo (`--repo-path`)

The executor module needs a local bare clone of the integrator repo to render `work/*` refs and produce diffs. Today: one clone at `--repo-path`. Multi-experiment: one clone per experiment under `<repo-path>/<experiment_id>.git/`.

Materializer (in `cli.py` / `app.py`):

- Today's `_materialize_repo(args)` becomes `_materialize_repo_for(args, experiment_id)`, called lazily by the executor module's resolve helper.
- On miss: `git clone --bare <forgejo_url>/<experiment_id>.git <repo-path>/<experiment_id>.git` (when `--forgejo-url` is set), else `GitRepo(<repo-path>/<experiment_id>.git)` if the directory pre-exists.
- Fetch-on-access (matching the existing once-per-startup behavior, generalized to once-per-request — operators rely on the AGENTS.md guidance "Long-lived bare/non-bare clones need read-before-write/display fetches against the remote").

Cache: per-experiment `GitRepo` instances on the factory; a tiny dict.

`--forgejo-url`'s grammar today is `http(s)://<host>/<org>/<exp>.git` — a per-experiment URL. The plan parses out the org base (`http(s)://<host>/<org>/`) and substitutes the active `experiment_id`. Documented in the CLI help; backwards-compatible for single-experiment deployments where `--experiment-id=X` and `--forgejo-url=…/<org>/X.git` agree.

### 3.6 Session-CSRF interaction across switches

CSRF token lives in the session cookie (12a-1 §D.5). Switching experiments does NOT invalidate CSRF — the token is per-session, not per-experiment. A form submitted from a page that was rendered against experiment X but submitted after the user switched to Y would POST to a Y-scoped handler with X-scoped form data.

**Decision: route handlers compare the active experiment at form-render time vs. at form-submit time.** Hidden field `<input type="hidden" name="form_experiment_id" value="{{ experiment_id }}">` on every mutating form; submit-side handler raises `ExperimentMismatch` if the values disagree, and redirects to `/admin/experiments/?error=switched-mid-form&from=X&to=Y` with a clear "you switched experiments while filling out a form on X; the submission was discarded" message. Better than silently writing to the wrong experiment. (This is a v0-acceptable UX; a "save draft / move with me" affordance is a v1 follow-up.)

The CSRF token itself is unchanged.

### 3.7 Top-nav switcher widget

12c shipped the cross-experiment dashboard at `/admin/experiments/`. The top-nav today has an "experiments" link to that page. This plan additionally surfaces an in-nav **dropdown** showing the current selection + a quick-switch list:

- Label: `Active: <experiment_id>` (or `Default: <experiment_id>` when no selection is recorded).
- Dropdown items: every registered experiment in the control plane, each a POST form to `/admin/experiments/{E}/select` with the CSRF token.
- Hidden in no-control-plane deployments (same template guard as the existing top-nav "experiments" link).

The list fetch on every page render is a perf concern (`list_experiments` once per request). Mitigation: a 5s in-process TTL cache on `list_experiments`'s result (same pattern as 12c §7.4's dashboard counts). Acceptable: a stale 5s window is invisible to operators.

### 3.8 Startup cold-start sequence

The web-ui's startup probe (`wait_for_task_store`) requires `--experiment-id`. The factory needs SOME experiment to seed its caches. The cleanest shape:

- Startup ALWAYS: `--experiment-id` (still required) names the deployment-default. `bootstrap_worker_credential(experiment_id=args.experiment_id, ...)` for the default-experiment worker. `--experiment-config` (still required in no-control-plane mode; optional in control-plane mode when `--experiment-config-dir` is set) seeds the config cache for the default.
- Startup IF `--control-plane-url` SET: ALSO bootstrap the control-plane-scoped web-ui credential (§3.2 posture B/C). Do NOT loop `list_experiments` to pre-register per-experiment workers; that's deferred to JIT on first switch.
- On every request: `resolve_active_experiment(request)` then `store_factory.for_experiment(active_id)`; the factory does JIT bootstrap on cache miss.

The deployment-default flag is preserved for three reasons: (a) cold-start needs SOMETHING for the no-selection case; (b) the no-control-plane single-experiment deployment is preserved unchanged; (c) the bootstrap-time admin-token / `wait_for_task_store` posture from today's startup stays load-bearing on first boot (Posture B/C in §3.2). The default-experiment lookup in the resolve helper is "fast path with no validation"; only non-default experiments traverse the three-state classification of Decision 8.

### 3.9 Alternatives considered

**Per-request fresh `StoreClient` (no caching).** Construct a new client on every handler call. Pros: no cache invalidation logic. Cons: connection-pooling broken; per-handler latency ~+5ms (TCP handshake + TLS). Rejected because the connection-pool cost is real at 10+ requests/sec.

**Middleware that mutates `request.state.experiment_id`.** Cleaner-looking handlers (no resolve call). Rejected because the data dependency is invisible to readers; a 14-module refactor where the change is "delete the visible state lookup, gain a hidden state lookup" is the kind of refactor where bugs accrue silently. Decision 1.

**One `worker_id` per session, never service-default `web-ui-1`.** Effectively requires #140 (operator-as-worker) as a hard prerequisite. Rejected per §1.4 sequencing call: this plan ships under today's model; #140 layers on top.

**Tab-scoped selection via query param.** Allows two tabs to view different experiments simultaneously. Rejected for v0 per Decision 5; v1 affordance noted in §7.5.

**Force the selection into the URL (e.g., `/<experiment_id>/ideator/`).** Operator-friendly permalinks; matches REST shape better. Rejected: every existing route's URL changes, every template's link rendering changes, every test fixture's URL changes — significantly larger refactor than the plan-§5 wave shape and gains little operator value (the dashboard's `?exp=Y` permalink covers the use case at far lower cost).

### 3.10 What this plan does NOT do

- **No spec change.** Per Decision 10. The 12c plan §3.10 line about per-route store swap being out-of-scope is removed from CHANGELOG; nothing in `spec/v0/` moves.
- **No conformance scenario.** Per Decision 11.
- **No new wire endpoint.** All wire ops exist; the plan rebinds which `experiment_id` segment they target per request.
- **No operator-CLI changes** (eden-manual is per-invocation; it doesn't have a "switcher").
- **No multi-tab parallel experiments.** Per Decision 5.
- **No cross-experiment views inside per-experiment pages.** "Show me ideas across all experiments" is the cross-experiment dashboard's job (12c §3.6); the per-experiment ideator stays scoped to one.
- **No `worker_id` flag change.** Today's `--worker-id` is preserved; #140 is what changes it.

## 4. Scope

### 4.1 In scope

Code (reference impl, web-ui only — no other service touched):

- New `routes/_helpers.py` helpers: `resolve_active_experiment`, `active_config`, `StaleSelection` / `MissingAdminToken` / `ControlPlaneUnreachable` exceptions.
- New `eden_web_ui/store_factory.py` module: `StoreFactory`, `BearerCache`, `_persisted_credential_path`. (Integrated to its own module rather than buried in `app.py` because the unit tests need to drive it directly.)
- New `eden_web_ui/credentials.py` module: persisted-credential read/write helpers. Mirrors `eden_service_common.resolve_worker_bearer`'s on-disk shape (12a-1 §D.2) but scoped to web-ui paths.
- Per-route refactor (the 14 modules in §1.5): every `request.app.state.store` / `request.app.state.experiment_id` / `request.app.state.experiment_config` / `request.app.state.admin_store` lookup gets replaced with the resolve-then-factory pattern. `request.app.state.worker_id` (used by `admin/actions.py:191,307` for `actor` attribution) stays as the deployment-default until #140 lands.
- `app.py` `make_app(...)`: drop `store`, `admin_store`, `experiment_config` parameters; add `store_factory`. Templates that read `experiment_id` from globals (`templates.env.globals["experiment_id"]`) instead read it from a `Request` context var so per-request rendering reflects the active experiment.
- `cli.py`: build `StoreFactory` instead of one `StoreClient`. Add `--credential-dir` (per-experiment credentials) and `--experiment-config-dir` (per-experiment YAML configs per Decision 6) flags. Preserve `--experiment-id` (deployment default), `--experiment-config` (fallback for single-experiment / no-config-dir mode), `--task-store-url`, `--control-plane-url`, `--repo-path`, `--forgejo-url`, `--worker-id`. Reject startup when both `--experiment-config` and `--experiment-config-dir` are set with different values for the deployment-default experiment; allow `--experiment-config-dir` to be the only source when control-plane mode is on.
- `templates/base.html`: top-nav switcher dropdown widget per §3.7. Hidden when `control_plane` is None.
- `templates/admin_experiments.html`: remove the v0-limitation footer per Decision 12; add a "switch to this experiment" CTA on each row (already exists in 12c).
- `templates/*_form.html` (every mutating form): hidden `form_experiment_id` field per §3.6.
- Per-experiment repo materialization in `cli.py` / `app.py` per §3.5.

Tests (web-ui only):

- `tests/test_store_factory.py` (new): factory caching, eviction, shared-client lifecycle, JIT credential bootstrap, persisted-credential round-trip, all failure modes from §3.2's table.
- `tests/test_resolve_active.py` (new): session-default fallback, stale-selection redirect, missing-admin-token redirect, control-plane-unreachable redirect.
- `tests/test_admin_experiments_routes.py` (existing — the actual switcher-test home today; there is no `test_experiment_switcher.py`): EXTEND with full round-trip `select → next page renders active experiment's data`; switch-mid-form rejection per §3.6; deployment-default fallback for fresh-session.
- Existing tests under `tests/` (**43 files** counted via `find reference/services/web-ui/tests -name 'test_*.py' | wc -l`, NOT 30) all get a shared fixture update: instead of `make_app(store=..., experiment_id=..., experiment_config=..., admin_store=...)` they construct via a `make_test_app(...)` helper in `conftest.py` that wires a one-experiment `StoreFactory` with the same in-memory store. The change is mechanical but touches a lot of files (notably `conftest.py`, `test_admin_routes.py`, `test_evaluator_flow.py`, `test_executor_routes.py`, `test_ideator_flow.py`, `test_security_invariants.py` all call `make_app(...)` directly). The plan-§5 wave shape isolates this churn to one wave.
- New `tests/test_per_experiment_repo.py`: executor module's repo materialization under multi-experiment posture.

Documentation:

- [`docs/glossary.md`](../glossary.md): add `active_experiment_id`, `StoreFactory`, `active_store` per §1's naming.
- [`docs/user-guide.md`](../user-guide.md) §5 (web-ui walkthrough): update "select an experiment" section to reflect that selection now changes data, not just the label.
- [`docs/operations/`](../operations/): one new doc `web-ui-multi-experiment.md` explaining the credential bootstrap, persisted-credential dir, and the admin-token requirement.
- CHANGELOG: removed the 12c "per-route swap deferred" footer note from the index of deferrals; new chunk entry documents the closure.

Compose / setup:

- `reference/compose/compose.yaml` web-ui service: add a bind-mount for `${EDEN_EXPERIMENT_DATA_ROOT}/web-ui-credentials/` and an `EDEN_CREDENTIAL_DIR=/var/lib/eden/web-ui-credentials` env var.
- `reference/compose/.env.example`: document the new var.
- `reference/scripts/setup-experiment/setup-experiment.sh`: ensure the `web-ui-credentials` dir exists with `chmod 0777` (same posture as the per-host credentials volumes in 12a-1g §D.3).
- No new smoke; the existing `compose-smoke.sh` runs single-experiment and remains the golden path. Multi-experiment smoke is 12c's deferred follow-up (separate issue).

### 4.2 Cross-references to followups

- **#128 (id/name rename).** Eventually re-targets all the wire-call sites this plan rewrites; mechanical retro-fit when it lands. No protocol-level conflict.
- **#140 (operator-as-worker).** Replaces the JIT service-worker bootstrap in §3.2 with operator-session-carried bearers. The `BearerCache` interface is preserved.
- **#141 (deployment-level workers).** Collapses the per-experiment credential dir to a single deployment credential.
- **`GET /v0/experiments/{E}/config` wire endpoint** (the alternative considered in Decision 6). Replace the on-disk `--experiment-config-dir` with a wire read. Spec change (Chapter 7 + JSON schemas + Pydantic models + conformance citation). File a new issue at this plan's W1 alongside this plan's first PR so future maintainers see the cleaner shape on the roadmap.
- **Multi-experiment Compose smoke.** 12c §3.8 deferred; a separate issue (TBD — file as part of this plan's first wave) covers it.
- **Tab-scoped experiment selection** (Decision 5 / §3.6 reject-on-switch UX). v1 affordance.

### 4.3 Out of scope

- All wire / spec / schema / Pydantic-model changes (see §3.10).
- Multi-tab parallel experiments (Decision 5).
- Operator-as-worker rework (#140).
- Deployment-level worker registry (#141).
- ID / name rename (#128).
- Conformance scenarios (Decision 11; per chapter 9 §6 IUT contract).
- Compose multi-experiment smoke (existing 12c follow-up).
- Per-experiment task-store-server URLs (12c Decision 11 fixes one URL deployment-wide).
- "Save draft on experiment switch" UX (§3.6 takes the simpler reject-and-redirect path).

### 4.4 Non-goals

- Improving the no-control-plane single-experiment deployment. That posture is preserved unchanged; this plan is purely additive for the control-plane-enabled case.
- Optimizing the cross-experiment dashboard's N-call pattern (12c §7.4). Out of scope; revisit when N hits real scale.
- Renaming "experiment_id" or restructuring URLs (Decision 5 reject; §3.9 alternative).
- Changing the bearer / auth model.

## 5. Files to touch

| File | Change | Why |
|---|---|---|
| `reference/services/web-ui/src/eden_web_ui/store_factory.py` (new) | `StoreFactory`, `BearerCache`, JIT bootstrap | §3.3 |
| `reference/services/web-ui/src/eden_web_ui/credentials.py` (new) | Persisted-credential read/write — delegates to `eden_service_common.auth.bootstrap_worker_credential` per-experiment; does NOT reimplement the lock or idempotent-register-then-reissue branches | §3.2 |
| `reference/services/web-ui/src/eden_web_ui/routes/_helpers.py` | Add `resolve_active_experiment`, `active_config`, exception types | §3.1 / §3.4 |
| `reference/services/web-ui/src/eden_web_ui/app.py` | Replace `store`/`admin_store`/`experiment_config` parameters with `store_factory`; templates read `experiment_id` from request context | §3.3 |
| `reference/services/web-ui/src/eden_web_ui/cli.py` | Build `StoreFactory`; add `--credential-dir`; per-experiment repo materialization | §3.3 / §3.5 |
| `reference/services/web-ui/src/eden_web_ui/routes/ideator.py` | resolve → factory pattern on every handler; `form_experiment_id` hidden field on mutating forms | §3.1 / §3.6 |
| `reference/services/web-ui/src/eden_web_ui/routes/executor.py` | Same + per-experiment repo | §3.1 / §3.5 / §3.6 |
| `reference/services/web-ui/src/eden_web_ui/routes/evaluator.py` | Same | §3.1 / §3.6 |
| `reference/services/web-ui/src/eden_web_ui/routes/admin/observability.py` | resolve → factory pattern; both worker + admin views | §3.1 |
| `reference/services/web-ui/src/eden_web_ui/routes/admin/actions.py` | Same; `actor` attribution stays as `app.state.worker_id` until #140 | §3.1 / §3.2 |
| `reference/services/web-ui/src/eden_web_ui/routes/admin/work_refs.py` | Same | §3.1 |
| `reference/services/web-ui/src/eden_web_ui/routes/admin/index.py` | Same | §3.1 |
| `reference/services/web-ui/src/eden_web_ui/routes/admin_workers.py` | Same; admin store factory lookup | §3.1 |
| `reference/services/web-ui/src/eden_web_ui/routes/admin_groups.py` | Same | §3.1 |
| `reference/services/web-ui/src/eden_web_ui/routes/admin_artifacts.py` | Same | §3.1 |
| `reference/services/web-ui/src/eden_web_ui/routes/artifacts.py` | Same | §3.1 |
| `reference/services/web-ui/src/eden_web_ui/routes/index.py` | Read active experiment for header rendering | §3.1 |
| `reference/services/web-ui/src/eden_web_ui/routes/_lineage.py` | Functions taking `store` already do; ensure callers pass per-experiment store | §3.1 |
| `reference/services/web-ui/src/eden_web_ui/routes/_submit_readback.py` | Same | §3.1 |
| `reference/services/web-ui/src/eden_web_ui/routes/admin_experiments.py` | Remove v0-limitation footer copy; the dashboard's "select" CTA stays | Decision 12 |
| `reference/services/web-ui/src/eden_web_ui/templates/base.html` | Top-nav switcher dropdown (the existing top-nav lives here, not in `_layout.html` — there is no `_layout.html`) | §3.7 |
| `reference/services/web-ui/src/eden_web_ui/templates/admin_experiments.html` | Remove v0-limitation footer; "switch" CTA unchanged | Decision 12 |
| `reference/services/web-ui/src/eden_web_ui/templates/*_form.html` | Hidden `form_experiment_id` field | §3.6 |
| `reference/services/web-ui/tests/test_store_factory.py` (new) | Factory + bearer-cache + JIT-bootstrap coverage | §6.1 |
| `reference/services/web-ui/tests/test_resolve_active.py` (new) | Resolve helper + redirect coverage | §6.1 |
| `reference/services/web-ui/tests/test_per_experiment_repo.py` (new) | Per-experiment repo materialization | §6.1 |
| `reference/services/web-ui/tests/test_experiment_switcher.py` (existing) | Extend with full round-trip + switch-mid-form rejection | §6.2 |
| `reference/services/web-ui/tests/conftest.py` (existing) | Shared fixture rewrite: `app.state.store_factory` instead of `app.state.store` | §6.3 |
| `reference/services/web-ui/tests/test_*.py` (existing ~28 files) | Mechanical fixture update; one shape per file | §6.3 |
| `reference/compose/compose.yaml` | web-ui service: bind-mount `web-ui-credentials` dir; `EDEN_CREDENTIAL_DIR` env | §4.1 |
| `reference/compose/.env.example` | Document `EDEN_CREDENTIAL_DIR` | §4.1 |
| `reference/scripts/setup-experiment/setup-experiment.sh` | `mkdir -p` + `chmod 0777` for `web-ui-credentials/` | §4.1 |
| `docs/glossary.md` | Add `active_experiment_id`, `StoreFactory`, `active_store` | §1 |
| `docs/user-guide.md` | §5 web-ui walkthrough: selection now changes data | §4.1 |
| `docs/operations/web-ui-multi-experiment.md` (new) | Credential bootstrap; admin-token requirement | §4.1 |
| `CHANGELOG.md` | Remove 12c per-route-swap deferral from index; new chunk entry | §4.1 |

## 6. Test design

### 6.1 Unit tests (per-module)

`test_store_factory.py`:

- Cache hit / miss / eviction shape.
- `for_experiment(X, role="worker")` returns the same instance across two calls.
- `for_experiment(X, role="admin")` returns a different instance than the `role="worker"` one.
- Persisted-credential read: write a `.cred` file, construct factory, assert no JIT register call.
- JIT register: empty credential dir, factory + admin token, assert one `register_worker` call to Y's registry, assert persisted file on disk.
- Failure modes from §3.2's table: stale selection (404 on `read_experiment(Y)`), missing admin token (no JIT path possible), control-plane unreachable.
- Shared `httpx.Client` lifecycle: factory.close() closes the underlying client; per-experiment StoreClient `close()` does NOT (they don't own the client).

`test_resolve_active.py`:

- Session has `selected_experiment_id` → returns it.
- Session has no field → returns `app.state.experiment_id`.
- Session points to nonexistent experiment → raises `StaleSelection`.
- Control plane unreachable → raises `ControlPlaneUnreachable`.
- `app.state.control_plane is None` → always returns deployment default, no validation call.

`test_per_experiment_repo.py`:

- First access materializes a new bare clone under `<repo-path>/<experiment_id>.git/`.
- Second access reuses the cached `GitRepo`.
- `--forgejo-url` substitution: configured URL `…/<org>/<startup-exp>.git` rewrites to `…/<org>/Y.git` when active is Y.
- Pre-existing bare repo at the target path: opens, doesn't re-clone.

### 6.2 Cross-request flow tests

`test_experiment_switcher.py` (extended):

- Sign in with deployment-default selected → navigate to ideator → see X's data.
- POST `/admin/experiments/Y/select` → navigate to ideator → see Y's data.
- Click "back to dashboard" → row for Y has "is_selected" set.
- Stale selection: unregister Y while session points to Y → next request redirects with `error=stale-selection`.
- Switch-mid-form: render ideator form (X), switch to Y, submit the form → `error=switched-mid-form&from=X&to=Y`; no store write happened.
- Top-nav switcher widget renders the dropdown and the active row is highlighted.
- No-control-plane posture: switcher widget is absent; resolve always returns deployment default.

### 6.3 Existing-test fixture sweep

The mechanical fixture rewrite is the single largest chunk of churn. The plan-§5 wave-3 isolates it. Specifically:

- Today's fixture sets `app.state.store = mock_store_instance`.
- After: `app.state.store_factory = _one_experiment_factory(experiment_id, store=mock_store_instance)`.
- A helper `_one_experiment_factory` lives in `tests/conftest.py`; every existing test only changes the fixture call.

If any test reads `app.state.store` directly (rather than via the test client), it gets updated to read `app.state.store_factory.for_experiment(args.experiment_id)`.

### 6.4 Verification gates

Before merge:

- `uv run ruff check .` clean.
- `uv run pyright` 0 errors.
- `uv run pytest -q` (full suite) passes.
- `uv run pytest -q reference/services/web-ui/tests/` passes (the bulk of new + churned tests).
- `uv run pytest -q conformance/` passes (no new scenarios; ensure none regress).
- `python3 scripts/check-rename-discipline.py` clean.
- `python3 scripts/spec-xref-check.py` clean (no spec edits, but the script run is cheap insurance against accidental edits).
- `python3 scripts/check-complexity.py` clean (the refactor adds modules and per-handler resolve calls; size growth is small but worth checking).
- `bash reference/compose/healthcheck/smoke.sh` passes (single-experiment posture must remain green).
- `bash reference/compose/healthcheck/smoke-subprocess.sh` passes.
- Markdownlint clean.
- Manual verification (single-experiment): existing single-experiment Compose deployment behaves identically (no control plane, no switcher widget, no per-request resolve overhead visible).
- Manual verification (multi-experiment): bring up the stack with `--control-plane-url`, register 2 experiments, switch between them, navigate each per-experiment page, observe that data follows the selection.

## 7. Tricky areas

### 7.1 Credential bootstrap fail-loud vs fail-soft

The JIT register path in §3.2 needs the deployment admin token. Common operator misconfiguration: admin token set on the task-store-server, not propagated to the web-ui. Without it, every "switch to a new experiment" fails with `MissingAdminToken`. Mitigation:

- The CLI's startup probe in `_build_runtime` already validates the admin bearer (`admin_store.list_workers()`). Extend it to also flag "no admin token; per-route swap will not be able to bootstrap new credentials" as a startup warning (log line, NOT a fail-startup error — the no-admin-token posture has to keep working for single-experiment deployments).
- The dashboard's "switch" CTA can probe credential availability and disable the CTA with a tooltip when the target experiment has no cached / persisted credential AND no admin token is set.
- Document the failure mode in `docs/operations/web-ui-multi-experiment.md`.

### 7.2 Switching during a long-running form (executor workspace, evaluator draft)

The executor flow keeps a draft on disk; switching experiments mid-draft strands work that operators care about. §3.6's reject-and-redirect is the v0 posture, but the draft's path includes the experiment_id, so the operator can manually navigate back. Documented in the user guide. A "drafts move with you" or "save draft, switch, come back" affordance is v1 (#TBD followup).

### 7.3 The `experiment_id` template global

Today `templates.env.globals["experiment_id"] = experiment_id` is set once at app construction. The plan moves this to a per-request context variable so every template render reflects the active experiment. The risk: any template that captures `experiment_id` outside a request scope (e.g., test helpers) gets the wrong value. Mitigation: grep for `experiment_id` template usage and audit; the existing usage sites are exclusively per-request renders.

### 7.4 Per-experiment repo storage growth

The executor module clones one bare repo per experiment. In a deployment with 100 experiments, that's 100 clones. Each is bounded by the integrator repo size (typically <100 MB for the EDEN reference workloads), so 100 experiments → ~10 GB. Acceptable for v0; a future improvement is shallow clones or on-demand prune-after-LRU.

### 7.5 Permalinks to specific experiments

A user shares a URL `/ideator/draft/<id>` with a colleague. The colleague's session points to a different experiment; the link 404s (the idea_id doesn't exist in their experiment). Mitigation: a v1 query param override `?exp=<E>` that overrides the session selection for that one request (documented in §3.9 / §4.2 as v1). v0 is "share with caveat: tell your colleague which experiment to switch to first."

### 7.6 Test fixture churn risk

~28 test files getting a fixture rewrite is the kind of mechanical change where a wrong shared-helper signature ripples through everything and confuses code review. Mitigation: the fixture rewrite is its own wave (plan-§8 W3), gets its own PR, and is reviewed independently of the per-handler refactor. The single-experiment behavior must be observably unchanged by W3 alone (no per-route swap yet; just the fixture shape).

### 7.7 Compose multi-experiment posture

Today the Compose stack assumes one experiment (one `--experiment-id` per stack). Running multiple experiments through one Compose web-ui requires either (a) using the control plane to register the additional experiments + having the web-ui's admin token bootstrap each, OR (b) running multiple Compose stacks. v0 supports (a) as the path-of-intent; (b) remains operationally valid. Documented.

## 8. Sequencing

Recommended PR shape (in order):

1. **W1: factory + helpers PR.** Introduce `StoreFactory`, `BearerCache`, `credentials.py`, `resolve_active_experiment`, `active_config`. Wire them into `make_app` and `cli.py`, but keep `app.state.store` / `app.state.admin_store` / `app.state.experiment_config` as thin aliases (preserved). Existing routes untouched. Verifies: existing tests pass unchanged; new factory/helper tests pass. Single-experiment posture observable indistinguishable from today.
2. **W2: per-handler refactor PR.** Migrate every route handler to `resolve_active_experiment(request)` + `store_factory.for_experiment(...)`. Drop the `app.state.store` / `admin_store` / `experiment_config` aliases at the end. Existing tests still hit the same code paths (because the fixture still injects a one-experiment factory in W3); they all stay green.
3. **W3: test-fixture sweep PR.** Mechanical fixture rewrite across ~28 files. Tests still green; no functional change.
4. **W4: switcher + form-mismatch PR.** Add top-nav dropdown, `form_experiment_id` hidden field on mutating forms, switch-mid-form rejection. New flow tests.
5. **W5: per-experiment repo PR.** Materialize per-experiment bare clones; per-request fetch. New repo tests.
6. **W6: docs PR.** Glossary, user guide, operations doc, CHANGELOG entry; remove the v0-limitation footer from the dashboard; flip the roadmap pointer for this issue. Includes the codex-review impl record under `docs/plans/review/issue-145/impl/<timestamp>/`.

Per-PR review gates: each PR must pass `uv run pytest -q`, `uv run ruff check .`, `uv run pyright`, markdownlint, and the Compose smokes the change could plausibly affect (the smokes run single-experiment, so they must stay green through every wave).

A reviewer going from W1 → W6 should see: W1 lands quiet (no operator-visible change). W2 lands quiet. W3 lands quiet. W4 makes the switcher functional. W5 fixes the executor multi-experiment story. W6 closes the loop.

Single-PR landing is also viable for an operator who wants to ship the whole thing atomically — the wave shape is a review-friendliness hint, not a hard sequencing constraint. The codex-review pass at the impl stage is the gate that calls the shot.

## 9. Estimated effort

- W1 (factory + helpers): ~1.5 days. Most of the heavy lifting is in `StoreFactory` + bearer cache + persisted-credential round-trip + the failure-mode tests.
- W2 (per-handler refactor): ~2 days. Mechanical but spans 14 modules; the resolve pattern needs to be applied uniformly.
- W3 (fixture sweep): ~1 day. Truly mechanical.
- W4 (switcher + mismatch): ~1 day.
- W5 (per-experiment repo): ~0.5 day.
- W6 (docs + record + roadmap flip): ~0.5 day.

**Realistic total: ~6 working days.** The issue's estimate was "Medium. ~1 week. ~12 routes × small handler edit + tests. Plus the StoreClient-by-selected-experiment plumbing." — this plan lands in the same range. The credential-bootstrap path (§3.2) is the discovery cost on top of the issue's "~12 routes × small edit" framing.

## 10. Risks

1. **Credential-bootstrap path silently produces wrong attribution.** If the JIT register call mints a `web-ui` worker in experiment Y with the deployment-default `worker_id`, but the operator expects per-operator attribution, every action attributes to `web-ui-1`. This is identical to today's behavior for the deployment-default experiment, so the regression risk is zero — but the design choice carries forward an attribution debt that #140 closes. The plan calls this out so reviewers don't expect this chunk to fix it.

2. **Per-experiment credential dir + crash mid-bootstrap.** The JIT register call commits to Y's registry, then writes the credential file. A crash between the two leaves the credential dangling (registered, persisted-empty); next start re-registers (12a-1 §D.1 says `register_worker` is idempotent on the existing record). The retry is correctness-equivalent; documented.

3. **Switcher dropdown's `list_experiments` call hammers the control plane.** Mitigation: 5s in-process TTL cache per §3.7. Worst case: 1 call per 5s per process, regardless of request rate.

4. **The deployment-default fallback hides the no-selection case from operators.** When the operator clicks ideator on a fresh session, they see X's data — without a clear "you're looking at the deployment default" cue. Mitigation: the top-nav widget shows `Default: <X>` in that state (vs `Active: <Y>` after selection). Visually distinct.

5. **Cross-experiment leakage in cached state.** A handler that pre-resolves an `experiment_id` and then passes it down through `_lineage.py` / `_submit_readback.py` must keep that id consistent; if any helper re-reads `app.state.experiment_id` it produces wrong cross-experiment data. Mitigation: the test-fixture sweep includes a one-shot grep for `app.state.experiment_id` in all of `routes/`; any remaining hit is a bug. The W2 PR drops `app.state.experiment_id` from `make_app(...)` parameter shape so a stale reference fails type-check.

6. **CSRF token cross-experiment validity.** §3.6's reject-on-mismatch handles this for forms; a non-form-based htmx mutation that POSTs without rendering a fresh page first could miss the check. Mitigation: every mutating route's CSRF check is paired with a `form_experiment_id` check OR an `htmx` request-time active-experiment re-resolve that compares to the form's hidden field. The W4 PR includes a sweep that asserts every mutating route has one of these two posture.

7. **`--experiment-config` semantic drift.** Today the YAML at `--experiment-config` is the authoritative config for the only experiment the web-ui serves; routes read it directly. Under this plan, that YAML is the **fallback** for the deployment-default experiment only; non-default experiments fetch their config from the task-store-server. The risk: operators who manually edited the YAML expect their edits to apply to all experiments. Mitigation: the CLI help line for `--experiment-config` is updated to call this out explicitly; the user guide §5 names it; the no-control-plane posture is unchanged (YAML drives everything).

8. **The 12c-shipped v0-limitation footer copy is operator-facing.** Removing it without removing the underlying limitation would be embarrassing; shipping the underlying fix without removing the footer would confuse operators. Mitigation: the W6 PR ties the footer removal to the rest of the chunk's deliverables and the CHANGELOG entry; gated by code review.

9. **Naming collision between `active_experiment_id` and 12c-shipped `selected_experiment_id`.** They're not the same: `selected_experiment_id` is the session field (may be None); `active_experiment_id` is the per-request resolved value (always a string, falling back to deployment default). The plan calls the distinction out in §1 / glossary so reviewers don't conflate them.

10. **Test fixture rewrite churn.** 43 files touched in W3; merge conflicts likely if any concurrent web-ui work is in flight. Mitigation: schedule W3 with no overlapping web-ui PRs in the queue.

11. **Misclassifying registered-but-unseeded experiments as stale.** Decision 8's three-state model handles this, but the code paths are easy to get wrong: a control-plane `read_experiment_metadata` 200 + task-store-server `read_experiment_state` 404 is a real state that the codebase hits today the moment an operator clicks "Register" on the dashboard. Mitigation: explicit fixture-driven tests in `test_resolve_active.py` for all three states; the unseeded-state render-path is tested per-route in W2 (a "looks empty but here's why" banner is a small but observable UI element on every per-experiment page).

12. **Per-experiment config-dir drift from the task-store-server's internal `experiment_config` text.** The task-store-server stores `experiment_config` text at experiment-creation time (currently only via checkpoint import). The on-disk config dir (Decision 6) is a separate source; if operators hand-edit one but not the other, the web-ui's view of the experiment's objective / evaluation schema diverges from the worker hosts' view. Mitigation: document the divergence in `docs/operations/web-ui-multi-experiment.md`; the wire-endpoint follow-up (§4.2) closes it permanently. v0 accepts the divergence with documentation; v1 (the wire endpoint) makes it impossible by construction.

13. **Control-plane-scoped web-ui worker registration vs. 12c's authority model.** The plan adds a deployment-scoped web-ui worker (§3.2 Posture B/C) but the existing control-plane authority table (12c plan §5.1) defines `list_experiments` / `read_experiment_metadata` as callable by any "deployment-scoped worker OR admin-token". The plan reuses that existing authority pattern — no new wire surface. But: bootstrapping that worker uses admin-token-gated `POST /v0/control/workers` (also 12c §5.1). Operators who skip bootstrap end up in Posture D (switcher hidden). Mitigation: the startup flow logs a clear warning when control-plane URL is set but no admin token AND no persisted control-plane-scoped credential is available.

## 11. Followups (out of scope)

- **#128, #140, #141 retrofits** — recorded in §1.4 / §4.2. Each is its own plan.
- **Tab-scoped experiment override** (`?exp=<E>`). v1.
- **Drafts move with operator on experiment switch.** v1 UX improvement.
- **Multi-experiment Compose smoke** (12c §3.8 deferred). Separate issue.
- **Cross-experiment views inside per-experiment pages.** Out of scope; cross-experiment dashboard owns this.
- **Per-tab experiment selection.** Out of scope per Decision 5.
- **Permalinks across experiments.** §7.5 sketches the v1 affordance.

## 12. What lands at the end of this chunk

After all waves merge:

- Operators can register N experiments in the control plane and switch between them in the web-ui; every per-experiment page renders data scoped to the active selection.
- The `Session.selected_experiment_id` field becomes load-bearing (was nominal in 12c).
- The dashboard's v0-limitation footer is removed.
- Single-experiment Compose deployments are observably unchanged.
- The deferral footer in CHANGELOG.md (line 188) is rewritten to "shipped issue #145" and the roadmap pointer flips.

The multi-experiment usability story closes: the cross-experiment dashboard (12c) + cross-experiment selection (12c) + per-route data scoping (this plan) compose to deliver the full multi-experiment web-ui surface that 12c promised.
