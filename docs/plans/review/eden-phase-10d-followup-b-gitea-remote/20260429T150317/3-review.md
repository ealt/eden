**Findings**
No blocking findings.

The round-2 revisions close the remaining correctness gaps I had flagged:
- step-2 integrator transport ambiguity now has an explicit immediate `ls-remote` read-back ladder, rather than assuming transport failure means “remote untouched” ([plan](</Users/ericalt/Documents/eden/docs/plans/eden-phase-10d-followup-b-gitea-remote.md:306>));
- remote-orphan cleanup no longer depends on parsing `trial_id` out of the ref name, and instead uses the spec-owned `.eden/trials/<trial_id>/eval.json` tree shape ([plan](</Users/ericalt/Documents/eden/docs/plans/eden-phase-10d-followup-b-gitea-remote.md:244>));
- the HTTP/TLS wording is now internally consistent ([plan](</Users/ericalt/Documents/eden/docs/plans/eden-phase-10d-followup-b-gitea-remote.md:99>)).

**Residual risks**
- The new `§D.7c` sweep logic is load-bearing enough that I would make sure the implementation adds at least one test for a malformed “trial-like” remote commit: zero or multiple `.eden/trials/*` entries should fail closed and log loudly.
- The “push transport-fails, then `ls-remote` also transport-fails” branch is now well-described, but it depends on the orchestrator actually failing out and restarting so the startup sweep runs. That is consistent with the current loop shape, but it is worth preserving in tests.

**Assessment**
Ready to implement.