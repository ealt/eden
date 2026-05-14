**Findings**

- `Bug` — [reference/scripts/setup-experiment/setup-experiment.sh](/Users/ericalt/Documents/eden-worktrees/phase-12a-1g-experiment-durability/reference/scripts/setup-experiment/setup-experiment.sh:267)  
  This does not implement plan §D.4’s relocation rule. The plan says to abort only when the existing data root’s durable subdirs are non-empty. The script aborts whenever `postgres/` or `gitea/` merely exist, which happens after any prior setup run, even if the root is effectively empty, and it ignores the rest of the durable subdir set. It also makes the documented “move to a new data root” workflow in [docs/operations/experiment-data-durability.md](/Users/ericalt/Documents/eden-worktrees/phase-12a-1g-experiment-durability/docs/operations/experiment-data-durability.md:150) fail in practice. Fix by checking the full durable-subdir set for actual contents, not directory existence.

- `Risk` — [reference/scripts/setup-experiment/setup-experiment.sh](/Users/ericalt/Documents/eden-worktrees/phase-12a-1g-experiment-durability/reference/scripts/setup-experiment/setup-experiment.sh:48)  
  The new help text says the data root “does NOT survive `docker compose down -v`”. That is the opposite of plan §D.3/§D.5 and the actual implementation: `down -v` only removes the remaining named volumes, not the bind-mounted durable state. Fix the help text to say that explicit deletion of `${EDEN_EXPERIMENT_DATA_ROOT}` is the destructive step.

- `Risk` — [reference/compose/.env.example](/Users/ericalt/Documents/eden-worktrees/phase-12a-1g-experiment-durability/reference/compose/.env.example:66)  
  The example file still uses `EDEN_IDEATE_TASKS`, but the compose stack and `setup-experiment.sh` use `EDEN_IDEATION_TASKS`. An operator editing `.env.example` will think they changed ideation task count when the stack will ignore it. Fix the variable name.

- `Risk` — [docs/user-guide.md](/Users/ericalt/Documents/eden-worktrees/phase-12a-1g-experiment-durability/docs/user-guide.md:112)  
  This guide was touched for the chunk but still describes pre-12a-1g behavior in multiple places: it omits the new `--data-root` flag, still says setup creates an `eden-bare-repo` volume at [line 128](/Users/ericalt/Documents/eden-worktrees/phase-12a-1g-experiment-durability/docs/user-guide.md:128), and still presents `docker compose down -v` as a “full wipe” at [line 191](/Users/ericalt/Documents/eden-worktrees/phase-12a-1g-experiment-durability/docs/user-guide.md:191). That deviates from plan §D.4/§D.5 and will mislead operators. Fix the flag table and reset/wipe guidance to match the bind-mount model.

- `Nit` — [reference/compose/README.md](/Users/ericalt/Documents/eden-worktrees/phase-12a-1g-experiment-durability/reference/compose/README.md:3)  
  The README still says the stack includes a “blob volume”, says 10d/10e are still ahead at [line 10](/Users/ericalt/Documents/eden-worktrees/phase-12a-1g-experiment-durability/reference/compose/README.md:10), says Gitea is idle at [line 74](/Users/ericalt/Documents/eden-worktrees/phase-12a-1g-experiment-durability/reference/compose/README.md:74), and says there is no Gitea admin user at [line 126](/Users/ericalt/Documents/eden-worktrees/phase-12a-1g-experiment-durability/reference/compose/README.md:126). The actual compose implementation no longer matches that story. Fix by refreshing the intro/status/connection details to the current post-10dB, post-12a-1g stack.

**Overall Assessment**

The core implementation looks aligned with the plan: the spec changes are in the right place, the durable named volumes were converted to bind mounts, the overlay rewrites avoid the `volumes:` merge trap, and the DooD `--exec-bind` changes preserve host-path identity correctly. I did not find a deeper compose-mount or docker-exec wiring bug.

The main problems are one real implementation bug in `setup-experiment.sh`’s relocation guard and a cluster of stale operator-facing docs/help text that still describe the pre-bind-mount world.
