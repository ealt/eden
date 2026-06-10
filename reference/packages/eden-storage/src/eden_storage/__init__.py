"""Storage protocol + reference backends for the EDEN protocol.

See [`protocol.py`](protocol.py) for the ``Store`` structural
interface every backend satisfies, and [`memory.py`](memory.py) /
[`sqlite.py`](sqlite.py) / [`postgres.py`](postgres.py) for the
three reference backends. Error types and submission dataclasses
live in [`errors.py`](errors.py) and [`submissions.py`](submissions.py).
"""

from ._base import RESERVED_GROUP_NAMES, RESERVED_WORKER_NAMES
from ._checkpoint import ImportResult
from ._ops.events import iter_events_by_type
from .artifact_backend import (
    ArtifactBackend,
    FileArtifactBackend,
    GcsBackend,
    InMemoryArtifactBackend,
    S3Backend,
)
from .errors import (
    AlreadyExists,
    ConflictingResubmission,
    CycleDetected,
    DispatchError,
    IllegalTransition,
    InvalidName,
    InvalidPrecondition,
    NoOpVariant,
    NotClaimed,
    NotFound,
    ReservedIdentifier,
    StorageError,
    WorkerAlreadyRegistered,
    WorkerNotEligible,
    WorkerNotRegistered,
    WrongClaimant,
)
from .memory import InMemoryStore
from .postgres import PostgresStore, ensure_readonly_role
from .protocol import ArtifactStore, Store
from .sqlite import SqliteStore
from .submissions import (
    EvaluationSubmission,
    IdeaSubmission,
    Submission,
    VariantSubmission,
    submissions_equivalent,
)

__all__ = [
    "AlreadyExists",
    "ArtifactBackend",
    "ArtifactStore",
    "ConflictingResubmission",
    "CycleDetected",
    "DispatchError",
    "EvaluationSubmission",
    "FileArtifactBackend",
    "GcsBackend",
    "IllegalTransition",
    "VariantSubmission",
    "InMemoryArtifactBackend",
    "S3Backend",
    "InMemoryStore",
    "InvalidName",
    "InvalidPrecondition",
    "NotClaimed",
    "NotFound",
    "IdeaSubmission",
    "ImportResult",
    "NoOpVariant",
    "PostgresStore",
    "RESERVED_GROUP_NAMES",
    "RESERVED_WORKER_NAMES",
    "ReservedIdentifier",
    "SqliteStore",
    "StorageError",
    "Store",
    "Submission",
    "WorkerAlreadyRegistered",
    "WorkerNotEligible",
    "WorkerNotRegistered",
    "WrongClaimant",
    "ensure_readonly_role",
    "iter_events_by_type",
    "submissions_equivalent",
]
