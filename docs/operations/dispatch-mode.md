# Dispatch-mode operator playbook

`dispatch_mode` is a per-experiment object with four keys, each `"auto"`
(default) or `"manual"`. When a key is `"manual"`, every auto-orchestrator
instance MUST refrain from running that decision — the decision is
reserved for admins-group operators driving the same wire op.

## The four keys

| Key | Gates | When to flip to manual |
|---|---|---|
| `ideation_creation` | Auto-orchestrator's continuous ideation-task creation policy | Paused while you review in-flight variants; sweeping in a corpus of human-authored ideas without competing with auto-generated ones. |
| `execution_dispatch` | Auto-orchestrator creating one `kind=execution` task per ready idea | An idea is queued but you want to inspect parent-commit reachability before dispatch; routing a specific idea to a specific executor (combine with reassign in 12a-3+). |
| `evaluation_dispatch` | Auto-orchestrator creating one `kind=evaluation` task per starting-variant-with-commit_sha | You want a human gate on evaluation (e.g., experimental evaluator that needs human pre-check); manual triage of specific variants. |
| `integration` | Auto-orchestrator invoking the integrator on success variants | Pausing integration during a deploy / repo migration; verifying squash shape on the first few variants before committing to the integration pipeline. |

Flipping a key from `auto` to `manual` does **not** abort in-flight
decisions (chapter 04 §7.3). Flipping back to `auto` resumes at the
next iteration; the orchestrator catches up on whatever the manual
path left for it.

## Flipping via the web UI

1. Sign in as an `admins`-group operator (see
   [initial-admin-credential.md](initial-admin-credential.md) if you
   need to mint one).
2. Navigate to `/admin/dispatch-mode/`.
3. Click the radio button to switch the key's value. Submit.
4. The page redirects to itself with `?dispatched=ok` and re-renders
   reflecting the new state. The wire-side `experiment.dispatch_mode_changed`
   event carries the `changed` diff + `updated_by` stamped from your
   bearer.

A flip that doesn't actually change any value (every key already matches)
lands `?dispatched=no-change` and emits NO event. The form submits all
four keys every time (HTML radio groups can't omit a key); the wave-2
partial-merge semantics no-op the unchanged keys.

## Flipping via the wire (curl)

```bash
curl -fsS \
  -X PATCH \
  -H "Authorization: Bearer <admin-worker-id>:<worker-secret>" \
  -H "X-Eden-Experiment-Id: <experiment-id>" \
  -H "Content-Type: application/json" \
  -d '{"evaluation_dispatch":"manual"}' \
  http://task-store-server:8080/v0/experiments/<experiment-id>/dispatch_mode
```

The bearer is your worker credential (the admin worker is a regular
registered worker that happens to be a member of `admins` — the
deployment-admin bearer `admin:<EDEN_ADMIN_TOKEN>` is **NOT** accepted
on this endpoint; the server returns 403 forbidden).

Response: 200 with the full post-update state. Read-only companion
endpoint is `GET .../dispatch_mode`.

## What "manual" actually does

The auto-orchestrator's loop skips the gated branch. The decision is
reserved for an admins-group caller using the same wire op:

- `manual ideation_creation` → operator drives `POST /v0/.../tasks`
  with `kind=ideation` (chapter 04 §2.1).
- `manual execution_dispatch` → today, the spec reserves
  `kind=execution` creation to the `orchestrators` group only; the
  operator path lands in 12a-3 alongside `intended_executor`.
- `manual evaluation_dispatch` → operator drives `POST /v0/.../tasks`
  with `kind=evaluation` and `payload.variant_id` set.
- `manual integration` → operator drives `POST /v0/.../variants/{T}/integrate`
  with the chosen `variant_commit_sha`.

## Common scenarios

**Pause the experiment.** Flip `execution_dispatch`, `evaluation_dispatch`,
and `integration` all to manual. New ideas can still be drafted (or
flip `ideation_creation` too if you want a full freeze), but no new
work flows downstream.

**Pause integration during a deploy.** Flip only `integration` to manual.
Variants still get evaluated; `success` variants pile up at
`variant_commit_sha=null` until you flip back. When you flip back, the
orchestrator catches up on the backlog.

**Manual evaluator triage.** Flip `evaluation_dispatch` to manual. The
orchestrator stops creating evaluation tasks; you create them yourself
with `target` set to whichever evaluator (or group) you want.

## Spec references

- Chapter 02 §2.5 — `dispatch_mode` field shape.
- Chapter 03 §6.1 — orchestrator MUST observe the field; §6.5 — manual
  delegation.
- Chapter 04 §7 — `update_dispatch_mode` op.
- Chapter 05 §3.4 — `experiment.dispatch_mode_changed` event payload.
- Chapter 07 §2.8 — wire endpoint.
