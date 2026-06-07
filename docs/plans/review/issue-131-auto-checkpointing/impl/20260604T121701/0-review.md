**Findings**

- **Must fix: legal sub-second cadences can overwrite checkpoint archives.** The schema allows any `number > 0` for `interval_seconds`, and the corpus explicitly accepts `0.5` at [cases.py](/Users/ericalt/Documents/eden-worktrees/impl-issue-131-auto-checkpointing/reference/packages/eden-contracts/tests/cases.py:801). But periodic filenames use second-granularity timestamps at [checkpoint_scheduler.py](/Users/ericalt/Documents/eden-worktrees/impl-issue-131-auto-checkpointing/reference/services/orchestrator/src/eden_orchestrator/checkpoint_scheduler.py:222), then `os.replace` overwrites the final path at [checkpoint_scheduler.py](/Users/ericalt/Documents/eden-worktrees/impl-issue-131-auto-checkpointing/reference/services/orchestrator/src/eden_orchestrator/checkpoint_scheduler.py:296). I reproduced two legal `0.1s` attempts in the same wall second: two exports ran, one `.tar` remained, containing the second payload. Either reject intervals below 1 second, or make filenames unique within a second.

- **Must fix: the CHANGELOG violates the deferral tracking rule.** [AGENTS.md](/Users/ericalt/Documents/eden-worktrees/impl-issue-131-auto-checkpointing/AGENTS.md:35) says CHANGELOG phrases like “out of scope” require an issue link in the same entry. The #131 entry says checkpoint compression is “out of scope” and “not filed” at [CHANGELOG.md](/Users/ericalt/Documents/eden-worktrees/impl-issue-131-auto-checkpointing/CHANGELOG.md:34). Remove that bullet from the completion record or file/link the issue.

- **Should fix: the auto-checkpoint smoke is weaker than the plan.** The plan requires each archive to carry `manifest.json` with populated wire-state JSONL at [issue-131-auto-checkpointing.md](/Users/ericalt/Documents/eden-worktrees/impl-issue-131-auto-checkpointing/docs/plans/issue-131-auto-checkpointing.md:381). The smoke only checks tar parse plus `manifest.json` presence at [smoke-auto-checkpoint.sh](/Users/ericalt/Documents/eden-worktrees/impl-issue-131-auto-checkpointing/reference/compose/healthcheck/smoke-auto-checkpoint.sh:241). Add a cheap `tar -xOf ... events.jsonl/tasks.jsonl/variants.jsonl | test nonempty` or manifest counts check.

**Assessment**

Plan adherence is strong overall. D1-D7 are implemented in the intended shape: orchestrator-local scheduler, seconds cadence, destination outside portable config, admin bearer export, best-effort loop isolation, observed-terminated terminal trigger, and schema/model parity. The two called-out deviations are justified: Compose hardcoding `/var/lib/eden/checkpoints` still preserves D3, and the smoke’s operator-bearer `dispatch_mode` flip is the right adaptation because config does not seed store dispatch mode.

Correctness is mostly solid around catch-up avoidance, retention scope, terminal restart-dedup, temp cleanup, and loop isolation. The sub-second filename overwrite is the main behavioral bug.

Verification run:

- `uv run pytest -q reference/services/orchestrator/tests/test_checkpoint_scheduler.py reference/services/orchestrator/tests/test_auto_checkpoint_wiring.py reference/packages/eden-contracts/tests/test_schema_parity.py`: `293 passed`
- `python3 scripts/check-complexity.py`: OK, no blocking violations
- `python3 scripts/check-rename-discipline.py`: clean
- `bash -n reference/compose/healthcheck/smoke-auto-checkpoint.sh`: clean

I did not run the Docker smoke or full `uv run pytest -q`.