**Findings**

1. **Feasibility**
- **Plan-blocking:** the plan centers a new first-party surface on `promtail` even though Grafana now treats Promtail as sunset software. As of **May 27, 2026**, Grafana’s docs say Promtail was in LTS through **February 28, 2026** and reached **EOL on March 2, 2026**. That makes the proposed `promtail` service, config tree, smoke, CI job, and operator docs a weak foundation for a fresh implementation plan. See [docs/plans/issue-110-loki-grafana-overlay.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-110-loki/docs/plans/issue-110-loki-grafana-overlay.md:30), [docs/plans/issue-110-loki-grafana-overlay.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-110-loki/docs/plans/issue-110-loki-grafana-overlay.md:63), [docs/plans/issue-110-loki-grafana-overlay.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-110-loki/docs/plans/issue-110-loki-grafana-overlay.md:81), [docs/plans/issue-110-loki-grafana-overlay.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-110-loki/docs/plans/issue-110-loki-grafana-overlay.md:202), [docs/plans/issue-110-loki-grafana-overlay.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-110-loki/docs/plans/issue-110-loki-grafana-overlay.md:228).
- The repo-local reasoning is otherwise solid: tailing the #109 JSONL bind-mount is lower-privilege and cleaner than `docker_sd_configs`, and Promtail file discovery does support `__path__` globs. But “can be made to work” is not the bar for a plan-stage doc here; the problem is choosing an already-EOL collector as new blessed infra.
- Smaller feasibility concern: the optional infra-log path is documented as “uncomment a block in `promtail-config.yaml` and add the socket mount” rather than as a dedicated overlay or other repo-owned switch. That is workable for ad hoc local hacking, but weak for a documented first-party operator path. See [docs/plans/issue-110-loki-grafana-overlay.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-110-loki/docs/plans/issue-110-loki-grafana-overlay.md:102).

2. **Missing context**
- Repo-specific context is generally strong. The plan correctly grounds itself in #109, current compose patterns, `setup-experiment.sh`, and the observability ladder.
- The major missing context is the upstream lifecycle status above. Without that, a reader sees “Promtail” as a normal dependency choice instead of a short-lived one that already needs a migration story.

Because that feasibility issue is significant, I would stop there rather than spend time on alternatives/completeness/edge cases as if the current approach were basically sound.

**Overall assessment**

Not ready as written. The JSONL-tail direction is the right local insight, but the plan should be reworked around a supported collector before merge, or explicitly downgraded to a consciously temporary stopgap with a tracked replacement.

**Sources**

- Plan: [docs/plans/issue-110-loki-grafana-overlay.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-110-loki/docs/plans/issue-110-loki-grafana-overlay.md:1)
- Project context: [AGENTS.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-110-loki/AGENTS.md:1)
- Grafana Promtail deprecation/EOL + file target discovery: https://grafana.com/docs/enterprise-logs/latest/send-data/promtail/scraping/
- Grafana Loki “Send log data” client guidance: https://grafana.com/docs/loki/latest/send-data/
- Grafana provisioning docs: https://grafana.com/docs/grafana/latest/administration/provisioning/
- Loki HTTP API (`/ready`, query endpoints): https://grafana.com/docs/loki/latest/reference/loki-http-api/