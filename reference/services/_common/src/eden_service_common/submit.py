"""Shared submit helpers for the reference worker hosts.

All three worker-host submit paths (ideator / executor / evaluator)
share one retry-before-orphan + committed-state read-back discipline
(``07-wire-protocol.md`` §2.4 / §8.1): a transport-lost response must
not strand the task in ``claimed`` until the sweeper TTL, and a "we won,
response lost, orchestrator already terminalized" sequence must classify
as success rather than orphan. Before this module each host carried its
own copy (executor + evaluator identical; the ideator lacked read-back
entirely and used naked ``store.submit``); this is the single canonical
implementation.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence

from eden_storage import (
    ConflictingResubmission,
    DispatchError,
    IllegalTransition,
    InvalidPrecondition,
    NotClaimed,
    Store,
)
from eden_storage.submissions import Submission, submissions_equivalent

log = logging.getLogger(__name__)

# Backoff before the read-back fallback. Shared by every host.
_RETRY_DELAYS_S = (0.05, 0.2, 0.5)


def submit_with_readback(
    *,
    store: Store,
    task_id: str,
    token: str,
    submission: Submission,
    role: str,
    reraise: Sequence[type[BaseException]] = (),
) -> None:
    """Submit ``submission`` with retry-before-orphan + read-back.

    Definitive server-side rejections short-circuit (``NotClaimed``,
    ``ConflictingResubmission``, ``InvalidPrecondition``): a retry of the
    same payload is rejected the same way, and leaving the task hanging
    in ``claimed`` until the sweeper TTL is a worse outcome than a fast
    return. Transport-shaped failures retry on ``_RETRY_DELAYS_S``; on
    exhaustion (or an ``IllegalTransition`` that may mean "we already
    won, response lost, orchestrator terminalized") the committed
    submission is read back and compared for equivalence.

    ``reraise`` names exception types the caller must handle itself
    rather than have swallowed here — the executor passes ``NoOpVariant``
    so its success-submit path can route to ``status="error"`` and free
    the claim cleanly. ``role`` is only used to disambiguate log events.
    """
    reraise_tuple = tuple(reraise)
    last_exc: Exception | None = None
    for delay in (0.0, *_RETRY_DELAYS_S):
        if delay:
            time.sleep(delay)
        try:
            store.submit(task_id, token, submission)
            return
        except (NotClaimed, ConflictingResubmission, InvalidPrecondition):
            return
        except reraise_tuple:
            # Caller-owned: re-raise so it can route the outcome; a retry
            # of the same submission would be rejected the same way.
            raise
        except IllegalTransition:
            # The task may already be terminal (we won, response lost,
            # orchestrator already terminalized). Fall through to
            # read-back.
            last_exc = None
            break
        except DispatchError as exc:
            last_exc = exc
            continue
        except Exception as exc:  # noqa: BLE001 — transport-shaped
            last_exc = exc
            continue
    # Read-back classification.
    try:
        prior = store.read_submission(task_id)
    except Exception:  # noqa: BLE001
        if last_exc is not None:
            log.warning(
                "%s_submit_read_back_failed",
                role,
                extra={"task_id": task_id, "error": str(last_exc)},
            )
        return
    if prior is None:
        return
    if type(prior) is not type(submission):
        return
    if submissions_equivalent(prior, submission):
        return
    log.warning(
        "%s_submit_conflicts_with_committed",
        role,
        extra={"task_id": task_id},
    )
