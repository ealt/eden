# Experiment lifecycle operator playbook

Covers the 12a-3 lifecycle surface: the `running → terminated` state
transition, the orchestrator's termination policy, and the
operator-driven `/terminate` wire op.

For the underlying protocol semantics see
[`spec/v0/02-data-model.md`](../../spec/v0/02-data-model.md) §2.5
(experiment lifecycle state), [`spec/v0/03-roles.md`](../../spec/v0/03-roles.md)
§6.2 decision-type 0 (termination decision) +
[`spec/v0/04-task-protocol.md`](../../spec/v0/04-task-protocol.md) §8
(terminate_experiment op).

## Two ways to terminate

Both routes commit through the same Store-level
`terminate_experiment` composite op, so they collapse onto a single
observable `experiment.terminated` event per
`03-roles.md` §6.4. Pick whichever fits the workflow.

### Operator-driven (admin wire op)

Use this when the operator has decided the experiment is done — a
"call the experiment" moment, not a policy decision.

```bash
# Admin bearer for the deployment (see docs/operations/initial-admin-credential.md).
curl -X POST \
    -H "X-Eden-Experiment-Id: $EDEN_EXPERIMENT_ID" \
    -H "Authorization: Bearer <admin-eric>:<bearer-token>" \
    -H 'Content-Type: application/json' \
    -d '{"reason": "objective achieved; calling it"}' \
    http://localhost:8080/v0/experiments/$EDEN_EXPERIMENT_ID/terminate
```

Web UI equivalent: navigate to `/admin/experiment/`, type a reason,
click "terminate". The form short-circuits with
`?error=admin-disabled` when `--admin-token` is not configured.

### Policy-driven (orchestrator decision-type 0)

Use this when termination is a function of experiment state —
"terminate when N variants attempted", "terminate when score crosses
0.95", etc. The orchestrator consults the experiment-config-supplied
`termination_policy` once per iteration when
`dispatch_mode.termination == "auto"`.

`dispatch_mode.termination` defaults to `"manual"` so pre-12a-3
deployments are unchanged. To enable, set both keys in the experiment
config **before** bringing the stack up — the single-experiment
orchestrator reads `termination_policy` from the experiment-config YAML
at startup (issue #157 retired the `--termination-policy` CLI flag /
`EDEN_TERMINATION_POLICY` env var for this path):

```yaml
# experiment-config.yaml
dispatch_mode:
  termination: auto          # required for termination_policy to be consulted
termination_policy:
  kind: max_wall_time
  duration: "PT2H"
```

`termination_policy` is **required** when `dispatch_mode.termination ==
"auto"` — config validation fails if it is absent. To flip an
already-running experiment to `auto`, you must also restart the
orchestrator so it re-reads the config (policies are set at experiment
creation; there is no hot-reload). The `dispatch_mode` itself can still
be toggled at runtime via `PATCH /v0/experiments/{E}/dispatch_mode` (or
`/admin/dispatch-mode/`), but the policy callable is bound at startup.

> In `--control-plane-url` multi-experiment mode the orchestrator still
> reads the `--termination-policy` CLI flag (per-experiment config
> resolution through the chapter-11 registry is deferred to
> [#214](https://github.com/ealt/eden/issues/214)). The single-experiment
> path — every Compose deployment today — uses the YAML block.

## Reference policies

[`eden_dispatch.termination`](../../reference/packages/eden-dispatch/src/eden_dispatch/termination.py)
ships five kinds. Select one declaratively in the experiment config; the
orchestrator's `build_termination_policy()` factory maps the `kind` +
parameters to the callable. Three worked recipes:

```yaml
# Terminate after 20 variants have been attempted.
termination_policy:
  kind: max_variants
  target: 20
```

```yaml
# Terminate after 2 hours of wall-time (ISO 8601 duration string).
termination_policy:
  kind: max_wall_time
  duration: "PT2H"
```

```yaml
# Terminate when the trailing 3 integrated variants show no improvement
# on the `score` metric.
termination_policy:
  kind: convergence_window
  metric: score
  window: 3
  direction: maximize       # optional; defaults to maximize
```

The remaining kinds are `never_terminate` (the default when the block is
absent) and `target_condition` (`{kind: target_condition, metric: score,
threshold: 0.95}` — terminate when the latest integrated variant's metric
crosses the threshold).

### `max_wall_time_policy` caveat

The orchestrator service runs against the wire binding (`StoreClient`),
which currently lacks a `GET /experiment` endpoint that returns the
recorded `created_at`. `StoreClient.read_experiment` synthesizes a
best-effort `created_at` from the client's wall-clock at read time,
so `max_wall_time_policy` against a wire-bound Store never accumulates
wall-time toward the deadline. Wall-time policies require an
in-process Store (e.g. a deployment that embeds the orchestrator
inside the task-store-server process) until a future
`GET /experiment` endpoint lands.

## What happens at terminate

The composite commit
([`04-task-protocol.md`](../../spec/v0/04-task-protocol.md) §8.1):

1. Atomic state-field update: `state` flips from `"running"` to
   `"terminated"`.
2. Atomic event append: `experiment.terminated` with the caller's
   `reason` + the authenticated `terminated_by` worker_id.
3. Idempotency: a second terminate against the already-terminated
   state returns success without committing a second transition; the
   winning caller's `reason` is the one recorded.

Once terminated, the Store-layer guard rejects:

- Every `create_task` op (all three kinds) with 409
  `eden://error/illegal-transition`.
- Every `claim` against a still-pending task with 409
  `eden://error/illegal-transition`.

The Store-layer guard does NOT reject:

- `submit` / `accept` / `reject` on already-claimed tasks (drain
  semantics — committed work in flight is not stranded).
- `integrate_variant` on success variants (the integrator continues
  to drain).

The terminated state is **absorbing** in v0: no
`terminated → running` transition exists. Reactivation is reserved
for a future spec lineage.

## Quiescence vs termination

These are distinct concepts:

- `max_quiescent_iterations` (the experiment-config field; issue #157
  retired the `EDEN_MAX_QUIESCENT_ITERATIONS` env var / CLI flag for the
  single-experiment path) is a **heuristic** for when the orchestrator
  process should exit (no progress observed for N iterations). Pre-12a-3
  deployments used this as the *only* way to shut an experiment down.
  Manual-UI sessions — where a human is drafting / claiming at human
  speed — should set a high value (e.g. `max_quiescent_iterations: 3600`)
  so the orchestrator does not quiesce mid-session.
- `terminate_experiment` is a **deliberate decision**: state flips
  to `terminated`, no new work is accepted, the integration drain
  runs to completion, the orchestrator process then quiesces and
  exits via the existing heuristic.

The two compose cleanly: a deployment with `dispatch_mode.termination
== "auto"` and a configured policy will terminate when the policy
fires, drain integration, and exit via quiescence. A deployment with
`termination == "manual"` (the default) keeps the pre-12a-3 behavior
— quiescence is the only exit path.

## Idempotent re-terminate

Calling `POST /terminate` against an already-terminated experiment is
a clean 200 no-op:

- No second `experiment.terminated` event.
- The winning (first) call's `reason` + `terminated_by` are
  preserved.
- The returned `Experiment` body carries `state: "terminated"`.

The web UI distinguishes the two outcomes via event-log read-back:
`?terminated=ok` means "we won the race"; `?terminated=already-terminated`
means "a prior caller won and our reason was discarded."

## Multi-instance termination

When N orchestrator replicas all observe `state == "running"` and
each independently consults its policy, several may simultaneously
return `Terminate(reason)`. The Store's atomic transition serializes
them per `03-roles.md` §6.4: exactly one wins, emitting one event;
the other replicas observe the post-commit terminated state and
no-op. Both then proceed with the integration drain.

The reference reference impl is single-process for the
`terminate_experiment` op (the Store's `_atomic_operation` lock
serializes concurrent callers), so the multi-instance race-resolution
is wire-tested by the
[`reference/packages/eden-wire/tests/test_lifecycle_wire.py`](../../reference/packages/eden-wire/tests/test_lifecycle_wire.py)
TestTerminateEndpoint::test_idempotent_repeat_different_admin
scenario.
