# Codex review — round 3 (smoke-fix confirmation)

No Bug/Risk findings from this follow-up.

The fix is acceptable and it does not reintroduce round-0 Bug 1.

The key reason is the separation between startup resolution and per-request resolution:

- [_resolve_default_config()`](/Users/ericalt/Documents/eden-worktrees/impl-issue-145-per-route-store-swap/reference/services/web-ui/src/eden_web_ui/cli.py:412) now just decides whether the deployment-default config comes from `--experiment-config`, from `<config-dir>/<default>.yaml`, or is a startup misconfiguration. Removing the startup mismatch check only stops rejecting a harmless divergence.
- [active_config()](/Users/ericalt/Documents/eden-worktrees/impl-issue-145-per-route-store-swap/reference/services/web-ui/src/eden_web_ui/routes/_helpers.py:248) still preserves the load-bearing Bug-1 fix:
  - default experiment with a startup config returns `app.state.experiment_config` at [lines 262-264](/Users/ericalt/Documents/eden-worktrees/impl-issue-145-per-route-store-swap/reference/services/web-ui/src/eden_web_ui/routes/_helpers.py:262)
  - non-default experiments do not fall back to the default config; if no config dir is available they raise `ExperimentConfigMissing` at [lines 269-274](/Users/ericalt/Documents/eden-worktrees/impl-issue-145-per-route-store-swap/reference/services/web-ui/src/eden_web_ui/routes/_helpers.py:269)
- The existing regression test for the real Bug-1 condition is still the right guard: [test_resolve_active.py](/Users/ericalt/Documents/eden-worktrees/impl-issue-145-per-route-store-swap/reference/services/web-ui/tests/test_resolve_active.py:249).

I also checked for other web-ui code paths that might read `<config-dir>/<default>.yaml` directly; I didn’t find any outside `active_config()`. So, in the current implementation, a stale default entry in the config dir is operationally harmless.

Overall assessment: acceptable fix, no remaining Bug/Risk issues in this area. I did not rerun the Compose smokes myself in this pass.
