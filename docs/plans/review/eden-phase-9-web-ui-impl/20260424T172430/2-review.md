**Findings**
- Medium: not sufficiently addressed against the plan contract. The code still sends submit-time `WrongToken` / `IllegalTransition` / `ConflictingResubmission` / `InvalidPrecondition` to the orphan page in [_retry_submit()](/Users/ericalt/Documents/eden/reference/services/web-ui/src/eden_web_ui/routes/planner.py:282) and [submit_plan()](/Users/ericalt/Documents/eden/reference/services/web-ui/src/eden_web_ui/routes/planner.py:267), while the plan still says those `store.submit` errors should re-render the planner list with a canonical banner, “claim again,” and preserved input in [eden-phase-9-web-ui.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-9-web-ui.md:477). The new tests in [test_planner_flow.py](/Users/ericalt/Documents/eden/reference/services/web-ui/tests/test_planner_flow.py:242) and the tightened docstring in [planner.py](/Users/ericalt/Documents/eden/reference/services/web-ui/src/eden_web_ui/routes/planner.py:307) justify and pin the current behavior, but they do not make it match the review contract.

No new issues jumped out beyond that. I reran the new `TestDefinitiveSubmitErrors` block locally and it passed.

If you want this review to clear, one of these has to happen:
- Change the implementation to match the current plan text.
- Or update the plan to explicitly bless the orphan-page behavior for definitive Phase-3 submit errors, distinguishing it from pre-mutation validation recovery.

Given your rationale, updating the plan is the cleaner fix.