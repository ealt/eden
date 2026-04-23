"""Exception hierarchy for every conforming EDEN store backend.

Chapter 8 (`spec/v0/08-storage.md`) specifies the observable
outcomes: "not found", "illegal transition", "wrong token", and
so on. The Protocol in [`protocol.py`](protocol.py) states those
outcomes in prose; this module names them. Every backend —
``InMemoryStore``, ``SqliteStore``, and any third-party
implementation — raises these types so conformance scenarios can
assert the rejection reason, not merely that something raised.

The legacy name ``DispatchError`` is preserved as an alias for the
pre-Phase-6 package layout, in which these types lived in
``eden_dispatch.errors``.
"""

from __future__ import annotations


class StorageError(Exception):
    """Base class for every store rejection."""


class NotFound(StorageError):
    """Referenced entity does not exist."""


class AlreadyExists(StorageError):
    """An insert collided with an existing identifier."""


class IllegalTransition(StorageError):
    """Requested state transition is not in the state machine."""


class WrongToken(StorageError):
    """Operation presented a token that does not match the current claim."""


class ConflictingResubmission(StorageError):
    """Resubmit disagreed with the previously-committed result payload."""


class InvalidPrecondition(StorageError):
    """Operation's referenced entity is not in the required state."""


DispatchError = StorageError
"""Legacy alias for pre-Phase-6 callers that import
``DispatchError`` from ``eden_dispatch.errors``.
"""
