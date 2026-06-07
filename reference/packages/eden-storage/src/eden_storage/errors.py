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


class ConflictingResubmission(StorageError):
    """Resubmit disagreed with the previously-committed result payload."""


class InvalidPrecondition(StorageError):
    """Operation's referenced entity is not in the required state."""


class WorkerNotRegistered(StorageError):
    """The supplied ``worker_id`` is not registered for the experiment.

    Raised by ``claim`` and ``submit`` when the binding-authenticated
    caller's id has no matching row in the per-experiment registry
    ([`spec/v0/04-task-protocol.md`](../../../../spec/v0/04-task-protocol.md)
    §3.5 step 2; §10).
    """


class WorkerNotEligible(StorageError):
    """The worker is registered but does not satisfy the task's ``target``.

    Raised by ``claim`` per
    [`spec/v0/04-task-protocol.md`](../../../../spec/v0/04-task-protocol.md)
    §3.5 step 3 — the task's ``target`` named a different worker, or a
    group that does not transitively contain the caller.
    """


class WrongClaimant(StorageError):
    """``submit`` from a worker that does not match ``task.claim.worker_id``.

    The atomic claim-match runs as part of the submit transition
    ([`spec/v0/04-task-protocol.md`](../../../../spec/v0/04-task-protocol.md)
    §4.1). A pre-flight read-then-compare in the binding would race; the
    Store-layer error is the only sound place to surface this.
    """


class NotClaimed(StorageError):
    """``submit`` against a task whose ``claim`` has been cleared.

    Raised when the task is not in ``claimed`` (or ``submitted``) — for
    example, the claim was reclaimed or the task already terminated.
    """


class InvalidName(StorageError):
    """``register_worker`` / ``register_group`` rejected an ill-formed display name.

    Raised when the operator-supplied ``name`` violates the display-name
    grammar in [`spec/v0/02-data-model.md`](../../../../spec/v0/02-data-model.md)
    §1.7 (NFC-normalized, 1..128 code points, no leading/trailing or
    all-whitespace, no control/surrogate/unassigned/private-use chars).
    The wire binds this to 422 ``eden://error/invalid-name``. Distinct
    from ``ReservedIdentifier`` (a well-formed but reserved name) and
    from ``InvalidPrecondition`` (a state/precondition failure).
    """


class ReservedIdentifier(StorageError):
    """``register_worker`` / ``register_group`` rejected a reserved name.

    Reserved values moved from id-space to name-space in the identity
    rename (issue #128): opaque ids are minted by the store, so the
    collision surface is the operator-supplied ``name``. The reserved
    worker names are ``admin`` / ``system`` / ``internal``; the reserved
    group names are ``admins`` / ``orchestrators`` (the latter rejected
    unless the privileged seed path passes ``allow_reserved=True``). See
    [`spec/v0/02-data-model.md`](../../../../spec/v0/02-data-model.md)
    §1.7 / §7. The wire binds this to 409
    ``eden://error/reserved-identifier``.
    """


class WorkerAlreadyRegistered(StorageError):
    """``register_worker`` collided with an existing record under different intent.

    Note: since the identity rename (issue #128) every ``register_worker``
    MINTS a fresh opaque ``worker_id`` (there is no id-based idempotency),
    so a collision under this error is structurally unreachable on the
    reference backends. Retained as a reserved type for third-party
    backends that may impose additional registry-integrity constraints.
    """


class CycleDetected(StorageError):
    """A group mutation would close a cycle in the group DAG.

    Raised by ``register_group``, ``add_to_group``, and any equivalent
    mutation per [`spec/v0/02-data-model.md`](../../../../spec/v0/02-data-model.md)
    §7.3. Detection is performed at write time inside the same
    transaction that performs the membership update.
    """


class NoOpVariant(StorageError):
    """Execution-task submission whose variant tree matches every parent's tree.

    Raised by ``submit`` (or, at IUT latitude, the accept-time
    validation path) per
    [`spec/v0/03-roles.md`](../../../../spec/v0/03-roles.md) §3.3
    non-no-op invariant + §3.4 rejection rule. A successful
    `VariantSubmission` whose `commit_sha` resolves to a git tree
    identical to the tree of every entry in `idea.parent_commits`
    represents the absence of a candidate, not a candidate, and MUST
    NOT terminalize the variant as ``success``.
    """


DispatchError = StorageError
"""Legacy alias for pre-Phase-6 callers that import
``DispatchError`` from ``eden_dispatch.errors``.
"""
