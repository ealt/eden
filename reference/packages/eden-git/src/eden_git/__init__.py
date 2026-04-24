"""Reference git primitives for the EDEN integrator.

See [`spec/v0/06-integrator.md`](../../../../spec/v0/06-integrator.md)
for the normative contract. This package provides the subprocess
wrapper and plumbing primitives (write-blob, write-tree, commit-tree,
update-ref CAS) that the Phase-7b integrator composes into the
single-commit squash defined in §3.2.
"""

from .errors import GitError
from .repo import GitRepo, Identity, TreeEntry, WorktreeInfo

__all__ = [
    "GitError",
    "GitRepo",
    "Identity",
    "TreeEntry",
    "WorktreeInfo",
]
