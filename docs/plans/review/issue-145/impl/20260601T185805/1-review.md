# Codex review — round 1

**Findings**

1. Round-0 finding 1: resolved. The config contract now matches the intended behavior: `--experiment-config` is optional in control-plane mode, `_resolve_default_config()` enforces the single-experiment vs control-plane startup rules, and `active_config()` no longer falls back from a non-default experiment to the default experiment’s config ([cli.py](/Users/ericalt/Documents/eden-worktrees/impl-issue-145-per-route-store-swap/reference/services/web-ui/src/eden_web_ui/cli.py:397), [routes/_helpers.py](/Users/ericalt/Documents/eden-worktrees/impl-issue-145-per-route-store-swap/reference/services/web-ui/src/eden_web_ui/routes/_helpers.py:247)).

2. Round-0 finding 2: resolved. Non-default experiment resolution now handles 401s by evicting cached state, retrying once, and routing a persistent auth failure to `cannot-bootstrap-credential` instead of leaking the 401 or misclassifying it as unseeded ([routes/_helpers.py](/Users/ericalt/Documents/eden-worktrees/impl-issue-145-per-route-store-swap/reference/services/web-ui/src/eden_web_ui/routes/_helpers.py:220)).

3. Round-0 finding 3: resolved. The per-experiment repo clones now have an explicit durable root via `--repo-root`, and Compose mounts that directory, so non-default experiment clones no longer land on transient container storage ([cli.py](/Users/ericalt/Documents/eden-worktrees/impl-issue-145-per-route-store-swap/reference/services/web-ui/src/eden_web_ui/cli.py:171), [cli.py](/Users/ericalt/Documents/eden-worktrees/impl-issue-145-per-route-store-swap/reference/services/web-ui/src/eden_web_ui/cli.py:347), [compose.yaml](/Users/ericalt/Documents/eden-worktrees/impl-issue-145-per-route-store-swap/reference/compose/compose.yaml:472)).

4. Round-0 finding 4: not fully resolved. The new stale-credential eviction path only runs for non-default selected experiments. The deployment-default fast path still returns immediately without any probe or retry, so if the default experiment’s credential goes stale after startup, requests against the default experiment remain stuck on the cached client until restart ([routes/_helpers.py](/Users/ericalt/Documents/eden-worktrees/impl-issue-145-per-route-store-swap/reference/services/web-ui/src/eden_web_ui/routes/_helpers.py:203), [routes/_helpers.py](/Users/ericalt/Documents/eden-worktrees/impl-issue-145-per-route-store-swap/reference/services/web-ui/src/eden_web_ui/routes/_helpers.py:220), [store_factory.py](/Users/ericalt/Documents/eden-worktrees/impl-issue-145-per-route-store-swap/reference/services/web-ui/src/eden_web_ui/store_factory.py:222)).
   Fix: give the default-experiment path the same evict-and-rebootstrap recovery, either in `resolve_active_experiment()` or at the first worker-store 401.

5. Round-0 finding 5: resolved. A cold-cache control-plane read failure now hides the switcher instead of rendering an empty dropdown ([routes/_helpers.py](/Users/ericalt/Documents/eden-worktrees/impl-issue-145-per-route-store-swap/reference/services/web-ui/src/eden_web_ui/routes/_helpers.py:435)).

**New issue**

1. Risk — [cli.py](/Users/ericalt/Documents/eden-worktrees/impl-issue-145-per-route-store-swap/reference/services/web-ui/src/eden_web_ui/cli.py:71). `_build_control_plane_client()` only catches `RuntimeError`. If control-plane bootstrap hits a transport or protocol failure during `whoami` / register / reissue, that exception still escapes and can fail web-ui startup outright. That is stricter than the rest of the design, which treats control-plane unavailability as a degraded runtime posture with banners / hidden switcher, not a hard service-start dependency.
   Fix: catch control-plane transport/protocol failures here as well, log a warning, and degrade to the same “switcher unavailable” posture rather than aborting startup.

Overall: 4 of the 5 round-0 findings are resolved; finding 4 is only partially fixed, and there is one new startup robustness risk in the control-plane bootstrap path.
