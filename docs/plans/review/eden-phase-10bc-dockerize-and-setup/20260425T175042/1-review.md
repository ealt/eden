**1. Missing Context**

Assessment: much better. The round-0 blockers around roadmap drift, repo-seeding ownership, and env-file shape are now explicitly addressed. I do not see a major “reader can’t tell what this means” problem anymore.

**2. Feasibility**

Assessment: mostly feasible, but one first-run bootstrap gap remains.

- The advertised `setup-experiment.sh && docker compose up` flow still appears to assume an image is available before setup can seed the repo. The script’s critical step is `docker compose … run --rm eden-repo-init` in [the bootstrap section](/Users/ericalt/Documents/eden/docs/plans/eden-phase-10bc-dockerize-and-setup.md:293) and [the step ordering](/Users/ericalt/Documents/eden/docs/plans/eden-phase-10bc-dockerize-and-setup.md:385), but the plan only says “if step 3 fails, tell the operator to build first” at [lines 395-397](/Users/ericalt/Documents/eden/docs/plans/eden-phase-10bc-dockerize-and-setup.md:395). That leaves first-run behavior underdefined. I would make the script do the build itself, either by using `docker compose run --build --rm …` or by running an explicit `docker compose build eden-repo-init` first.

**3. Alternatives**

Assessment: the chosen approach is now defensible.

- The new roadmap-delta section makes the shared-image choice and the deferred 10c sub-jobs explicit enough that I no longer think this needs a different high-level approach. I don’t have a stronger alternative than “keep this shape, but make the setup bootstrap self-sufficient.”

**4. Completeness**

Assessment: one important runtime contract is still inconsistent.

- The `web-ui` row says the bare repo mount can be read-only because the admin work-refs page only needs reads in [the service matrix](/Users/ericalt/Documents/eden/docs/plans/eden-phase-10bc-dockerize-and-setup.md:256). But in current code, passing `--repo-path` enables the whole implementer module, not just admin ref browsing: see [web-ui CLI](/Users/ericalt/Documents/eden/reference/services/web-ui/src/eden_web_ui/cli.py:75) and [app factory](/Users/ericalt/Documents/eden/reference/services/web-ui/src/eden_web_ui/app.py:89). That means `/implementer/*` would be live in Compose and would try to write refs against a read-only mount. The plan needs one of three explicit resolutions: mount read-write, split “admin repo access” from “implementer enabled,” or disable `/implementer/*` in the Compose deployment.

**5. Edge Cases and Risks**

Assessment: one likely CI flake remains.

- The smoke test checks the seeded ref via `docker compose exec orchestrator git -C /var/lib/eden/repo show-ref refs/heads/main` in [lines 464-466](/Users/ericalt/Documents/eden/docs/plans/eden-phase-10bc-dockerize-and-setup.md:464), then separately expects the orchestrator to exit on quiescence in [lines 467-469](/Users/ericalt/Documents/eden/docs/plans/eden-phase-10bc-dockerize-and-setup.md:467). If the experiment finishes quickly, step 4 becomes racey because `exec` requires a running container. I’d make that assertion through a one-shot container or direct volume inspection instead of depending on orchestrator liveness. I’d also consider `docker compose run --rm --no-deps eden-repo-init` to keep future dependency edits from changing setup behavior.

**Overall Assessment**

This is a substantial improvement over round 0. The plan is now coherent enough to review all five levels, and I think the high-level shape is sound. I would still fix the first-run bootstrap/build story and the web-ui `--repo-path` vs read-only-mount contradiction before treating it as implementation-ready.

External docs checked for Compose behavior: https://docs.docker.com/reference/cli/docker/compose/run/ , https://docs.docker.com/reference/cli/docker/compose/up/