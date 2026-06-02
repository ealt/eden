# Codex review — round 2

No remaining Bug/Risk findings in the files I re-reviewed.

The round-1 startup robustness risk is resolved. [_build_control_plane_client()`](/Users/ericalt/Documents/eden-worktrees/impl-issue-145-per-route-store-swap/reference/services/web-ui/src/eden_web_ui/cli.py:44) now catches both `httpx.TransportError` and control-plane `WireError` and degrades to the existing hidden-switcher / banner posture instead of aborting startup.

The remaining default-experiment credential-staleness gap is acceptable for `#145` as currently scoped. [resolve_active_experiment()](/Users/ericalt/Documents/eden-worktrees/impl-issue-145-per-route-store-swap/reference/services/web-ui/src/eden_web_ui/routes/_helpers.py:180) still preserves the zero-overhead default fast path at [lines 202-205](/Users/ericalt/Documents/eden-worktrees/impl-issue-145-per-route-store-swap/reference/services/web-ui/src/eden_web_ui/routes/_helpers.py:202), and the evict-and-rebootstrap recovery remains correctly applied to non-default selections at [lines 220-234](/Users/ericalt/Documents/eden-worktrees/impl-issue-145-per-route-store-swap/reference/services/web-ui/src/eden_web_ui/routes/_helpers.py:220). Since that default-path behavior matches pre-`#145` process-lifetime caching and is explicitly tracked in `#260`, I would treat it as a documented follow-up, not a blocker or regression in this change.

Overall assessment: the two round-2 points are acceptable, and I do not see any remaining Bug/Risk-level issues in `cli.py`, `routes/_helpers.py`, `store_factory.py`, or the reviewed wiring/docs slice.
