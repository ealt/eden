"""Deterministic ideation_command for the fixture experiment.

Long-running JSON-line worker that exercises the Phase 10d ideator
subprocess protocol (``docs/plans/eden-phase-10d-llm-worker-hosts.md``
§D.2). For each ``ideate`` dispatch line on stdin, emits one or more
``idea`` lines followed by a ``ideation-done`` terminator, all
scoped to the dispatch's ``task_id``.

Knobs:

- ``EDEN_BASE_COMMIT_SHA`` — required; the parent commit threaded
  into every idea's ``parent_commits``.
- ``EDEN_IDEAS_PER_IDEATION`` — optional, default 1.

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
    ideas_per_ideation = int(os.environ.get("EDEN_IDEAS_PER_IDEATION", "1"))

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
        if dispatch.get("event") != "ideation":
            continue
        task_id = dispatch.get("task_id")
        if not isinstance(task_id, str):
            continue
        for i in range(ideas_per_ideation):
            idea = {
                "event": "idea",
                "task_id": task_id,
                "slug": f"{task_id}-p{i}",
                "priority": float(ideas_per_ideation - i),
                "parent_commits": [base_commit_sha],
                "rationale": (
                    f"Auto-generated rationale for {task_id} idea {i}.\n"
                ),
            }
            sys.stdout.write(json.dumps(idea, sort_keys=True) + "\n")
            sys.stdout.flush()
        sys.stdout.write(
            json.dumps({"event": "ideation-done", "task_id": task_id}) + "\n"
        )
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
