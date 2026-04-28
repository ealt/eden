"""Deterministic evaluate_command for the fixture experiment.

Short-lived per-task subprocess. Reads ``EDEN_TASK_JSON`` for the
trial context (commit_sha + metrics_schema), emits a deterministic
metric value matching the ``ScriptedEvaluator`` shape, and writes
the result to ``EDEN_OUTPUT``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def _value_for_kind(kind: str, sha: str) -> Any:
    """Return a deterministic metric value matching ``make_evaluate_fn``.

    Matches the scripted evaluator's shape so the compose-smoke
    assertions keep working unchanged.
    """
    seed = int(sha[:8], 16) if sha else 0
    if kind == "real":
        return 0.5 + (seed % 3) * 0.1
    if kind == "integer":
        return seed
    return f"value-{seed}"


def main() -> int:
    """Run the per-task evaluate step."""
    task_json_rel = os.environ.get("EDEN_TASK_JSON", ".eden/eval-task.json")
    output_rel = os.environ.get("EDEN_OUTPUT", ".eden/eval-outcome.json")
    cwd = Path.cwd()
    task = json.loads((cwd / task_json_rel).read_text(encoding="utf-8"))
    metrics_schema: dict[str, str] = task.get("metrics_schema") or {}
    commit_sha = task.get("trial_commit_sha") or ""
    metrics: dict[str, Any] = {
        name: _value_for_kind(kind, commit_sha)
        for name, kind in metrics_schema.items()
    }
    outcome = {
        "status": "success",
        "metrics": metrics,
    }
    (cwd / output_rel).write_text(json.dumps(outcome, sort_keys=True), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
