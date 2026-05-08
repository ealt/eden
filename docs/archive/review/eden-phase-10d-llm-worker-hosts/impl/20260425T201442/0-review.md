**Plan Adherence**

- **Bug** — [compose.subprocess.yaml](/Users/ericalt/Documents/eden/reference/compose/compose.subprocess.yaml:58), [compose.subprocess.yaml](/Users/ericalt/Documents/eden/reference/compose/compose.subprocess.yaml:94): Plan §D.1/§D.8 says subprocess-mode Compose wiring should carry the per-host `--*-env-file` mechanism through to all three workers. The overlay only wires `--plan-env-file`; implementer and evaluator get no equivalent mount or flag. In the real Compose deployment, that means `implement_command`/`evaluate_command` cannot receive API keys via the documented path. Fix by adding mounted env files plus `--implement-env-file` and `--evaluate-env-file`, or update the plan/docs if this was intentionally deferred.

- **Risk** — [host.py](/Users/ericalt/Documents/eden/reference/services/planner/src/eden_planner_host/host.py:89): Plan §Risks says planner respawn-on-crash is in scope “subject to a max respawn count”. The loop currently respawns forever with no cap or backoff. A broken `plan_command` can thrash indefinitely. Fix by tracking consecutive startup/task failures and stopping after a configurable ceiling.

**Correctness**

- **Risk** — [cli.py](/Users/ericalt/Documents/eden/reference/services/planner/src/eden_planner_host/cli.py:136): the planner sets `EDEN_EXPERIMENT_DIR` and then overlays the user env file on top of it. That lets `--plan-env-file` replace a host-owned reserved variable, which breaks the §D.0 command/cwd/env contract and can redirect command expansion to the wrong tree. Fix by loading the env file first and then force-writing reserved `EDEN_*` keys, or rejecting collisions.

- **Risk** — [subprocess_mode.py](/Users/ericalt/Documents/eden/reference/services/planner/src/eden_planner_host/subprocess_mode.py:278), [host.py](/Users/ericalt/Documents/eden/reference/services/planner/src/eden_planner_host/host.py:117): a normal `store.claim()` race on a pending plan task is treated as an unexpected planner failure and causes the long-running subprocess to be torn down. In a deployment with another planner or the Web UI, that discards accumulated planner context for a non-fault condition. Fix by catching claim-state exceptions before dispatch and skipping the task without respawning the subprocess.

**Integration**

- **Bug** — [compose.subprocess.yaml](/Users/ericalt/Documents/eden/reference/compose/compose.subprocess.yaml:86), [subprocess_mode.py](/Users/ericalt/Documents/eden/reference/services/implementer/src/eden_implementer_host/subprocess_mode.py:314): Plan §D.3 step 5 depends on planner-written rationale artifacts being visible to the implementer so `rationale_path` can be threaded into `.eden/task.json`. In Compose subprocess mode, the implementer container does not mount `eden-artifacts-data`, so `file:///var/lib/eden/artifacts/.../rationale.md` does not exist there and `_rationale_path_from_uri` drops the field. The fixture passes because `implement.py` ignores `rationale_path`, but real user commands won’t get the planned planner→implementer handoff. Fix by mounting the artifacts volume into `implementer-host` at `/var/lib/eden/artifacts` (read-only is enough).

**Robustness**

- **Risk** — [subprocess_runner.py](/Users/ericalt/Documents/eden/reference/services/_common/src/eden_service_common/subprocess_runner.py:195), [subprocess_mode.py](/Users/ericalt/Documents/eden/reference/services/planner/src/eden_planner_host/subprocess_mode.py:178), [subprocess_mode.py](/Users/ericalt/Documents/eden/reference/services/implementer/src/eden_implementer_host/subprocess_mode.py:222), [subprocess_mode.py](/Users/ericalt/Documents/eden/reference/services/evaluator/src/eden_evaluator_host/subprocess_mode.py:183): the plan says worker-side failure context should be surfaced through structured logs with `task_id`/role/reason, and that worker `description` fields stay in logs rather than on the wire. The implementation loses most of that context: stderr forwarding has no `task_id`, planner `plan-error.reason` is discarded, and implementer/evaluator `outcome.description` is ignored. Fix by plumbing `task_id` into subprocess log forwarding and explicitly logging `reason` / `description` before normalizing to wire submissions.

**Code Quality**

- **Nit** — [worker-host-subprocess.md](/Users/ericalt/Documents/eden/spec/v0/reference-bindings/worker-host-subprocess.md:7): the new reference-binding doc uses broken relative links (`03-roles.md`, `04-task-protocol.md`, `schemas/...`) from inside `spec/v0/reference-bindings/`. Fix them to `../03-roles.md`, `../04-task-protocol.md`, and `../schemas/...`.

**Overall Assessment**

The core host-side subprocess flows are mostly faithful to the plan, and the implementer’s `create_trial(status="starting")` ordering is correct. The main problems are in the real deployment wiring and in planner robustness: Compose subprocess mode is still missing pieces the plan treated as part of the feature, and normal operational edge cases can currently reset planner state or hide the only useful diagnostics.