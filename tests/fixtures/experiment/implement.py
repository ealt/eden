r"""Deterministic implement_command for the fixture experiment.

Short-lived per-task subprocess. Reads ``EDEN_TASK_JSON`` for the
proposal context, writes a deterministic blob into the worktree,
runs ``git add`` + ``git commit`` to produce a single commit on
``parent_commits[0]``, and writes the resulting commit SHA to
``EDEN_OUTPUT``.

The blob shape mirrors ``ScriptedImplementer`` so the existing
compose-smoke assertions keep working unchanged:

  * one file at ``<slug>.txt``
  * content ``f"trial={trial_id!r} slug={slug!r}\n"``
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        # Surface git's stderr so smoke / e2e diagnostics can show
        # what actually broke; otherwise check=True's
        # CalledProcessError swallows it.
        print(
            f"implement.py: git {' '.join(args)} failed (rc={result.returncode}); "
            f"stdout={result.stdout!r}; stderr={result.stderr!r}",
            file=sys.stderr,
        )
        sys.exit(2)
    return result.stdout.strip()


def main() -> int:
    """Run the per-task implement step."""
    task_json_rel = os.environ.get("EDEN_TASK_JSON", ".eden/task.json")
    output_rel = os.environ.get("EDEN_OUTPUT", ".eden/outcome.json")
    cwd = Path.cwd()
    task = json.loads((cwd / task_json_rel).read_text(encoding="utf-8"))
    slug = task["proposal_slug"]
    trial_id = task["trial_id"]
    payload_path = cwd / f"{slug}.txt"
    payload_path.write_text(
        f"trial={trial_id!r} slug={slug!r}\n",
        encoding="utf-8",
    )
    _git("add", str(payload_path), cwd=cwd)
    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", "EDEN Implementer")
    env.setdefault("GIT_AUTHOR_EMAIL", "implementer@eden.invalid")
    env.setdefault("GIT_COMMITTER_NAME", "EDEN Implementer")
    env.setdefault("GIT_COMMITTER_EMAIL", "implementer@eden.invalid")
    env.setdefault("GIT_AUTHOR_DATE", "2026-04-01T00:00:00+00:00")
    env.setdefault("GIT_COMMITTER_DATE", "2026-04-01T00:00:00+00:00")
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", f"eden: {slug} ({trial_id})"],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    commit_sha = _git("rev-parse", "HEAD", cwd=cwd)
    outcome = {
        "status": "success",
        "commit_sha": commit_sha,
    }
    (cwd / output_rel).write_text(json.dumps(outcome, sort_keys=True), encoding="utf-8")
    print(f"implement.py: wrote {commit_sha}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
