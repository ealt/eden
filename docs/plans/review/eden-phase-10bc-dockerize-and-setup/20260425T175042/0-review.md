**Missing Context**

Assessment: significant concerns. I would not treat this as implementation-ready yet.

- The plan changes the phase contract without clearly amending the source of truth. The roadmap says 10b is “each reference service dockerized with its own image” and 10c includes building an experiment-specific image and control-plane registration, but the plan switches to one shared image and defers two 10c responsibilities to later phases. That may be the right call, but it needs to be framed as an explicit scope change, not just a local interpretation. See [the roadmap contract](/Users/ericalt/Documents/eden/docs/roadmap.md:198) versus [the plan’s deferrals](/Users/ericalt/Documents/eden/docs/plans/eden-phase-10bc-dockerize-and-setup.md:29) and [shared-image choice](/Users/ericalt/Documents/eden/docs/plans/eden-phase-10bc-dockerize-and-setup.md:151).

- The repo/bootstrap ownership story is internally contradictory. Early on, the plan says `setup-experiment` initializes and seeds the bare repo, but later it says a new `eden-repo-init` service does that work; it also says `setup-experiment` writes `EDEN_BASE_COMMIT_SHA` into `.env`, then later says the seed SHA is only known after `compose up` and must be read from `SEED_SHA` on the volume. A reader cannot tell which component owns repo creation, seeding, and base-SHA propagation. See [initial scope table](/Users/ericalt/Documents/eden/docs/plans/eden-phase-10bc-dockerize-and-setup.md:32), [repo-init section](/Users/ericalt/Documents/eden/docs/plans/eden-phase-10bc-dockerize-and-setup.md:282), and [seed-SHA section](/Users/ericalt/Documents/eden/docs/plans/eden-phase-10bc-dockerize-and-setup.md:346).

- The runtime config contract is incomplete. The Compose section gives abbreviated commands, but several services have required flags whose source is never specified in the plan: planner/implementer/evaluator all require `--worker-id`; evaluator also requires `--experiment-config`; web-ui requires `--session-secret` and `--artifacts-dir`. Without a full variable/mount matrix, the deployment is underspecified. Compare [the Compose section](/Users/ericalt/Documents/eden/docs/plans/eden-phase-10bc-dockerize-and-setup.md:230) with [planner CLI](/Users/ericalt/Documents/eden/reference/services/planner/src/eden_planner_host/cli.py:27), [evaluator CLI](/Users/ericalt/Documents/eden/reference/services/evaluator/src/eden_evaluator_host/cli.py:28), and [web-ui CLI](/Users/ericalt/Documents/eden/reference/services/web-ui/src/eden_web_ui/cli.py:31).

**Feasibility**

Assessment: there is at least one concrete blocker even if the scope issues above are resolved.

- The Dockerfile’s `uv sync --frozen --no-dev` step is wrong for this workspace shape. The root project is non-installable and the workspace packages are pulled in through the `dev` group, so `--no-dev` at the root does not install the service packages. I verified locally with a dry run: it would uninstall all `eden-*` packages from the environment. This needs `uv sync --frozen --no-dev --all-packages` or a different install strategy. See [planned Dockerfile step](/Users/ericalt/Documents/eden/docs/plans/eden-phase-10bc-dockerize-and-setup.md:185) and [workspace root config](/Users/ericalt/Documents/eden/pyproject.toml:1).

- The idempotency story for config changes is shaky. The plan says the stack can be updated with `docker compose restart`, but it also chooses Compose `configs:` for the experiment config. Docker documents configs as created at deployment time, so a plain restart is not a safe “pick up new config” story; this likely needs `docker compose up -d` with recreation semantics instead. See [plan output/update flow](/Users/ericalt/Documents/eden/docs/plans/eden-phase-10bc-dockerize-and-setup.md:355) and Docker’s configs docs: https://docs.docker.com/reference/compose-file/configs/

I did not evaluate alternatives, completeness, or edge cases further. The scope contract and bootstrap/runtime mechanics need to be made consistent first.

**Overall Assessment**

The plan has a plausible high-level direction, but it is not ready as an execution contract. Resolve the scope redefinition explicitly, collapse the repo/bootstrap flow to one authoritative path, and fix the Docker/uv install story before spending time on lower-level completeness or risk review.

Sources used for external behavior checks: https://docs.docker.com/reference/compose-file/configs/ , https://docs.docker.com/reference/cli/docker/compose/up/ , https://docs.docker.com/compose/how-tos/startup-order/