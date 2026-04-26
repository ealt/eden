**Findings**

- Bug — [reference/compose/healthcheck/smoke.sh](/Users/ericalt/Documents/eden/reference/compose/healthcheck/smoke.sh:25)  
  The `EXIT` trap deletes `"$TMPENV"` before running `docker compose ... down -v`. Because the script always passes `--env-file "$TMPENV"`, teardown then fails with “couldn't find env file”, and the `|| true` masks it. That means a failing smoke run can leak containers/volumes instead of cleaning up as promised. Fix by running `docker compose ... down -v` before `rm -f "$TMPENV"`; ideally wrap cleanup in a function so you can preserve the original exit status and still report cleanup failures.

- Risk — [reference/compose/healthcheck/smoke.sh](/Users/ericalt/Documents/eden/reference/compose/healthcheck/smoke.sh:58)  
  The Postgres assertion hardcodes `-U eden -d eden` even though the script says `.env.example` is the source of truth. If `POSTGRES_USER` or `POSTGRES_DB` change in `.env.example`, the stack can be healthy and the smoke test will still fail. Fix by deriving both values from `"$TMPENV"` the same way the script already derives `GITEA_HOST_PORT`, or by sourcing the env file before the checks.

- Nit — [reference/README.md](/Users/ericalt/Documents/eden/reference/README.md:9)  
  The 10a update landed on top of stale status text: the file still says the repo is only through Phase 9 chunk 9d, says admin views “arrive in 9e”, and line 5 still says the spec chapters are not yet written. That now contradicts [AGENTS.md](/Users/ericalt/Documents/eden/AGENTS.md:14) and [docs/roadmap.md](/Users/ericalt/Documents/eden/docs/roadmap.md:198). Fix by refreshing the whole status block to current state instead of only prepending the 10a paragraph.

**Overall assessment**

Plan adherence is strong overall: the expected files exist, `compose.yaml` parses cleanly, and the CI/docs wiring largely matches the plan. The main issue is the smoke script teardown bug, which is real and should be fixed before treating `compose-smoke` as a reliable guardrail. After that, the remaining concern is documentation drift rather than infrastructure correctness.

I also cross-checked the Compose/Gitea assumptions against the official docs: [Docker Compose `up --wait`](https://docs.docker.com/reference/cli/docker/compose/up/), [Compose `depends_on` conditions](https://docs.docker.com/compose/compose-file/05-services/), and [Gitea rootless Docker install](https://docs.gitea.com/1.24/installation/install-with-docker-rootless).