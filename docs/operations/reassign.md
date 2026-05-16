# Reassignment operator playbook

`reassign_task` is the operator's mechanism for updating a task's
`target` field after the orchestrator (or an admin) created it.
Authority: caller in `admins`.

## When to reassign

- **A worker host went offline mid-claim.** The task's `claim` field
  still points at the dead worker; the claim eventually expires and
  the sweeper reclaims, but reassign accelerates the recovery and
  lets you also route the task elsewhere.
- **Route a specific task to a specific worker / group.** The
  orchestrator dispatches with `target=null` (no constraint); a
  human operator may want to scope a task to a particular evaluator
  or admin group.
- **Manual triage.** The task is in a state where the operator wants
  to grab it back from whichever worker holds the claim and route
  it differently — typically combined with a re-decide on what to
  do with the variant the original claimant produced.

## Semantics (chapter 04 §6)

| Task state | What reassign does |
|---|---|
| `pending` | Atomic field update + single `task.reassigned` event. Same-target reassign is a no-op (no event emitted, surfaced to the operator as `?reassigned=no-change`). |
| `claimed` | Composite-commit: `task.reclaimed(cause=operator)` + `task.reassigned` + (for execution tasks with an in-flight starting variant) `variant.errored`. The claimant's claim is invalidated; the task returns to `pending`; any orphan starting variant transitions to `error`. |
| `submitted` / `completed` / `failed` | 409 `eden://error/invalid-precondition` with no partial state. Reassign is not permitted past the claimed phase to preserve attribution contracts (`submitted_by` survives terminal transitions; a post-submit reassign would either rewrite history or split attribution). |

## Reassigning via the web UI

1. Sign in as an `admins`-group operator.
2. Navigate to `/admin/tasks/`, find the task, click into its detail
   page. The "operator reassign" section is visible when the task is
   in `pending` or `claimed`.
3. Click "reassign this task →". The form offers three target options:
   - `none` — any registered worker (clears the target).
   - `worker:<id>` — scope to one specific worker. Use the dropdown
     populated from the registry, or paste an id in the manual field.
   - `group:<id>` — scope to a group (e.g. `humans`). Same dropdown
     populated from `list_groups`.
4. Enter a `reason` (free-form audit text — suggested values:
   `operator`, `failed_worker`, `misrouted`; the spec doesn't
   enumerate the set).
5. Submit. The page redirects with `?reassigned=ok` on success or
   one of the error banners (`invalid-target` / `missing-reason` /
   `unknown-target` / `illegal-state` / `transport`).

## Reassigning via the wire (curl)

```bash
curl -fsS \
  -X POST \
  -H "Authorization: Bearer <admin-worker-id>:<worker-secret>" \
  -H "X-Eden-Experiment-Id: <experiment-id>" \
  -H "Content-Type: application/json" \
  -d '{"new_target":{"kind":"worker","id":"alice"},"reason":"failed_worker"}' \
  http://task-store-server:8080/v0/experiments/<experiment-id>/tasks/<task-id>/reassign
```

To clear the target (open the task to any registered worker), send
`"new_target": null`:

```bash
-d '{"new_target":null,"reason":"open up"}'
```

`reassigned_by` is stamped server-side from the bearer; the body MUST
NOT carry it (the schema rejects).

## What to expect downstream

After a successful reassign on a `claimed` execution task, the variant
the previous claimant was working on is in `error`. The next worker
who claims the task starts fresh on a new variant. The original
variant's `executed_by` reflects the previous claimant (attribution
is preserved on the error path per chapter 02 §9).

For ideation / evaluation tasks the reassign-on-claimed path emits
the composite `task.reclaimed` + `task.reassigned` but no variant-side
write — those tasks don't produce variants.

## Spec references

- Chapter 04 §6 — `reassign_task` op + composite-commit semantics.
- Chapter 05 §3.1 — `task.reassigned` event payload.
- Chapter 07 §2.7 — wire endpoint.
