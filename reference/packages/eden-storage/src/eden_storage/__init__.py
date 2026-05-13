"""Storage protocol + reference backends for the EDEN protocol.

See [`protocol.py`](protocol.py) for the ``Store`` structural
interface every backend satisfies, and [`memory.py`](memory.py) /
[`sqlite.py`](sqlite.py) / [`postgres.py`](postgres.py) for the
three reference backends. Error types and submission dataclasses
live in [`errors.py`](errors.py) and [`submissions.py`](submissions.py).
"""

from ._base import RESERVED_IDENTIFIERS, iter_events_by_type
from .errors import (
    AlreadyExists,
    ConflictingResubmission,
    CycleDetected,
    DispatchError,
    IllegalTransition,
    InvalidPrecondition,
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
from .postgres import PostgresStore
from .protocol import Store
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
    "ConflictingResubmission",
    "CycleDetected",
    "DispatchError",
    "EvaluationSubmission",
    "IllegalTransition",
    "VariantSubmission",
    "InMemoryStore",
    "InvalidPrecondition",
    "NotClaimed",
    "NotFound",
    "IdeaSubmission",
    "PostgresStore",
    "RESERVED_IDENTIFIERS",
    "ReservedIdentifier",
    "SqliteStore",
    "StorageError",
    "Store",
    "Submission",
    "WorkerAlreadyRegistered",
    "WorkerNotEligible",
    "WorkerNotRegistered",
    "WrongClaimant",
    "iter_events_by_type",
    "submissions_equivalent",
]
