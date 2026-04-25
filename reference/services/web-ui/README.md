# eden-web-ui

Reference Web UI service for the EDEN protocol. Phase 9 chunk 1
delivers the UI shell plus the planner module — enough surface for a
human to play one role end-to-end through a browser.

The UI service is a **backend-for-frontend (BFF)**: it holds the
`--shared-token` (the chapter 07 §12 reference bearer the
orchestrator and worker hosts already use), runs `eden_wire.StoreClient`
in-process to talk to the task-store-server, and exposes only
server-rendered HTML to the browser. The browser never sees the
shared token; it gets a signed session cookie.

## Run locally

```bash
python3 -m eden_task_store_server \
    --db-path /tmp/eden.sqlite \
    --experiment-id exp-1 \
    --experiment-config tests/fixtures/experiment/.eden/config.yaml \
    --port 0 \
    --shared-token devtoken \
    &

# (Read EDEN_TASK_STORE_LISTENING from stdout to learn the port.)

python3 -m eden_web_ui \
    --task-store-url http://127.0.0.1:<port> \
    --experiment-id exp-1 \
    --experiment-config tests/fixtures/experiment/.eden/config.yaml \
    --shared-token devtoken \
    --session-secret "$(openssl rand -hex 32)" \
    --artifacts-dir /tmp/eden-artifacts \
    --port 0
```

The web-ui announces `EDEN_WEB_UI_LISTENING host=... port=...` on stdout
on bind so harnesses (and the test suite) can discover the ephemeral
port without scraping logs.

## Auth model

- The session cookie holds `{worker_id, csrf}` and is signed with
  `--session-secret` via `itsdangerous`.
- Cookie attributes: `HttpOnly`, `SameSite=Lax`, `Path=/`. `Secure`
  is opt-in via `--secure-cookies` (use behind TLS).
- Every mutating route validates a `csrf_token` form field in
  constant time. The cookie's `SameSite=Lax` is **not** treated as
  sufficient on its own.
- The shared bearer never reaches the browser, the rendered HTML,
  any session cookie, or any structured log line.

## Stranded-claim recovery

Every UI claim sets `expires_at = now + --claim-ttl-seconds`
(default 1 hour). The orchestrator service runs
`eden_dispatch.sweep_expired_claims` once per iteration so claims
abandoned by closing the tab are reclaimed automatically — no
operator action required.

## Planner submit flow

The planner module pins three phases:

1. **Phase 1 — drafting.** For every proposal: write rationale
   markdown to `<artifacts-dir>/<proposal_id>.md` (atomically, via
   tmp-and-rename), build a `file://` URI, then call
   `store.create_proposal(state="drafting")`. Drafting proposals
   are invisible to the orchestrator's dispatch path.
2. **Phase 2 — ready.** Loop over the just-created proposals and
   call `store.mark_proposal_ready` for each.
3. **Phase 3 — submit.** `store.submit(...)` with retry-before-orphan
   on transport-shaped failures (3 attempts, exponential backoff,
   leveraging chapter 07 §2.4 / §8.1 idempotent resubmit). On a
   definitive divergent response or after the retries are
   exhausted, the orphaned-proposals error page lists the
   `ready`-but-unreferenced proposal IDs for operator recovery.

The narrowest unsafe window is between Phase 2 and Phase 3:
proposals are `ready` but not yet referenced by a submitted plan
task. We accept this for the reference impl; it applies equally
to the existing scripted planner host. A spec-level fix (atomic
ready-and-submit) is out of scope for chunk 1.

## What this chunk does **not** ship

- Implementer / evaluator role modules (9c / 9d).
- Observability views, admin-reclaim button (9e).
- Multi-experiment switcher (Phase 12).
- Per-user authentication (Milestone 3).
- Compose / Dockerization (Phase 10).
