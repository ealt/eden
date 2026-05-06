**Stopped At Level 5: Edge Cases**

- The new orchestrator-revocation contract depends on an error taxonomy that is still internally inconsistent. [§5.4](/Users/ericalt/Documents/eden/docs/plans/eden-phase-12a-2-orchestrator-as-role.md:760) says the new server-side authority checks return `403 eden://error/unauthorized` on membership miss. [§7.9](/Users/ericalt/Documents/eden/docs/plans/eden-phase-12a-2-orchestrator-as-role.md:1242) says the orchestrator should distinguish bad credentials from revoked role authority by treating bearer-validation failure as `403 eden://reference-error/unauthorized` and group-membership failure as `403 eden://error/forbidden`. But 12a-1’s auth plan pins bad credentials as **401**, not 403, at [12a-1 D.1](/Users/ericalt/Documents/eden/docs/plans/eden-phase-12a-1-worker-identity.md:175) and [12a-1 §5.4](/Users/ericalt/Documents/eden/docs/plans/eden-phase-12a-1-worker-identity.md:677). So the revocation logic is relying on three different shapes:
  - `401 eden://reference-error/unauthorized` in 12a-1
  - `403 eden://error/unauthorized` in §5.4 here
  - `403 eden://error/forbidden` in §7.9 here

  Those need to be collapsed to one exact matrix before the edge-case behavior is actually implementable.

- The `reassign_task` churn tests are now specific enough that they need the claim-time error mapping to be explicit, but the plan still does not pin that mapping. [§7.10](/Users/ericalt/Documents/eden/docs/plans/eden-phase-12a-2-orchestrator-as-role.md:1291) expects claim attempts against a deleted target group to return `409 eden://error/illegal-transition`. But 12a-1 models this class of failure as `WorkerNotEligible` at claim time, not as a state-machine violation, at [12a-1 D.3](/Users/ericalt/Documents/eden/docs/plans/eden-phase-12a-1-worker-identity.md:283). If 12a-2 wants to make “stale target after reassign” a specific wire-visible 409 contract, that belongs in the spec/file-touch inventory; otherwise the test is overcommitting to one possible mapping.

**Overall Assessment**

This is very close to convergence. The remaining issue is not architectural anymore; it is the exact error vocabulary/status mapping for authority and claim-eligibility failures under these edge cases. Once that matrix is made consistent across §5.4, §7.9, §7.10, and the inherited 12a-1 behavior, I would consider the plan converged.