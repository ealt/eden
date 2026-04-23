"""Storage protocol + reference backends for the EDEN protocol.

See [`protocol.py`](protocol.py) for the ``Store`` structural
interface every backend satisfies, and [`memory.py`](memory.py) /
[`sqlite.py`](sqlite.py) for the two reference backends. Error
types and submission dataclasses live in
[`errors.py`](errors.py) and [`submissions.py`](submissions.py).
"""

from ._base import iter_events_by_type
from .errors import (
    AlreadyExists,
    ConflictingResubmission,
    DispatchError,
    IllegalTransition,
    InvalidPrecondition,
    NotFound,
    StorageError,
    WrongToken,
)
from .memory import InMemoryStore
from .protocol import Store
from .sqlite import SqliteStore
from .submissions import (
    EvaluateSubmission,
    ImplementSubmission,
    PlanSubmission,
    Submission,
    submissions_equivalent,
)

__all__ = [
    "AlreadyExists",
    "ConflictingResubmission",
    "DispatchError",
    "EvaluateSubmission",
    "IllegalTransition",
    "ImplementSubmission",
    "InMemoryStore",
    "InvalidPrecondition",
    "NotFound",
    "PlanSubmission",
    "SqliteStore",
    "StorageError",
    "Store",
    "Submission",
    "WrongToken",
    "iter_events_by_type",
    "submissions_equivalent",
]
