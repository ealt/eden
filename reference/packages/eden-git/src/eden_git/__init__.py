"""Reference git primitives + integrator for the EDEN protocol.

See [`spec/v0/06-integrator.md`](../../../../spec/v0/06-integrator.md)
for the normative contract. This package provides:

- the subprocess wrapper and plumbing primitives (``write_blob``,
  ``write_tree_from_entries`` / ``write_tree_with_file``,
  ``commit_tree``, ``create_ref`` / ``update_ref`` CAS), and
- the Phase-7b ``Integrator`` that composes them with a ``Store``
  to produce the §3.2 single-commit squash.
"""

from .errors import GitError, GitTransportError, RefRefused
from .integrator import (
    AtomicityViolation,
    CorruptIntegrationState,
    EvalManifestPathCollision,
    IntegrationResult,
    Integrator,
    IntegratorError,
    NotReadyForIntegration,
    ReachabilityViolation,
)
from .repo import GitRepo, Identity, TreeEntry, WorktreeInfo

__all__ = [
    "AtomicityViolation",
    "CorruptIntegrationState",
    "EvalManifestPathCollision",
    "GitError",
    "GitRepo",
    "GitTransportError",
    "Identity",
    "IntegrationResult",
    "Integrator",
    "IntegratorError",
    "NotReadyForIntegration",
    "ReachabilityViolation",
    "RefRefused",
    "TreeEntry",
    "WorktreeInfo",
]
