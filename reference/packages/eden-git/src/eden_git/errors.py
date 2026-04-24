"""Typed errors for eden-git operations."""

from __future__ import annotations


class GitError(RuntimeError):
    """A ``git`` subprocess returned a non-zero exit code.

    Carries the executed argv, exit code, stdout, and stderr so callers
    can distinguish failure modes (ref-format violation, missing object,
    stale CAS, dirty worktree) without re-parsing log lines.
    """

    def __init__(
        self,
        argv: list[str],
        returncode: int,
        stdout: str,
        stderr: str,
    ) -> None:
        self.argv = list(argv)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        detail = stderr.strip() or stdout.strip() or f"exit code {returncode}"
        super().__init__(f"git {' '.join(argv)} failed: {detail}")
