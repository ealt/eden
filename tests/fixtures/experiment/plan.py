"""Deterministic plan_command for the fixture experiment.

Long-running JSON-line worker that exercises the Phase 10d planner
subprocess protocol (``docs/plans/eden-phase-10d-llm-worker-hosts.md``
§D.2). For each ``plan`` dispatch line on stdin, emits one or more
``proposal`` lines followed by a ``plan-done`` terminator, all
scoped to the dispatch's ``task_id``.

Knobs:

- ``EDEN_BASE_COMMIT_SHA`` — required; the parent commit threaded
  into every proposal's ``parent_commits``.
- ``EDEN_PROPOSALS_PER_PLAN`` — optional, default 1.

Output is line-buffered so the host's reader sees lines promptly.
"""

from __future__ import annotations

import json
import os
import sys


def main() -> int:
    """Run the JSON-line plan loop."""
    base_commit_sha = os.environ.get("EDEN_BASE_COMMIT_SHA")
    if not base_commit_sha:
        print(
            json.dumps(
                {"event": "fatal", "reason": "EDEN_BASE_COMMIT_SHA must be set"}
            ),
            flush=True,
        )
        return 2
    proposals_per_plan = int(os.environ.get("EDEN_PROPOSALS_PER_PLAN", "1"))

    sys.stdout.write(json.dumps({"event": "ready"}) + "\n")
    sys.stdout.flush()

    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            dispatch = json.loads(line)
        except json.JSONDecodeError:
            continue
        if dispatch.get("event") != "plan":
            continue
        task_id = dispatch.get("task_id")
        if not isinstance(task_id, str):
            continue
        for i in range(proposals_per_plan):
            proposal = {
                "event": "proposal",
                "task_id": task_id,
                "slug": f"{task_id}-p{i}",
                "priority": float(proposals_per_plan - i),
                "parent_commits": [base_commit_sha],
                "rationale": (
                    f"Auto-generated rationale for {task_id} proposal {i}.\n"
                ),
            }
            sys.stdout.write(json.dumps(proposal, sort_keys=True) + "\n")
            sys.stdout.flush()
        sys.stdout.write(
            json.dumps({"event": "plan-done", "task_id": task_id}) + "\n"
        )
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
