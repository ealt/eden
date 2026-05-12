# Orchestrator and worker roles — design discussion

**Status:** design exploration; not yet a spec change.
**Origin:** captured during a manual-UI validation run on 2026-05-02
through conversation with @ericalt. Supersedes issues #9–#12 of
`MANUAL_UI_ISSUES.md`.

## Problem statement

EDEN's current task abstraction inherits from work-queue patterns where
workers are interchangeable consumers of a single shared queue. Two
core assumptions of that model break in a real human/AI deployment:

1. **Workers are *not* interchangeable.** Real deployments host humans
   (each unique), agents of different kinds (Claude, Codex, ...), and
   multiple instances per kind. Tasks routinely need to target a
   specific worker, a specific class, or a specific group. A fast
   agent will outcompete a slow human for any task on a shared queue,
   structurally locking the human out.

2. **The orchestrator is *not* a singleton autonomous process.** The
   "orchestrator" today is a polling loop that makes four kinds of
   routing decisions. Each decision is a plausible point for human
   override — "evaluate this variant with Alice, not Bob"; "don't
   integrate yet, I want to verify off-protocol". The current shape
   gives operators no in-protocol way to override individual
   decisions.

This document sketches a redesign that treats workers as
non-interchangeable identities with optional group membership, and
treats the orchestrator's decisions as another role that humans (and
multiple competing auto-processes) can play.

## Core decisions

### 1. Worker identity: ids and groups (RBAC-style)

**Resolved by Phase 12a-1** — landed in [`spec/v0/02-data-model.md`](../../spec/v0/02-data-model.md) §6 (worker registry), §7 (groups), §3.5 (`Task.target`) and [`spec/v0/04-task-protocol.md`](../../spec/v0/04-task-protocol.md) §3.5 (claim-time RBAC ladder). The §3.5 ladder runs three preconditions atomically with the claim write: (1) state is `pending`; (2) `worker_id` is registered or → `WorkerNotRegistered`; (3) `worker_id` satisfies `Task.target` (null/worker/group) or → `WorkerNotEligible`. Transitive group resolution with cycle detection lives in `Store.resolve_worker_in_group`.

- Every worker has a unique ``worker_id`` registered with the store.
- Groups are named sets whose members are ``worker_id`` s and/or other
  groups. Recursion permitted; cycle-detection at resolution time.
- ``Task.target`` is one of: a ``worker_id``, a group name, or null
  (= any worker matching the task ``kind`` can claim).
- ``Store.claim`` checks: ``worker_id == target`` OR ``worker_id`` is
  transitively in the ``target`` group OR ``target is null``.

This is RBAC, not Kubernetes labels. No attribute schema, no selector
expression language. Inclusion-only — to express "anyone but Claude",
define the inclusion group instead.

Mapping examples:

| Intent | ``Task.target`` |
|---|---|
| "Reserve for me" | ``eric`` |
| "Any human" | ``humans`` |
| "Claude only" | ``claude-agents`` |
| "Any ideator" | ``null`` |
| "Teammate Alice specifically" | ``alice`` |

### 2. Ideation-task creation: shared between auto-orchestrator and humans

Today the orchestrator pre-seeds N ideation tasks at startup and never
creates more. This conflates "experiment planning capacity" with
"static budget at t=0". Replace with:

- The auto-orchestrator continuously creates ideation tasks per a
  configurable policy (e.g., "maintain M pending ideation tasks at a
  time", "create one per integrated variant", "create one per fixed
  interval"). The policy is a deployment concern; default for
  validation runs is "maintain 3 pending".
- Humans (and other privileged callers) can also create ideation tasks
  directly via ``create_task``, with whatever ``target`` they want.
- Both paths use the same underlying wire op
  (``POST /v0/experiments/<id>/tasks`` already exists from chapter 7).

When a human creates a ideation task targeted at themselves, no other
worker can claim it — the contention with the auto-orchestrator
disappears. When a human creates one targeted broadly (e.g., at
``humans``), the routing intent is explicit.

### 3. Per-idea executor hint

The idea carries an optional ``intended_executor: worker_id |
group_id | null``. When the orchestrator (auto or human) creates the
execution task derived from this idea, that hint becomes the
created task's ``target``. ``null`` means "no preference; orchestrator
default policy applies".

The hint is at idea granularity, not ideation-submission granularity,
because each idea is dispatched independently and may want a
different executor.

The ideator needs to *know* what executor ids/groups are valid:

- **UI**: surface available worker ids and groups in the idea-draft
  form (autocomplete or dropdown).
- **CLI**: surface available worker ids and groups via a new subcommand
  (``eden-manual list-workers``).
- **Agentic ideator**: experiment setup (e.g., AGENTS.md or a
  per-experiment manifest) names the worker ids and groups the ideator
  should know about. Ideators that don't know — or don't care — submit
  ``intended_executor = null``.

### 4. Evaluator assignment: orchestrator owns; humans can override

Executor should not pick the evaluator (anti-pattern: executor
cherry-picks favorable evaluators). Ideator-set evaluator hints have a
similar smell. Default policy is the auto-orchestrator's call;
deployment chooses the policy (round-robin, random, load-balanced).

Humans override per-variant via the per-decision pause/unpause
mechanism (§6).

### 5. Worker attribution survives on artifacts

**Resolved by Phase 12a-1** — `Task.submitted_by` / `Task.created_by`, `Idea.created_by`, and `Variant.executed_by` / `Variant.evaluated_by` shipped per [`spec/v0/02-data-model.md`](../../spec/v0/02-data-model.md) §3.1, §5.1, §9. Each is written atomically with the transition that produces the artifact and preserved across the terminal transitions that clear `task.claim`. Wave-5 of the chunk wired the executor / evaluator accept-step writes; the conformance suite's `test_attribution_persistence.py` (chapter 9 §5 "Attribution persistence") pins the survival MUSTs.

Currently ``Task.claim.worker_id`` is cleared after accept; attribution
only survives in the event log. Make it a first-class field:

- ``Task`` gains ``created_by`` and ``submitted_by`` (preserves the
  claimant's ``worker_id`` after the task reaches a terminal state).
- ``Idea`` gains ``created_by`` (the ideator who drafted it).
- ``Variant`` gains ``executed_by`` and ``evaluated_by``.

Attribution is *data*, not log content. The user-facing query "who
implemented variant T?" should be a single read, not an event-log
fold.

Backward-compat: each is a new optional field; existing artifacts
without the field still validate.

### 6. Per-decision pause/unpause toggle (UI-driven)

The auto-orchestrator runs each of its four decision types
independently. For each type, the experiment carries a ``dispatch_mode:
{auto, manual}`` flag. When ``auto``, the auto-orchestrator
makes the decision. When ``manual``, the auto-orchestrator skips that
decision type entirely and waits.

The UI exposes a per-decision-type toggle. Operator workflow:

1. Variant T transitions to ``starting`` with ``commit_sha`` set.
2. Operator wants to manually assign an evaluator. They flip "evaluate
   dispatch" to ``manual`` *before* the auto-orchestrator runs (or
   after, see below).
3. Operator creates the evaluation task with the desired ``target``.
4. Operator flips back to ``auto``.

If the auto-orchestrator was faster and already dispatched, operator
uses ``reassign(task_id, new_target)`` instead.

The pause is per-decision-type, not global. Humans pausing "evaluate
dispatch" doesn't stop "execution-task dispatch" or integration.

### 7. Task reassignment

GitHub-style reassignment as an admin op:

- Pending task: ``reassign(task_id, new_target)`` updates ``target``.
- Claimed task: ``reassign`` = ``reclaim(operator) + update target``.
  Atomic.
- Submitted/terminal: not reassignable; create a new task instead.

### 8. Claims are scoped to the worker, not to the application

**Resolved by Phase 12a-1 (CLI-to-CLI side)** — `Task.claim` is now identity-keyed: the recorded `worker_id` is the sole identity the §4 submit transition matches against (per-claim opaque tokens were retired). Two clients authenticated as the same `worker_id` can collaborate on a claim — client A claims, client B submits — without exchanging an authentication artifact through the claim object. The conformance suite's `test_worker_auth.py::test_two_clients_share_a_claim_via_worker_identity` pins the cross-application MUST. **Deferred to a 12a-1b follow-up:** the Web-UI per-session retrofit (plan §D.5b) — at sign-in, web-ui uses its admin token to register the user as a worker and mint a session-scoped credential. This is orthogonal to the spec-level resolution above and is expected to land alongside the 12a-2 web-ui changes.

**Observed friction.** During the manual-UI session it became clear
that today's claim model ties a claim to whichever application
instance produced it. The web-ui stores claim tokens in an in-process
Python dict (``_CLAIMS``); the CLI stores them in
``/tmp/eden-manual/.claims.json``. A claim made via one is invisible
to the other — the user can't claim in the CLI and then draft
ideas in the web-ui's nicer form. They have to commit to one
application for the whole task lifecycle, or hand-copy the token.

**Why this happens.** Today's per-claim token does triple duty:
authentication ("prove you're the claimant"), idempotency ("tag this
submission"), and conflict detection ("catch concurrent submits").
Because the token is the *only* authentication mechanism and is
returned to the caller as opaque-secret-by-value, only the
application that received it can use it.

**The fix flows from §1.** Once worker identity is first-class
(workers register and authenticate to the store), the token's
authentication role can move to connection-level worker auth: the
store checks "is this submitter the worker who claimed?" by looking at
the authenticated worker_id of the connection, not by token equality.

Effects:

- Per-claim tokens become an *idempotency* mechanism, not an *auth*
  mechanism. They MAY be retained for that purpose (or replaced by
  client-supplied submission ids). Either way, they are no longer the
  authentication boundary.
- A user authenticated as worker ``eric`` can act on any of eric's
  claims from any application. CLI claim → Web UI submit works
  seamlessly. Both simply present credentials proving "I am eric".
- Application-local claim caches (``_CLAIMS``,
  ``/tmp/eden-manual/.claims.json``) become optional convenience —
  the source of truth is the store's `Task.claim.worker_id`, which
  any authenticated worker can query.

**Spec implications** (additive to §1):

- Chapter 4 ``submit`` no longer requires a token argument; it
  requires the submitter to be authenticated as the claim's worker.
  The token MAY be supplied as an idempotency hint.
- Chapter 7 wire endpoints add per-worker auth (the bearer token
  today is a deployment-shared secret; needs splitting into
  per-worker creds).
- ``Store.claim`` MAY still return a token-shaped value, but its
  contract becomes "for idempotency / dedup, not for ownership".

**This is a meaningful spec change to chapter 4** because the current
text hangs important guarantees on the token. The migration story:
implementations MAY accept submissions either with a valid token (old
behavior) or with worker-authenticated identity matching the claim's
worker_id (new behavior), allowing rolling deploys.

### 9. Termination is deployment policy, not spec mechanism

**Today's drift.** The spec at
[`spec/v0/02-data-model.md`](../../spec/v0/02-data-model.md) §3
declares four termination fields (``max_variants``, ``max_wall_time``,
``convergence_window``, ``target_condition``) and the JSON schema +
Pydantic models declare them too. **None are enforced anywhere in the
reference implementation.** The orchestrator's only termination is
the 30-iteration quiescence heuristic (which is itself the wrong
abstraction; see #1). See `MANUAL_UI_ISSUES.md` issue #14 for the
audit findings.

**The right framing isn't "implement those four fields".** It's:
*termination is policy, not protocol.* The spec should not enumerate
specific termination conditions. Different deployments have wildly
different needs:

- A research deployment: "no objective improvement in 10 variants AND
  wall-time > 1h"
- A production deployment: "abort if any variant's eval-error rate >
  5%"
- A demo deployment: "stop after 20 variants"
- A multi-experiment deployment: "no termination; experiments are
  archived by operator"

Hardcoding any subset into the spec imposes preconceptions that
foreclose other valid deployment patterns.

**Proposed model: the orchestrator delegates to a deployment-supplied
termination policy.**

- The spec defines the **mechanism**: before each iteration of its
  loop, the orchestrator consults a "termination decision" that takes
  a read-only view of experiment state and returns
  ``terminate(reason?)`` or ``continue``.
- The spec defines the **input surface**: the read-only view exposes
  exactly the data already in chapters 02 / 04 / 05 / 06 / 08 — tasks,
  ideas, variants, events, the integrated repo. No new data type;
  just a query interface over what's already canonical.
- The spec does **not** define the **policy**: how the deployment
  *provides* the decision is implementation-defined. The reference
  impl might use a Python callable; another impl might use a wire
  endpoint, a DSL expression, or an out-of-band signal. All
  conformant.

**What disappears from the spec:**

- ``max_variants``, ``max_wall_time``, ``convergence_window``,
  ``target_condition`` from
  [`02-data-model.md`](../../spec/v0/02-data-model.md) §3 and from
  [`experiment-config.schema.json`](../../spec/v0/schemas/experiment-config.schema.json).
- Any chapter text presuming a specific termination criterion.

**What appears in the spec:**

- A new section in chapter 3 (or chapter 11, parallel to "experiment
  config" being chapter 2) defining the termination decision contract.
- A new event type ``experiment.terminated`` with a ``reason`` payload.
- An ``experiment.state`` lifecycle including a ``terminated``
  transition (from ``running``).

**What deployments do:**

- Reference impl: orchestrator CLI gains
  ``--termination-policy <module:callable>`` or similar; the callable
  is invoked once per iteration. Default: a "never terminate" policy
  that lets the experiment run forever (operator-driven termination).
- Real deployments override with whatever predicate they want.
- Common predicates (max_variants, max_wall_time, etc.) ship as a
  reference-impl library of pluggable policies, not as spec
  requirements.

**Connection to ``experiment-config.yaml``.** The four fields can
move from "normative" to "informative" — a reference-impl library
function reads them when invoked. Deployments that don't use that
library don't need to set them. Schema's permissive
``additionalProperties`` already allows this.

**Termination state.** Once the policy says terminate:

- Orchestrator stops dispatching new tasks (no new execute /
  evaluation task creation).
- In-flight tasks complete or time out per existing semantics.
- ``experiment.terminated`` event emitted with the policy's reason
  string.
- Experiment transitions to ``terminated`` state. New ideation tasks
  cannot be created (so #2's "humans can create ideation tasks" stops
  applying).

**Resumability** is a separate concern: in v0, ``terminated`` is
absorbing. A future spec change could add ``terminated → running``
transitions for operator-resumable experiments.

### 10. Orchestrator as a role (cumulative implication)

Putting it all together: the auto-orchestrator becomes one worker in
the orchestrator role pool, with a permissive default selector. A
deployment with zero auto-orchestrators is valid — every routing
decision driven by humans.

The auto-orchestrator process changes:

- It no longer pre-seeds ideation tasks; instead it runs a continuous
  policy.
- It no longer "exits on quiescence" (issue #1) — there is no
  experiment-level quiescence to detect, since new ideation tasks may
  arrive at any time.
- Its decisions respect the per-decision-type ``dispatch_mode`` flags.

## Spec changes (sketch)

These are the chapter-level edits implied. None are committed; this
is a working list.

| Chapter | Change |
|---|---|
| 02 (data model) | ``Worker`` becomes first-class. ``Group`` introduced. ``Task.target``, ``Task.created_by``, ``Task.submitted_by`` added. ``Idea.intended_executor``, ``Idea.created_by`` added. ``Variant.executed_by``, ``Variant.evaluated_by`` added. ``max_variants``, ``max_wall_time``, ``convergence_window``, ``target_condition`` REMOVED from normative experiment-config (per §9 — termination is policy). |
| 03 (roles) | ``Orchestrator`` becomes a defined role with four decision types. Ideator contract gains optional ``intended_executor`` per idea. Evaluator-assignment policy clarified (orchestrator owns; executor/ideator do not set). New "termination decision" contract for the orchestrator: takes read-only state view, returns terminate/continue (per §9). |
| 04 (task protocol) | ``claim`` enforces ``target`` matching against the worker's id and group memberships. ``submit`` shifts authentication from per-claim token to authenticated worker-id matching the claim's worker_id (token retained as idempotency hint). ``reassign`` added. ``dispatch_mode`` per-experiment-per-decision flag added. |
| 05 (events) | New event type ``experiment.terminated`` with ``reason`` payload. |
| 07 (wire) | New endpoints: ``register_worker``, ``register_group``, ``list_workers``, ``list_groups``, ``reassign_task``, per-experiment ``set_dispatch_mode``. Per-worker auth (current shared bearer becomes per-worker credentials so `submit` can match by worker identity). |

## Open questions

1. **Where do worker-id and group registries live?** Per-experiment, or
   deployment-wide? Per-experiment is cleanly isolated but means
   re-registering for each new experiment. Deployment-wide is more
   convenient but adds a new persistence concern.

2. **Default groups?** Should ``humans`` and ``agents`` be
   well-known groups created on every deployment, or fully
   configurable?

3. **Multiple auto-orchestrators?** If you can run zero, can you run
   more than one for HA? If so, how is "the auto-orchestrator's
   default policy" coordinated across replicas? Phase 12's lease
   model might cover this.

4. **Migration path for existing experiments.** ``Task.target`` and
   the other new fields are optional and backward-compatible. But
   ``store.claim`` semantics change: a worker that previously was
   allowed to claim any task of the right ``kind`` is now blocked by
   selector matching. Existing deployments need a "no-op" migration
   where every task gets ``target=null`` and every worker_id stays
   unconstrained.

5. **Operator authority.** Who can create groups? Reassign tasks? Flip
   ``dispatch_mode``? RBAC-on-the-RBAC. v0 may want to skip this and
   say "all callers with the bearer token can do everything"; later
   phases tighten.

## Issues now superseded by this doc

The following entries in `MANUAL_UI_ISSUES.md` are consolidated into
this doc:

- #9 — Ideation-task budget is statically pre-allocated
- #10 — No worker affinity
- #11 — Orchestrator should be a role
- #12 — Worker attribution should survive on tasks/variants/ideas

(#1 — orchestrator quiescence-exit — is also addressed by §6/§9/§10 here
but kept in the issues file as an immediate-impact bug for current
runs.)
