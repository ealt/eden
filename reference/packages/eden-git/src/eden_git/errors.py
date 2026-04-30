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


class RefRefused(GitError):
    """Remote rejected a ref update.

    The remote either has a different SHA at the named ref (CAS
    miss) or refuses non-fast-forward.

    Distinct from :class:`GitTransportError`: ``RefRefused`` means the
    remote was reachable and authoritatively said no. Callers MAY treat
    this as a definite "the remote did not accept our publication" and
    skip the ls-remote read-back the integrator's transport-indeterminate
    branch performs.
    """


class GitTransportError(GitError):
    """A remote git operation failed at the transport layer.

    Possible causes: Gitea unreachable, DNS failure, TCP refused, TLS
    handshake error. The remote's state is INDETERMINATE — the request
    may have been received and applied before the response was lost.
    Callers driving ``trial/*`` writes (per chapter 6 §3.4) MUST run a
    follow-up ``ls-remote`` read-back to disambiguate before deciding
    whether to compensate.
    """
