"""Shared submit-then-readback ladder for web-ui role routes.

The executor + evaluator routes both implement the same atomicity
contract from ``spec/v0/07-wire-protocol.md`` §2.4 + §8.1 around their
final submit step: try → retry transport-shape failures → on
indeterminate exhaustion read the task back and classify. Previously
each route carried its own copy of the ladder (audit
[`docs/audits/2026-05-20-code-quality-audit.md`](../../../../../../docs/audits/2026-05-20-code-quality-audit.md)
finding D-3).

This module owns the canonical implementation. Per-route differences:

- The executor must surface ``NoOpVariant`` to its caller (so the
  caller can run the §3.3 compensating-delete + error-resubmit path).
  ``NoOpVariant`` is NEVER caught here; it propagates out naturally.
- The evaluator must surface ``InvalidPrecondition`` to its caller
  as a form re-render outcome (metrics shape failed validation).
  Caller passes ``extra_catches=((InvalidPrecondition,
  "invalid-precondition"),)``.

Other exceptions follow the same shape across both routes:

- ``NotClaimed`` / ``WrongClaimant`` / ``IllegalTransition`` →
  read-back resolves to one of ``pending`` (we lost), terminal with
  our equivalent prior (we won, response lost), or terminal with a
  non-equivalent prior (conflict).
- ``ConflictingResubmission`` → definitive ``conflict`` short-circuit.
- Any other exception → transport-shaped; retry with backoff, then
  read-back on exhaustion.
"""

from __future__ import annotations

import time
from typing import Any

from eden_storage.errors import (
    ConflictingResubmission,
    IllegalTransition,
    InvalidPrecondition,
    NoOpVariant,
    NotClaimed,
    WrongClaimant,
)
from eden_storage.submissions import Submission, submissions_equivalent

# Canonical mapping from store-side exception type to the wire-protocol
# error name surfaced in operator-visible banners. Shared by every
# web-ui route that renders one of these as a human banner. Keys are
# the concrete exception types; values are the spec-defined
# ``eden://error/<name>`` identifiers.
WIRE_ERROR_NAMES: dict[type, str] = {
    NotClaimed: "eden://error/not-claimed",
    IllegalTransition: "eden://error/illegal-transition",
    ConflictingResubmission: "eden://error/conflicting-resubmission",
    InvalidPrecondition: "eden://error/invalid-precondition",
    NoOpVariant: "eden://error/no-op-variant",
}


def wire_error_banner(exc: BaseException) -> str:
    """Map a store-side exception to its spec-defined wire-error banner."""
    name = WIRE_ERROR_NAMES.get(type(exc))
    if name is None:
        return f"unexpected error: {exc.__class__.__name__}"
    return name


_RETRY_DELAYS_S = (0.05, 0.2, 0.5)


def submit_with_readback(
    *,
    store: Any,
    task_id: str,
    token: str,
    submission: Submission,
    extra_catches: tuple[tuple[type[BaseException], str], ...] = (),
) -> tuple[str, str | None]:
    """Submit with retry, then reconcile via committed-state read-back.

    Returns one of:

    - ``("ok", None)`` — the submit committed (clean call, retry, or
      read-back found an equivalent prior submission).
    - ``("auto", banner)`` — orphan page; auto-recovers via reclaim.
      Retry exhaustion with claim still ours, claim-handoff to another
      worker, or task back to ``pending``.
    - ``("conflict", banner)`` — orphan page; a different submission
      won the race.
    - ``("transport", banner)`` — orphan page; an implementation-
      illegal store state was observed during read-back, or the read-
      back probe itself failed.
    - Plus any ``outcome`` value listed in ``extra_catches``: each
      ``(exc_type, outcome)`` pair short-circuits the loop when
      ``exc_type`` is raised by ``submit``, returning
      ``(outcome, wire_error_banner(exc))``.

    ``NoOpVariant`` is deliberately NOT caught here — callers (the
    executor route) need to run the §3.3 compensating-delete path
    around this call.
    """
    last_exc: BaseException | None = None
    needs_readback = False
    for delay in _RETRY_DELAYS_S:
        try:
            store.submit(task_id, token, submission)
            return "ok", None
        except (NotClaimed, WrongClaimant, IllegalTransition) as exc:
            # 12a-1 atomic claim-match outcomes (NotClaimed,
            # WrongClaimant) plus the residual IllegalTransition
            # branch all share the same recovery shape: read-back
            # resolves to one of state==pending (we lost), state in
            # {completed, failed, submitted} with our equivalent
            # prior (we won, response lost), or state with non-
            # equivalent prior (conflict).
            last_exc = exc
            needs_readback = True
            break
        except ConflictingResubmission as exc:
            return "conflict", wire_error_banner(exc)
        except NoOpVariant:
            # Definitive: the variant tree matches every parent's
            # tree. Re-raise so the executor caller can run the
            # §3.3 cleanup path (delete remote/local refs, resubmit
            # `status="error"`). Treating this as a generic
            # `conflict` orphan would leak the Phase 1 variant +
            # Phase 2 work/* ref the caller has already created.
            raise
        except BaseException as exc:
            for exc_type, outcome in extra_catches:
                if isinstance(exc, exc_type):
                    return outcome, wire_error_banner(exc)
            if isinstance(exc, Exception):  # transport-shaped
                last_exc = exc
                time.sleep(delay)
                continue
            raise

    if not needs_readback and last_exc is None:
        # All retries returned cleanly is impossible (we'd return
        # "ok" inside the loop). Defensive.
        return "transport", "submit returned without exception or commit"

    return _readback(
        store=store,
        task_id=task_id,
        token=token,
        submission=submission,
        last_exc=last_exc,
    )


def _readback(
    *,
    store: Any,
    task_id: str,
    token: str,
    submission: Submission,
    last_exc: BaseException | None,
) -> tuple[str, str | None]:
    last_name = last_exc.__class__.__name__ if last_exc else "unknown"
    try:
        task = store.read_task(task_id)
    except Exception as exc:  # noqa: BLE001
        return (
            "transport",
            f"transport failure after retries; read-back failed: {exc.__class__.__name__}",
        )
    state = task.state
    if state == "claimed":
        if task.claim is not None and task.claim.worker_id == token:
            return ("auto", f"transport failure after retries: {last_name}")
        return "auto", "eden://error/not-claimed"
    if state in {"submitted", "completed", "failed"}:
        try:
            prior = store.read_submission(task_id)
        except Exception as exc:  # noqa: BLE001
            return (
                "transport",
                (
                    "transport failure after retries; "
                    f"read-submission failed: {exc.__class__.__name__}"
                ),
            )
        if prior is None:
            return (
                "transport",
                "store invariant violation: submission missing for terminal/submitted task",
            )
        if submissions_equivalent(prior, submission):
            return "ok", None
        return "conflict", "eden://error/conflicting-resubmission"
    # state == "pending"
    return ("auto", f"transport failure after retries; task reclaimed: {last_name}")
