Levels 1-2 look converged now, so I continued.

**3. Alternatives**

Brief assessment: the chosen approach still looks like the right one. Keeping 13c as a chart/runbook substrate change, with full DSNs stored in Secrets and no bundled migration tool or pooler, is the best fit for the current codebase and scope. I don’t see a better alternative that preserves the “no code changes” goal.

No substantive alternatives findings.

**4. Completeness**

I have one remaining substantive completeness issue:

- The migration runbook now tells the operator to compare the restored/post-upgrade event count against a pre-snapshot baseline, but it never actually includes a step that captures that baseline before `pg_dump`. Step 4 says `SELECT count(*) FROM event; -- expects same count as before`, and step 7 says to compare against “the pre-snapshot count captured before pg_dump,” but no earlier step records it. ([eden-phase-13c-managed-postgres.md](/Users/ericalt/Documents/eden-worktrees/phase-13cde-substrate-plans/docs/plans/eden-phase-13c-managed-postgres.md:1103), [eden-phase-13c-managed-postgres.md](/Users/ericalt/Documents/eden-worktrees/phase-13cde-substrate-plans/docs/plans/eden-phase-13c-managed-postgres.md:1147)) Add one explicit pre-snapshot command in step 1 or step 2 to record the source event count, either from SQL or from the wire endpoint’s returned `cursor`.

**5. Edge Cases and Risks**

Brief assessment: the risk coverage is otherwise strong. The document now explicitly covers the real sharp edges: worker-host claims without TTL, the 13a `existingSecret` upgrade trap, external-secret TLS suffix ownership, and kind CoreDNS limitations.

No additional substantive edge-case findings beyond the completeness issue above.

**Overall Assessment**

This is very close. I would not call full convergence yet because the runbook still relies on a pre-snapshot event-count baseline that it never instructs the operator to capture. Once that explicit baseline-capture step is added, I’d consider the plan converged.