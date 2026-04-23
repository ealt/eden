"""Exception hierarchy for the in-memory dispatch store.

Conformance scenarios distinguish these categories. The task protocol
(``spec/v0/04-task-protocol.md`` §1.2) requires callers to tell
"invalid" from "raced"; the finer-grained split here makes that
explicit — illegal transition, wrong token, missing precondition,
etc. all surface as distinct types so tests can assert the rejection
reason rather than just "it raised."
"""

from __future__ import annotations


class DispatchError(Exception):
    """Base class for every in-memory dispatch rejection."""


class NotFound(DispatchError):
    """Referenced entity does not exist."""


class AlreadyExists(DispatchError):
    """An insert collided with an existing identifier."""


class IllegalTransition(DispatchError):
    """Requested state transition is not in the state machine."""


class WrongToken(DispatchError):
    """Operation presented a token that does not match the current claim."""


class ConflictingResubmission(DispatchError):
    """Resubmit disagreed with the previously-committed result payload."""


class InvalidPrecondition(DispatchError):
    """Operation's referenced entity is not in the required state."""
