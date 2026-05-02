**Findings**

- Medium: the plan still overclaims coverage of chapter 06 §2(b) (`commit_sha` is set). The proposed `starting-trial-without-commit_sha` scenario in [docs/plans/eden-phase-11d-integrator-conformance.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-11d-integrator-conformance.md:173) does not isolate §2(b); that seed also violates §2(a) because the trial is still `starting`, and the plan itself acknowledges the IUT may reject on status first in [docs/plans/eden-phase-11d-integrator-conformance.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-11d-integrator-conformance.md:183). With the current reference IUT, `integrate_trial` checks only `status` and prior `trial_commit_sha`, not `commit_sha` presence, as shown in [reference/packages/eden-storage/src/eden_storage/_base.py](/Users/ericalt/Documents/eden/reference/packages/eden-storage/src/eden_storage/_base.py:907). So the revised scope text in [docs/plans/eden-phase-11d-integrator-conformance.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-11d-integrator-conformance.md:62) is still too strong. Either reframe this test as an overlapping negative consequence of §2 rather than proof of (b), or explicitly say the wire-only suite cannot independently certify (b) with the current harness contract.

- Medium: the reference-impl section is now factually wrong in two places. It says `_accept_integrate` performs the write and that the integrate precondition check “looks for `trial.commit_sha`” in [docs/plans/eden-phase-11d-integrator-conformance.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-11d-integrator-conformance.md:306). There is no `_accept_integrate`; the relevant method is `integrate_trial`, and it does not inspect `trial.commit_sha` at all in [reference/packages/eden-storage/src/eden_storage/_base.py](/Users/ericalt/Documents/eden/reference/packages/eden-storage/src/eden_storage/_base.py:885). That section needs correction before the plan is implementation-accurate.

- Low: the out-of-scope section still refers to a dropped test name. [docs/plans/eden-phase-11d-integrator-conformance.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-11d-integrator-conformance.md:203) says rollback is “captured by the `failed-integrate-leaves-no-artifacts` test above,” but that standalone test was removed earlier in the revised plan.

**Per-section assessment**

Spec scope correctness: mostly improved. The §2(a)/(b)/(c) split now reads correctly for (a) and (c) against [spec/v0/06-integrator.md](/Users/ericalt/Documents/eden/spec/v0/06-integrator.md:39) and [spec/v0/06-integrator.md](/Users/ericalt/Documents/eden/spec/v0/06-integrator.md:33). The remaining issue is just that the plan still describes §2(b) as positively covered when the proposed test cannot distinguish it from §2(a).

Coverage / citation-check: the two-group shape is correct. If chapter 9 §5 is updated so `Integrator atomicity` cites `06-integrator.md §3.4, §5.3` and `Promotion preconditions` cites `06-integrator.md §2`, `check_citations.py` will accept it; the parser reads all table rows under §5 and matches per-test citations against any cited section or descendant in the group, per [check_citations.py](/Users/ericalt/Documents/eden/conformance/src/conformance/tools/check_citations.py:49) and [check_citations.py](/Users/ericalt/Documents/eden/conformance/src/conformance/tools/check_citations.py:150).

Test design: the single composed cross-artifact success test is the right granularity. It captures the actual chapter 06 §3.4 wire-visible invariant better than two mirrored tests would, and the §5.3 divergent-resubmit test is correctly separated.

Chapter-9 amendment: the revised §4 scope is the right shape, and the singular-to-plural prose update for §5 is also correct. No issue there.

Reference-IUT impact: still understated because the plan text describes behavior the reference IUT does not currently implement or check. The tests themselves should still be fine if you reframe the §2(b) claim, but the plan’s “already supports all wire-observable chapter-06 projections” wording is too broad.

**Assessment**

Needs revisions.

The structure is now close and I would proceed after these edits:

- Narrow the §2(b) claim so the plan no longer says the suite independently covers `commit_sha is set`.
- Fix the reference-impl section to describe `integrate_trial` accurately and drop the nonexistent `trial.commit_sha` check claim.
- Remove or rename the stale `failed-integrate-leaves-no-artifacts` reference.

After that, the plan is ready.