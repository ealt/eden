No findings on the contract gap. The revised plan now matches the shipped behavior:

- Claim-time wire errors redirect with a banner in [eden-phase-9-web-ui.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-9-web-ui.md:477), which matches [claim()](/Users/ericalt/Documents/eden/reference/services/web-ui/src/eden_web_ui/routes/planner.py:101).
- Phase-3 definitive submit errors now explicitly go to the orphan page with proposal IDs plus the canonical banner in [eden-phase-9-web-ui.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-9-web-ui.md:494), which matches [submit_plan()](/Users/ericalt/Documents/eden/reference/services/web-ui/src/eden_web_ui/routes/planner.py:267) and [_retry_submit()](/Users/ericalt/Documents/eden/reference/services/web-ui/src/eden_web_ui/routes/planner.py:282).
- Form-input preservation is now scoped to validation errors only, and that matches [TestValidationRecovery](/Users/ericalt/Documents/eden/reference/services/web-ui/tests/test_planner_flow.py:81).
- The four definitive chapter-07 names are covered in [TestDefinitiveSubmitErrors](/Users/ericalt/Documents/eden/reference/services/web-ui/tests/test_planner_flow.py:242).

I reran the relevant targeted checks during review; the remaining contract mismatch is closed. No new issues from this round.