**Findings**

- Blocker: the plan’s race strategy is not deterministic. The plan brings the full stack up, then stops `planner-host` and assumes four pending plan tasks are still available for the UI drill ([plan](/Users/ericalt/Documents/eden/docs/plans/eden-phase-10e-compose-e2e.md:125), [plan](/Users/ericalt/Documents/eden/docs/plans/eden-phase-10e-compose-e2e.md:333)). In the actual code, the scripted planner drains *all* pending plan tasks in a tight loop, and the host starts polling immediately on startup ([workers.py](/Users/ericalt/Documents/eden/reference/packages/eden-dispatch/src/eden_dispatch/workers.py:97), [host.py](/Users/ericalt/Documents/eden/reference/services/planner/src/eden_planner_host/host.py:44), [compose.yaml](/Users/ericalt/Documents/eden/reference/compose/compose.yaml:205)). By the time `docker compose stop planner-host` runs, there may be no pending plan tasks left.

- High: the planner submit contract in the plan does not match the route. The plan expects `POST /planner/<id>/submit` to 303 back to `/planner/?submitted=ok` ([plan](/Users/ericalt/Documents/eden/docs/plans/eden-phase-10e-compose-e2e.md:159)). The real route returns `200` with `planner_submitted.html` on success, and the existing real-process e2e test asserts exactly that ([planner.py](/Users/ericalt/Documents/eden/reference/services/web-ui/src/eden_web_ui/routes/planner.py:296), [planner.py](/Users/ericalt/Documents/eden/reference/services/web-ui/src/eden_web_ui/routes/planner.py:508), [test_e2e_real_subprocess.py](/Users/ericalt/Documents/eden/reference/services/web-ui/tests/test_e2e_real_subprocess.py:214)).

- Medium: the termination drill is mis-scoped against the actual Compose stack. The scope says “every container,” but section H narrows to `eden-*` containers and treats `eden-repo-init` as potentially present ([plan](/Users/ericalt/Documents/eden/docs/plans/eden-phase-10e-compose-e2e.md:61), [plan](/Users/ericalt/Documents/eden/docs/plans/eden-phase-10e-compose-e2e.md:254), [plan](/Users/ericalt/Documents/eden/docs/plans/eden-phase-10e-compose-e2e.md:268)). In reality the stack also includes `eden-postgres`, `eden-gitea`, and `eden-blob-init`, while `eden-repo-init` is started via `docker compose run --rm --no-deps` and removed ([compose.yaml](/Users/ericalt/Documents/eden/reference/compose/compose.yaml:4), [compose.yaml](/Users/ericalt/Documents/eden/reference/compose/compose.yaml:72), [setup-experiment.sh](/Users/ericalt/Documents/eden/reference/scripts/setup-experiment/setup-experiment.sh:251)).

- Medium: several repo-state assumptions in the prose are stale. Numeric `--plan-tasks` expands to `plan-0001..plan-000N`, not `plan-1..` ([plan](/Users/ericalt/Documents/eden/docs/plans/eden-phase-10e-compose-e2e.md:188), [orchestrator cli](/Users/ericalt/Documents/eden/reference/services/orchestrator/src/eden_orchestrator/cli.py:116)). The web UI already has `--claim-ttl-seconds` and its default is `3600`, not `60` ([plan](/Users/ericalt/Documents/eden/docs/plans/eden-phase-10e-compose-e2e.md:321), [plan](/Users/ericalt/Documents/eden/docs/plans/eden-phase-10e-compose-e2e.md:325), [web-ui cli](/Users/ericalt/Documents/eden/reference/services/web-ui/src/eden_web_ui/cli.py:53)). Admin CSRF failure is a bare `403`, not a redirect, and Postgres/Gitea are also host-published ports ([admin.py](/Users/ericalt/Documents/eden/reference/services/web-ui/src/eden_web_ui/routes/admin.py:313), [compose.yaml](/Users/ericalt/Documents/eden/reference/compose/compose.yaml:21), [compose.yaml](/Users/ericalt/Documents/eden/reference/compose/compose.yaml:60)).

No `CLAUDE.md` exists in this checkout; `rg --files -g 'CLAUDE.md'` returned nothing.

**Dimension Check**

1. Correctness & soundness  
(a) No: numeric seeding is `plan-0001`, `plan-0002`, etc., not `plan-1`.  
(b) Mostly yes on planner form semantics: slug is alnum/dash/underscore, priority is `float()`, `parent_commits` is comma-split, rationale is required; parent SHAs are validated as 40-hex entries ([forms.py](/Users/ericalt/Documents/eden/reference/services/web-ui/src/eden_web_ui/forms.py:57)).  
(c) Yes: admin reclaim redirects `303` to `/admin/tasks/<id>/?reclaimed=ok` ([admin.py](/Users/ericalt/Documents/eden/reference/services/web-ui/src/eden_web_ui/routes/admin.py:331)).  
(d) `task-store-server` is internal-only, but `WEB_UI_HOST_PORT` is not the only published host port; Postgres and Gitea are published too.  
(e) Yes: `setup-experiment.sh` writes `EDEN_PLAN_TASKS`, Compose passes it into `--plan-tasks`, and the orchestrator expands it.

2. Race correctness  
(i) `docker compose stop` is a wait-based stop, but that does not save this design: the bad race is before the stop, during initial startup.  
(ii) Restart is fine; the scripted planner has no sticky cache and just relists pending tasks.  
(iii) During the stopped window, no other component should claim `plan` tasks; the real problem is that `planner-host` may already have consumed them before the window starts.

3. End-state arithmetic  
With `EDEN_PLAN_TASKS=4` and `EDEN_PROPOSALS_PER_PLAN=1`, `>=4 trial.integrated`, `>=12 task.completed`, and `>=4` plan-task completions are right. The arithmetic is fine once the task-allocation race is fixed.

4. Termination correctness  
(a) `compose stop --timeout 10` is the right graceful-stop shape.  
(b) The EDEN Python services should normally exit `0` on SIGTERM because they install graceful handlers ([web-ui cli](/Users/ericalt/Documents/eden/reference/services/web-ui/src/eden_web_ui/cli.py:155), [task-store cli](/Users/ericalt/Documents/eden/reference/services/task-store-server/src/eden_task_store_server/cli.py:171), [planner cli](/Users/ericalt/Documents/eden/reference/services/planner/src/eden_planner_host/cli.py:109), [implementer cli](/Users/ericalt/Documents/eden/reference/services/implementer/src/eden_implementer_host/cli.py:85), [evaluator cli](/Users/ericalt/Documents/eden/reference/services/evaluator/src/eden_evaluator_host/cli.py:90)). `143` is mainly relevant to external images.  
(c) `blob-init` should be `0`. `eden-repo-init` should not still exist in `compose ps -a` because setup uses `run --rm`.

5. CI integration  
`httpx>=0.27,<0.30` would work, but it is not the repo’s existing pin; the workspace uses `httpx>=0.27,<1` ([eden-wire pyproject](/Users/ericalt/Documents/eden/reference/packages/eden-wire/pyproject.toml:8), [eden-service-common pyproject](/Users/ericalt/Documents/eden/reference/services/_common/pyproject.toml:8)). If you keep the Python driver, a one-line `pip install` is lighter than adding `uv sync`; if you want to avoid any install, the driver could be stdlib-only.

6. Failure modes  
Section K is incomplete and partly wrong. The biggest missing failure mode is “planner-host already drained all 4 plan tasks before the stop.” The CSRF row is wrong, the TTL row is stale, and “admin-reclaim 404” is not how the current route is shaped.

7. Out-of-scope discipline  
No major scope creep. The missing piece is not “more scope”; it is fixing the deterministic startup/claim choreography, which is core to the stated scope.

8. Overall assessment  
Not ready to implement as written. The plan needs at least these changes first:

1. Replace the current stop-after-full-startup strategy with a startup sequence that makes the UI-owned plan tasks deterministic.  
2. Fix the planner submit expectations to `200` rendered success, not `303 ?submitted=ok`.  
3. Correct the termination section to cover the actual container set and drop the `eden-repo-init` speculation.  
4. Refresh the stale repo-state details: plan-task IDs, TTL flag/default, CSRF behavior, published-port wording, and `httpx` pin rationale.