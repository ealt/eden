**Findings**

- `Risk` — [docs/operations/experiment-data-durability.md](/Users/ericalt/Documents/eden-worktrees/phase-12a-1g-experiment-durability/docs/operations/experiment-data-durability.md:163)  
  The new sentinel-based guard fixed the “fresh mkdir” false positive, but this documented “move an experiment to a new data root” recipe still does not work as written. With an existing `.env` pointing at `/OLD/ROOT`, `setup-experiment.sh --data-root /NEW/ROOT` still aborts because the old root intentionally contains `postgres/PG_VERSION` or `gitea/conf` ([setup-experiment.sh](/Users/ericalt/Documents/eden-worktrees/phase-12a-1g-experiment-durability/reference/scripts/setup-experiment/setup-experiment.sh:272)). Fix the recipe so it is compatible with the guard: either tell the operator to update `.env` first, temporarily move/remove the old root after the `rsync`, or document a different manual migration sequence.

- `Nit` — [docs/user-guide.md](/Users/ericalt/Documents/eden-worktrees/phase-12a-1g-experiment-durability/docs/user-guide.md:200)  
  This wipe recipe reads `EDEN_EXPERIMENT_DATA_ROOT` from `reference/compose/.env` while the surrounding commands use `docker compose --env-file .env`, which implies the operator is already in `reference/compose`. In that context the path should be `.env`, not `reference/compose/.env`. Fix the path or explicitly state the working directory.

- `Nit` — [reference/compose/README.md](/Users/ericalt/Documents/eden-worktrees/phase-12a-1g-experiment-durability/reference/compose/README.md:143)  
  The operations table still labels `docker compose down -v` as “Wipe all data”, but the paragraph immediately below correctly says it does not remove the bind-mounted durable state. Rename that row to something like “Remove containers + ephemeral volumes” and reserve “wipe all data” for the `rm -rf "$EDEN_EXPERIMENT_DATA_ROOT"` step.

**Overall Assessment**

The code-side fixes look good. The relocation guard now matches the intended “don’t trip on empty setup-created dirs” behavior, the help text is corrected, the `.env.example` typo is fixed, and the main user-facing docs are substantially closer to the actual bind-mount model.

I did not find a new runtime or compose-wiring regression in the updated files. The only material gap left is the documented current-root to new-root migration recipe, which still conflicts with the guard you now enforce.
