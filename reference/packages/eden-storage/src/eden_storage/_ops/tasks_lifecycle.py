"""Task-lifecycle operations mixin (chapters 03, 04, 05 §2.2).

Split out of the task mixin under the plan §3.7 fallback. Owns the
task reads + claim / submit / accept / reject / reclaim / reassign
transitions, the per-role accept/reject composite-commit helpers,
and the submit-gate / acceptance validators. Pairs with
``_TaskCreateOpsMixin``.
"""

from __future__ import annotations

import copy
from datetime import datetime
from typing import Any

from eden_contracts import (
    EvaluationTask,
    ExecutionTask,
    FailReason,
    ReclaimCause,
    Task,
    TaskClaim,
    TaskTarget,
    Variant,
)
from pydantic import ValidationError

from .._base import _StoreCore, _Tx
from ..errors import (
    ConflictingResubmission,
    IllegalTransition,
    InvalidPrecondition,
    NoOpVariant,
    NotClaimed,
    NotFound,
    WorkerNotEligible,
    WorkerNotRegistered,
    WrongClaimant,
)
from ..submissions import (
    EvaluationSubmission,
    IdeaSubmission,
    Submission,
    VariantSubmission,
    submissions_equivalent,
)
from ._helpers import (
    _all_parents_equal_sha,
    _deep,
    _no_op_check_inputs,
    _resolve_trees,
    _sha_equality_message,
    _tree_identity_message,
    _validated_update,
)


class _TaskLifecycleOpsMixin(_StoreCore):
    """Task reads + claim/submit/accept/reject/reclaim/reassign + validators."""

    def read_task(self, task_id: str) -> Task:
        """Return a snapshot of the current task, or raise ``NotFound``."""
        with self._atomic_operation():
            task = self._get_task(task_id)
            if task is None:
                raise NotFound(f"task {task_id!r}")
            return _deep(task)

    def read_submission(self, task_id: str) -> Submission | None:
        """Return the committed submission for a task, or ``None`` if not submitted.

        Returns a deep copy. Submission dataclasses are ``frozen``,
        but their nested ``metrics`` dict is not; without deep copy a
        caller could mutate the committed metrics in place and
        corrupt future idempotency decisions.
        """
        with self._atomic_operation():
            submission = self._get_submission(task_id)
            if submission is None:
                return None
            return copy.deepcopy(submission)

    def list_tasks(
        self,
        *,
        kind: str | None = None,
        state: str | None = None,
    ) -> list[Task]:
        """Return snapshots of tasks matching an optional ``kind`` and ``state``."""
        with self._atomic_operation():
            return [_deep(task) for task in self._iter_tasks(kind=kind, state=state)]

    def claim(
        self,
        task_id: str,
        worker_id: str,
        *,
        expires_at: datetime | str | None = None,
    ) -> TaskClaim:
        """Transition a pending task to claimed.

        Per
        [`spec/v0/04-task-protocol.md`](../../../../spec/v0/04-task-protocol.md)
        §3.5, the claim enforces three preconditions atomically with
        the state write:

        1. The task is in ``pending`` state (§3.4) — else
           :class:`IllegalTransition`.
        2. ``worker_id`` is registered for this experiment (§3.5 step 2)
           — else :class:`WorkerNotRegistered`.
        3. ``worker_id`` satisfies ``task.target`` (§3.5 step 3): a
           ``worker`` target requires ``worker_id == target.id``; a
           ``group`` target requires transitive membership; absent
           target permits any registered worker. Failure raises
           :class:`WorkerNotEligible`.

        The Store trusts the supplied ``worker_id`` as data;
        authentication of the caller against that id is the binding's
        responsibility (§3.3).
        """
        with self._atomic_operation():
            # §3.5 step 0 (12a-3): terminated-experiment guard runs
            # before the state precondition so a `pending` task in a
            # terminated experiment is unreachable, not "claim
            # rejected because not pending".
            self._require_running()
            task = self._require_task(task_id)
            if task.state != "pending":
                raise IllegalTransition(
                    f"cannot claim task in state {task.state!r} (must be 'pending')"
                )
            # §3.5 step 2: registration check. The check runs even when
            # the target is null; an unregistered worker MUST NOT claim
            # an open task per the spec.
            if self._get_worker(worker_id) is None:
                raise WorkerNotRegistered(
                    f"worker_id={worker_id!r} is not registered for "
                    f"experiment {self._experiment_id!r}"
                )
            # §3.5 step 3: target eligibility.
            target = task.target
            if target is not None:
                if target.kind == "worker":
                    if worker_id != target.id:
                        raise WorkerNotEligible(
                            f"task.target requires worker_id={target.id!r}; "
                            f"caller is {worker_id!r}"
                        )
                else:  # target.kind == "group"
                    # The public resolver re-enters the atomic-operation
                    # context (RLock + `_in_txn` guard), so calling it
                    # from inside `claim`'s transaction is safe.
                    if not self.resolve_worker_in_group(
                        worker_id, target.id
                    ):
                        raise WorkerNotEligible(
                            f"task.target requires membership in group "
                            f"{target.id!r}; worker_id={worker_id!r} is not "
                            "transitively a member"
                        )
            claimed_at = self._ts()
            claim_kwargs: dict[str, Any] = {
                "worker_id": worker_id,
                "claimed_at": claimed_at,
            }
            exp = self._maybe_ts(expires_at)
            if exp is not None:
                claim_kwargs["expires_at"] = exp
            claim = TaskClaim(**claim_kwargs)
            now = self._ts()
            tx = _Tx()
            tx.tasks[task_id] = _validated_update(
                task, state="claimed", claim=claim, updated_at=now
            )
            tx.events.append(
                self._event(
                    "task.claimed",
                    {"task_id": task_id, "worker_id": worker_id},
                )
            )
            self._apply_commit(tx)
            return _deep(claim)

    def submit(
        self, task_id: str, worker_id: str, submission: Submission
    ) -> None:
        """Transition a claimed task to submitted, persisting the result.

        Per ``04-task-protocol.md`` §4.1, the atomic claim-match
        ``task.claim.worker_id == worker_id`` runs **as part of the
        submit transition** — a non-atomic "read claim, compare, then
        write" sequence would introduce a TOCTOU race against reclaim.
        Mismatches raise ``WrongClaimant`` (claim exists, different
        worker) or ``NotClaimed`` (claim cleared / task not in
        ``claimed``).

        Idempotent per §4.2: a resubmit by the same claimant with a
        content-equivalent payload MUST succeed without mutating state;
        an inconsistent resubmit MUST be rejected. Authentication of
        the caller against ``worker_id`` is the binding's
        responsibility (§3.3); the Store trusts the parameter as data.

        Atomically writes ``task.submitted_by = worker_id`` so the
        claimant identity survives the terminal transitions that clear
        ``claim``.
        """
        with self._atomic_operation():
            task = self._require_task(task_id)
            if task.state not in {"claimed", "submitted"}:
                # NotClaimed covers reclaimed-or-terminal tasks; non-claimed
                # states all share the inverse of the §4.1 step-1 precondition.
                raise NotClaimed(
                    f"task {task_id!r} is in state {task.state!r}, not 'claimed'"
                )
            if task.claim is None:
                raise NotClaimed(
                    f"task {task_id!r} has no active claim (claim cleared by reclaim "
                    "or terminal transition)"
                )
            # §3.5 step 2 (extended to submit): the submitting worker_id MUST
            # still be a registered worker. A worker that was registered at
            # claim time and then removed from the registry MUST NOT be able
            # to submit. Surfaces as the same WorkerNotRegistered error the
            # claim path uses so the binding maps to the same wire status.
            if self._get_worker(worker_id) is None:
                raise WorkerNotRegistered(
                    f"submit by worker_id={worker_id!r} rejected: worker is "
                    f"not registered for experiment {self._experiment_id!r}"
                )
            if task.claim.worker_id != worker_id:
                raise WrongClaimant(
                    f"submit by worker_id={worker_id!r} does not match "
                    f"task.claim.worker_id={task.claim.worker_id!r}"
                )
            self._require_submission_kind_matches(task, submission)
            self._validate_submission_ref_binding(task, submission)

            if task.state == "submitted":
                # §4.2 idempotency precedes the no-op check: a
                # content-equivalent retry of an already-committed
                # submission MUST be accepted without re-evaluation.
                # Re-running `_validate_non_no_op_variant` here could
                # raise `NoOpVariant` on a previously-accepted submit
                # if the tree resolver's view of the SHAs has shifted
                # since the first commit (e.g. submit-time resolver
                # transient miss now resolved), which would violate
                # §4.2.
                prior = self._get_submission(task_id)
                if prior is None:
                    raise IllegalTransition(
                        f"task {task_id!r} is submitted but has no recorded submission"
                    )
                if not submissions_equivalent(prior, submission):
                    raise ConflictingResubmission(
                        f"resubmit of {task_id!r} disagrees with committed result"
                    )
                return

            # 03-roles.md §3.3 non-no-op invariant + §3.4 rejection
            # rule. Content-derived (depends only on submission fields
            # and the idea's parent_commits), so enforced at submit
            # time for fresh submissions. Resubmits against a still-
            # `submitted` task short-circuit at the §4.2 equivalence
            # check above, never reaching here.
            self._validate_non_no_op_variant(task, submission)

            now = self._ts()
            tx = _Tx()
            tx.tasks[task_id] = _validated_update(
                task, state="submitted", submitted_by=worker_id, updated_at=now
            )
            tx.submissions[task_id] = copy.deepcopy(submission)
            tx.events.append(self._event("task.submitted", {"task_id": task_id}))
            self._apply_commit(tx)

    def validate_acceptance(self, task_id: str) -> str | None:
        """Return an orchestrator-side reason to reject this submission, or ``None``.

        ``04-task-protocol.md`` §4.3 requires the orchestrator to
        transition a submitted task to ``failed`` when the result
        violates the role's success contract, even if the worker's
        declared status was ``success``. This helper performs that
        validation without mutating state; the driver calls it
        before deciding between ``accept`` and ``reject``.
        """
        with self._atomic_operation():
            return self._validate_acceptance_locked(task_id)

    def _validate_acceptance_locked(self, task_id: str) -> str | None:
        task = self._require_task(task_id)
        if task.state != "submitted":
            return None
        submission = self._get_submission(task_id)
        if submission is None:
            return "no submission recorded"
        if isinstance(submission, IdeaSubmission):
            if submission.status != "success":
                return None
            return self._validate_ideation_acceptance(submission)
        if isinstance(submission, VariantSubmission):
            if submission.status != "success":
                return None
            assert isinstance(task, ExecutionTask)
            return self._validate_execution_acceptance(task, submission)
        if isinstance(submission, EvaluationSubmission):
            if submission.status != "success":
                return None
            assert isinstance(task, EvaluationTask)
            return self._validate_evaluate_acceptance(task, submission)
        return None

    def validate_terminal(self, task_id: str) -> tuple[str, str | None]:
        """Decide how to terminalize a submitted task.

        Returns one of:
          * ``("accept", None)`` — worker-declared success and result
            satisfies the role's success contract.
          * ``("reject_worker", None)`` — worker-declared error/evaluation_error,
            and the recorded result is writable as specified in
            ``03-roles.md`` §2.4/§3.4/§4.4.
          * ``("reject_validation", reason)`` — the orchestrator must
            treat this submission as a validation failure per
            ``04-task-protocol.md`` §4.3, either because the worker
            declared ``success`` with a malformed payload, or because
            the worker declared ``error`` but the accompanying variant
            fields (metrics, artifacts_uri) would not validate.

        The driver calls this before every ``accept`` / ``reject`` and
        uses the returned decision to pick the correct `reason`.
        """
        with self._atomic_operation():
            task = self._require_task(task_id)
            if task.state != "submitted":
                return ("accept", None)
            submission = self._get_submission(task_id)
            if submission is None:
                return ("reject_validation", "no submission recorded")
            if submission.status == "success":
                reason = self._validate_acceptance_locked(task_id)
                if reason is not None:
                    return ("reject_validation", reason)
                return ("accept", None)
            # status is "error" or "evaluation_error" — worker_error is the
            # default, but a malformed payload on `error` still has to
            # be treated as validation_error so no invalid field
            # actually lands on the variant.
            if isinstance(submission, EvaluationSubmission) and submission.status == "error":
                assert isinstance(task, EvaluationTask)
                reason = self._validate_evaluate_error(task, submission)
                if reason is not None:
                    return ("reject_validation", reason)
            return ("reject_worker", None)

    def accept(self, task_id: str) -> None:
        """Orchestrator accept: ``submitted → completed`` with composite effects.

        Dispatches by task kind to emit the right composite-commit
        events (``05-event-protocol.md`` §2.2). Clears the task's
        ``claim``.
        """
        with self._atomic_operation():
            task = self._require_task(task_id)
            if task.state != "submitted":
                raise IllegalTransition(
                    f"cannot accept task in state {task.state!r}"
                )
            submission = self._get_submission(task_id)
            if submission is None:
                raise IllegalTransition(
                    f"task {task_id!r} is submitted but has no recorded submission"
                )
            if task.kind == "ideation":
                self._accept_ideation(task, submission)
            elif task.kind == "execution":
                self._accept_execution(task, submission)
            else:
                self._accept_evaluation(task, submission)

    def reject(self, task_id: str, reason: FailReason) -> None:
        """Orchestrator reject: ``submitted → failed`` with composite effects.

        Dispatches by task kind. For evaluation tasks, the variant-side
        effect depends on whether the worker declared ``error`` or
        ``evaluation_error`` (``04-task-protocol.md`` §4.3).
        """
        with self._atomic_operation():
            task = self._require_task(task_id)
            if task.state != "submitted":
                raise IllegalTransition(
                    f"cannot reject task in state {task.state!r}"
                )
            submission = self._get_submission(task_id)
            if submission is None:
                raise IllegalTransition(
                    f"task {task_id!r} is submitted but has no recorded submission"
                )
            if task.kind == "ideation":
                self._reject_ideation(task, reason)
            elif task.kind == "execution":
                self._reject_execution(task, submission, reason)
            else:
                self._reject_evaluate(task, submission, reason)

    def reclaim(self, task_id: str, cause: ReclaimCause) -> None:
        """Move a claimed (or operator-reclaimed submitted) task back to pending.

        Automatic causes (``expired``, ``health_policy``) are rejected
        against ``submitted`` tasks per ``04-task-protocol.md`` §5.1;
        only ``operator`` reclaim is permitted from ``submitted``. If
        the reclaimed task is an ``execution`` whose prior worker run
        left a variant in ``starting``, the variant transitions to
        ``error`` atomically with the reclaim
        (``05-event-protocol.md`` §2.2).
        """
        with self._atomic_operation():
            task = self._require_task(task_id)
            if task.state in {"completed", "failed"}:
                raise IllegalTransition(
                    f"cannot reclaim terminal task {task_id!r} (state={task.state!r})"
                )
            if task.state not in {"claimed", "submitted"}:
                raise IllegalTransition(
                    f"cannot reclaim task in state {task.state!r}"
                )
            if task.state == "submitted" and cause != "operator":
                raise IllegalTransition(
                    "automatic reclaim (expired, health_policy) is not permitted "
                    "against a submitted task; only operator reclaim is"
                )

            now = self._ts()
            tx = _Tx()
            tx.tasks[task_id] = _validated_update(
                task, state="pending", claim=None, updated_at=now
            )
            if self._get_submission(task_id) is not None:
                tx.task_deletes_submission.add(task_id)
            tx.events.append(
                self._event("task.reclaimed", {"task_id": task_id, "cause": cause})
            )

            if task.kind == "execution":
                variant = self._find_starting_variant_for_implement_task(task)
                if variant is not None:
                    tx.variants[variant.variant_id] = _validated_update(
                        variant, status="error", completed_at=now
                    )
                    tx.events.append(
                        self._event("variant.errored", {"variant_id": variant.variant_id})
                    )

            self._apply_commit(tx)

    def reassign_task(
        self,
        task_id: str,
        new_target: TaskTarget | None,
        *,
        reason: str,
        reassigned_by: str,
    ) -> Task:
        """Atomically update a task's ``target`` per chapter 04 §6.

        ``pending`` → field update + ``task.reassigned`` event.
        ``claimed`` → composite-commit: clear the claim,
        emit ``task.reclaimed`` (``cause="operator"``), update target,
        emit ``task.reassigned``. The reclaim path follows the
        execution-task variant-side rule from ``reclaim``: if the
        claimant was an executor whose in-flight variant is still in
        ``starting``, the variant transitions to ``error`` atomically.
        ``submitted`` / terminal → ``InvalidPrecondition``; the spec
        forbids reassign past the claimed phase to preserve
        attribution-of-submission contracts.

        Reason text must be non-empty (``05-event-protocol.md`` §3.1
        carries it in the event payload). ``reassigned_by`` must match
        the §6.1 grammar; binding-layer authorization is the caller's
        responsibility.
        """
        if not reason:
            raise InvalidPrecondition("reassign_task requires a non-empty reason")
        self._validate_registry_id(reassigned_by, kind="actor")
        with self._atomic_operation():
            task = self._require_task(task_id)
            if task.state not in {"pending", "claimed"}:
                raise InvalidPrecondition(
                    f"cannot reassign task {task_id!r} in state {task.state!r}; "
                    "only pending or claimed tasks may be reassigned "
                    "(04-task-protocol.md §6.1)"
                )
            # No-op idempotency: same target shape → emit nothing.
            if (
                self._targets_equal(task.target, new_target)
                and task.state == "pending"
            ):
                return _deep(task)

            now = self._ts()
            tx = _Tx()
            tx.tasks[task_id] = _validated_update(
                task,
                target=new_target,
                state="pending",
                claim=None,
                updated_at=now,
            )

            if task.state == "claimed":
                self._stage_reassign_reclaim(tx, task, now)

            tx.events.append(
                self._event(
                    "task.reassigned",
                    self._reassign_event_payload(
                        task_id, new_target, reason, reassigned_by
                    ),
                )
            )
            self._apply_commit(tx)
            return _deep(tx.tasks[task_id])

    def _stage_reassign_reclaim(self, tx: _Tx, task: Task, now: str) -> None:
        """Stage the claimed→pending composite for a reassign on a claimed task.

        Reclaim first, then reassign. Order in the event log mirrors
        the causal order; both land in the same ``read_range`` slice
        and no intermediate state is observable. Per ``reclaim``: an
        execution task whose claimant's in-flight variant is still
        ``starting`` transitions that variant to ``error`` atomically
        (``05-event-protocol.md`` §2.2).
        """
        tx.events.append(
            self._event(
                "task.reclaimed",
                {"task_id": task.task_id, "cause": "operator"},
            )
        )
        if task.kind == "execution":
            variant = self._find_starting_variant_for_implement_task(task)
            if variant is not None:
                tx.variants[variant.variant_id] = _validated_update(
                    variant, status="error", completed_at=now
                )
                tx.events.append(
                    self._event(
                        "variant.errored",
                        {"variant_id": variant.variant_id},
                    )
                )
        # A submission MUST NOT survive the reclaim half (a claimed task
        # can't yet have a recorded submission, but be defensive).
        if self._get_submission(task.task_id) is not None:
            tx.task_deletes_submission.add(task.task_id)

    @staticmethod
    def _reassign_event_payload(
        task_id: str,
        new_target: TaskTarget | None,
        reason: str,
        reassigned_by: str,
    ) -> dict[str, Any]:
        """Build the ``task.reassigned`` event payload.

        Re-dumps ``new_target`` through the model so the event payload's
        shape is identical to the wire schema (``05-event-protocol.md``
        §3.1).
        """
        target_dump: dict[str, Any] | None = None
        if new_target is not None:
            target_dump = TaskTarget.model_validate(
                new_target.model_dump(mode="json", exclude_none=True)
            ).model_dump(mode="json", exclude_none=True)
        return {
            "task_id": task_id,
            "new_target": target_dump,
            "reason": reason,
            "reassigned_by": reassigned_by,
        }

    @staticmethod
    def _targets_equal(
        a: TaskTarget | None, b: TaskTarget | None
    ) -> bool:
        if a is None and b is None:
            return True
        if a is None or b is None:
            return False
        return a.kind == b.kind and a.id == b.id

    def _accept_ideation(self, task: Task, submission: Submission) -> None:
        assert isinstance(submission, IdeaSubmission)
        reason = self._validate_ideation_acceptance(submission)
        if reason is not None:
            raise IllegalTransition(
                f"cannot accept ideation task {task.task_id!r}: {reason}"
            )
        now = self._ts()
        tx = _Tx()
        tx.tasks[task.task_id] = _validated_update(
            task, state="completed", claim=None, updated_at=now
        )
        tx.events.append(self._event("task.completed", {"task_id": task.task_id}))
        self._apply_commit(tx)

    def _reject_ideation(self, task: Task, reason: FailReason) -> None:
        now = self._ts()
        tx = _Tx()
        tx.tasks[task.task_id] = _validated_update(
            task, state="failed", claim=None, updated_at=now
        )
        tx.events.append(
            self._event("task.failed", {"task_id": task.task_id, "reason": reason})
        )
        self._apply_commit(tx)

    def _accept_execution(self, task: Task, submission: Submission) -> None:
        assert isinstance(task, ExecutionTask)
        assert isinstance(submission, VariantSubmission)
        reason = self._validate_execution_acceptance(task, submission)
        if reason is not None:
            raise IllegalTransition(
                f"cannot accept execute {task.task_id!r}: {reason}"
            )
        assert submission.commit_sha is not None
        idea = self._require_idea(task.payload.idea_id)
        variant = self._require_variant(submission.variant_id)

        now = self._ts()
        tx = _Tx()
        tx.tasks[task.task_id] = _validated_update(
            task, state="completed", claim=None, updated_at=now
        )
        tx.ideas[task.payload.idea_id] = _validated_update(idea, state="completed")
        # 12a-1: write executed_by atomically with the variant's
        # status transition out of "starting" per data-model §9.
        # task.submitted_by was set on the submit transition (§4.1)
        # and is the canonical claimant identity for attribution.
        executor_worker_id = task.submitted_by
        variant_updates: dict[str, Any] = {
            "commit_sha": submission.commit_sha,
            "executed_by": executor_worker_id,
        }
        if submission.artifacts_uri is not None:
            # 03-roles §3.4: orchestrator writes the executor's
            # submission.artifacts_uri to variant.executor_artifacts_uri
            # (disjoint from the evaluator-written variant.artifacts_uri
            # set in §4.4).
            variant_updates["executor_artifacts_uri"] = submission.artifacts_uri
        tx.variants[variant.variant_id] = _validated_update(variant, **variant_updates)
        tx.events.append(self._event("task.completed", {"task_id": task.task_id}))
        tx.events.append(
            self._event(
                "idea.completed",
                {"idea_id": task.payload.idea_id, "task_id": task.task_id},
            )
        )
        self._apply_commit(tx)

    def _reject_execution(
        self,
        task: Task,
        submission: Submission,
        reason: FailReason,
    ) -> None:
        assert isinstance(task, ExecutionTask)
        idea = self._require_idea(task.payload.idea_id)
        variant_for_error: Variant | None = None
        if isinstance(submission, VariantSubmission):
            # Only touch the variant if it was created under this very
            # task's idea — submission.variant_id is caller-supplied
            # and could reference an unrelated variant.
            variant = self._get_variant(submission.variant_id)
            if (
                variant is not None
                and variant.status == "starting"
                and variant.idea_id == task.payload.idea_id
            ):
                variant_for_error = variant

        now = self._ts()
        tx = _Tx()
        tx.tasks[task.task_id] = _validated_update(
            task, state="failed", claim=None, updated_at=now
        )
        tx.ideas[task.payload.idea_id] = _validated_update(idea, state="completed")
        tx.events.append(
            self._event("task.failed", {"task_id": task.task_id, "reason": reason})
        )
        tx.events.append(
            self._event(
                "idea.completed",
                {"idea_id": task.payload.idea_id, "task_id": task.task_id},
            )
        )
        if variant_for_error is not None:
            # §9: executed_by records the worker whose execution-task
            # submission produced the variant — and is preserved on
            # the error path too. The accept and reject branches both
            # stamp it from task.submitted_by (the canonical claimant
            # identity set at §4.1 submit time).
            error_updates: dict[str, Any] = {
                "status": "error",
                "completed_at": now,
                "executed_by": task.submitted_by,
            }
            # 03-roles §3.4: executor_artifacts_uri is written when
            # status ∈ {"success", "error"} (only "evaluation_error"
            # discards artifacts; executor never emits that status, but
            # the validation-error reject path could surface a malformed
            # payload — same as the evaluator's reject path, drop the
            # artifacts_uri when the payload itself was rejected).
            if (
                reason != "validation_error"
                and isinstance(submission, VariantSubmission)
                and submission.artifacts_uri is not None
            ):
                error_updates["executor_artifacts_uri"] = submission.artifacts_uri
            tx.variants[variant_for_error.variant_id] = _validated_update(
                variant_for_error, **error_updates
            )
            tx.events.append(
                self._event("variant.errored", {"variant_id": variant_for_error.variant_id})
            )
        self._apply_commit(tx)

    def _accept_evaluation(self, task: Task, submission: Submission) -> None:
        assert isinstance(task, EvaluationTask)
        assert isinstance(submission, EvaluationSubmission)
        reason = self._validate_evaluate_acceptance(task, submission)
        if reason is not None:
            raise IllegalTransition(
                f"cannot accept evaluate {task.task_id!r}: {reason}"
            )
        variant = self._require_variant(submission.variant_id)

        now = self._ts()
        tx = _Tx()
        tx.tasks[task.task_id] = _validated_update(
            task, state="completed", claim=None, updated_at=now
        )
        # 12a-1: write evaluated_by atomically with the variant's
        # status transition to "success" per data-model §9.
        # task.submitted_by was set on the submit transition (§4.1)
        # and is the canonical claimant identity for attribution.
        evaluator_worker_id = task.submitted_by
        tx.variants[variant.variant_id] = _validated_update(
            variant,
            status="success",
            evaluation=dict(submission.evaluation) if submission.evaluation else None,
            artifacts_uri=submission.artifacts_uri,
            completed_at=now,
            evaluated_by=evaluator_worker_id,
        )
        tx.events.append(self._event("task.completed", {"task_id": task.task_id}))
        tx.events.append(
            self._event(
                "variant.succeeded",
                {"variant_id": variant.variant_id, "commit_sha": variant.commit_sha},
            )
        )
        self._apply_commit(tx)

    def _reject_evaluate(
        self,
        task: Task,
        submission: Submission,
        reason: FailReason,
    ) -> None:
        assert isinstance(task, EvaluationTask)
        assert isinstance(submission, EvaluationSubmission)
        # submission.variant_id is already bound to task.payload.variant_id
        # by _validate_submission_ref_binding at submit time.
        variant = self._require_variant(task.payload.variant_id)

        now = self._ts()
        tx = _Tx()
        tx.tasks[task.task_id] = _validated_update(
            task, state="failed", claim=None, updated_at=now
        )
        tx.events.append(
            self._event("task.failed", {"task_id": task.task_id, "reason": reason})
        )

        # Variant-side effect depends on the submission status, not on
        # the orchestrator's reason (03-roles.md §4.4):
        #   • evaluation_error — variant stays in starting; evaluator-side
        #     failure does not condemn the variant.
        #   • success — reject can only happen via validation_error
        #     (malformed success). Variant stays in starting: the
        #     evaluator has not produced a verdict either way.
        #   • error — variant MUST transition to error, because the
        #     worker declared variant failure. If the payload is
        #     malformed (reason=validation_error), drop the invalid
        #     metrics/artifacts_uri but still record variant.errored.
        if submission.status == "error":
            if variant.status != "starting":
                raise IllegalTransition(
                    f"cannot error variant {variant.variant_id!r} from status {variant.status!r}"
                )
            # §9: evaluated_by records the evaluator whose submission
            # produced the result — and is preserved on the error
            # path too. The accept and reject branches both stamp it
            # from task.submitted_by (the canonical claimant identity
            # set at §4.1 submit time).
            update_kwargs: dict[str, Any] = {
                "status": "error",
                "completed_at": now,
                "evaluated_by": task.submitted_by,
            }
            if reason != "validation_error":
                if submission.evaluation is not None:
                    self._validate_evaluation(submission.evaluation)
                    update_kwargs["evaluation"] = dict(submission.evaluation)
                if submission.artifacts_uri is not None:
                    update_kwargs["artifacts_uri"] = submission.artifacts_uri
            tx.variants[variant.variant_id] = _validated_update(variant, **update_kwargs)
            tx.events.append(self._event("variant.errored", {"variant_id": variant.variant_id}))
        # status in {"evaluation_error", "success"} — no variant-side writes.

        self._apply_commit(tx)

    def _validate_non_no_op_variant(
        self, task: Task, submission: Submission
    ) -> None:
        """Reject an execution-task success submission whose tree matches every parent.

        ``spec/v0/03-roles.md`` §3.3 non-no-op invariant + §3.4
        rejection rule. The rule fires only on ``execution`` tasks with
        ``status == "success"`` and a non-empty ``parent_commits``.

        Enforcement runs two layers:

        1. **SHA-equality fast path** (always on, no git dependency).
           If ``commit_sha`` is byte-equal to the only parent SHA, the
           submission is rejected. (A commit's tree-of-self is itself's
           tree, so SHA equality is unconditionally a no-op when there
           is exactly one parent.) For multi-parent ideas, byte-equality
           with *every* parent is a stronger condition than the
           tree-identity rule but is the only sound SHA-only check
           (an empty merge can have a SHA distinct from any parent).
        2. **Tree-identity check** (when a ``tree_resolver`` is wired).
           If the resolver resolves every parent SHA AND the submission
           SHA to a non-``None`` tree, and every parent's tree equals
           the submission's tree, reject. A resolver that returns
           ``None`` for any SHA leaves the deeper check disabled for
           this submission (e.g. fixture SHAs absent from a real repo);
           the SHA-equality fast path still applies.
        """
        check = _no_op_check_inputs(task, submission, self._get_idea)
        if check is None:
            return
        sha, parents = check

        if _all_parents_equal_sha(sha, parents):
            raise NoOpVariant(_sha_equality_message(task.task_id, sha))

        resolver = self._tree_resolver
        if resolver is None:
            return
        trees = _resolve_trees(resolver, sha, parents)
        if trees is None:
            return
        sub_tree, parent_trees = trees
        if all(t == sub_tree for t in parent_trees):
            raise NoOpVariant(
                _tree_identity_message(task.task_id, sha, sub_tree)
            )

    def _validate_submission_ref_binding(
        self, task: Task, submission: Submission
    ) -> None:
        """Reject a submission whose referenced IDs don't match the task.

        Per ``04-task-protocol.md`` §4.1, the submission's result
        payload is scoped to the task it is being submitted against.
        An ideation-task submission's idea_ids must reference existing
        ideas (03-roles §2.4); an execution-task submission's
        variant_id must refer to a variant under the task's idea
        (03-roles §3.4); an evaluation-task submission's variant_id must
        equal the task payload's variant_id (03-roles §4.4).
        """
        if isinstance(submission, IdeaSubmission):
            for pid in submission.idea_ids:
                idea = self._get_idea(pid)
                if idea is None:
                    raise IllegalTransition(
                        f"ideation-task submission references unknown idea {pid!r}"
                    )
                if idea.state == "drafting":
                    raise IllegalTransition(
                        f"ideation-task submission references drafting idea {pid!r}; "
                        "ideator MUST NOT submit while ideas are in drafting "
                        "(03-roles.md §2.4)"
                    )
        elif isinstance(submission, VariantSubmission):
            assert isinstance(task, ExecutionTask)
            variant = self._get_variant(submission.variant_id)
            if variant is None:
                raise IllegalTransition(
                    f"execution-task submission references unknown variant "
                    f"{submission.variant_id!r}"
                )
            if variant.idea_id != task.payload.idea_id:
                raise IllegalTransition(
                    f"execution-task submission variant_id={submission.variant_id!r} "
                    f"belongs to idea {variant.idea_id!r}, not the "
                    f"task's idea {task.payload.idea_id!r}"
                )
        elif isinstance(submission, EvaluationSubmission):
            assert isinstance(task, EvaluationTask)
            if submission.variant_id != task.payload.variant_id:
                raise IllegalTransition(
                    f"evaluation-task submission variant_id={submission.variant_id!r} "
                    f"does not match task's variant {task.payload.variant_id!r}"
                )

    def _validate_ideation_acceptance(self, submission: IdeaSubmission) -> str | None:
        for pid in submission.idea_ids:
            idea = self._get_idea(pid)
            if idea is None:
                return f"idea {pid!r} no longer exists"
            if idea.state == "drafting":
                return f"idea {pid!r} is still in drafting at accept time"
        return None

    def _validate_execution_acceptance(
        self, task: ExecutionTask, submission: VariantSubmission
    ) -> str | None:
        if submission.commit_sha is None:
            return "success submission requires commit_sha (03-roles.md §3.4)"
        variant = self._get_variant(submission.variant_id)
        if variant is None:
            return f"referenced variant {submission.variant_id!r} does not exist"
        if variant.idea_id != task.payload.idea_id:
            return (
                f"referenced variant {submission.variant_id!r} belongs to a "
                "different idea"
            )
        if variant.status != "starting":
            return (
                f"referenced variant {submission.variant_id!r} is "
                f"{variant.status!r}, not 'starting'"
            )
        # 03-roles.md §3.3 enforcement runs at submit time only (see
        # `_validate_non_no_op_variant` call from `submit`). An
        # accept-time recheck would re-evaluate against a potentially-
        # shifted local clone view (e.g., a SHA that wasn't resolvable
        # at submit but is now); if it raises after the executor has
        # already published `work/*` refs upstream, those refs leak —
        # the recheck path doesn't have a clean-up channel. The
        # canonical enforcement is the executor's pre-submit
        # `_is_no_op_variant` check against its own controlled clone;
        # the server's submit-time SHA-equality fast path catches the
        # literal case for any wire-side caller. spec/v0/03-roles.md
        # §3.4 explicitly permits IUT-distributed enforcement.
        # Dry-run the variant write so an invalid commit_sha or
        # executor_artifacts_uri surfaces as validation_error instead
        # of crashing accept.
        dry_run_kwargs: dict[str, Any] = {"commit_sha": submission.commit_sha}
        if submission.artifacts_uri is not None:
            dry_run_kwargs["executor_artifacts_uri"] = submission.artifacts_uri
        try:
            _validated_update(variant, **dry_run_kwargs)
        except ValidationError as exc:
            err = exc.errors()[0]
            field = ".".join(str(p) for p in err.get("loc", ())) or "field"
            return f"invalid variant update: {field}: {err['msg']}"
        return None

    def _validate_evaluate_acceptance(
        self, task: EvaluationTask, submission: EvaluationSubmission
    ) -> str | None:
        if submission.variant_id != task.payload.variant_id:
            return "submission variant_id does not match task's variant_id"
        variant = self._get_variant(submission.variant_id)
        if variant is None:
            return f"variant {submission.variant_id!r} does not exist"
        if variant.status != "starting":
            return f"variant {submission.variant_id!r} is {variant.status!r}, not 'starting'"
        if variant.commit_sha is None:
            return f"variant {submission.variant_id!r} has no commit_sha"
        if submission.evaluation is None:
            return "success submission requires evaluation (03-roles.md §4.4)"
        try:
            self._validate_evaluation(submission.evaluation)
        except InvalidPrecondition as exc:
            return str(exc)
        # Dry-run the variant write so an invalid artifacts_uri (or
        # any other field) surfaces as validation_error instead of
        # crashing accept.
        try:
            _validated_update(
                variant,
                status="success",
                evaluation=dict(submission.evaluation),
                artifacts_uri=submission.artifacts_uri,
                completed_at=self._ts(),
            )
        except ValidationError as exc:
            err = exc.errors()[0]
            field = ".".join(str(p) for p in err.get("loc", ())) or "field"
            return f"invalid variant update: {field}: {err['msg']}"
        return None

    def _validate_evaluate_error(
        self, task: EvaluationTask, submission: EvaluationSubmission
    ) -> str | None:
        """Validate fields that a `status=error` evaluation-task submission would write."""
        if submission.variant_id != task.payload.variant_id:
            return "submission variant_id does not match task's variant_id"
        variant = self._get_variant(submission.variant_id)
        if variant is None:
            return f"variant {submission.variant_id!r} does not exist"
        if submission.evaluation is not None:
            try:
                self._validate_evaluation(submission.evaluation)
            except InvalidPrecondition as exc:
                return str(exc)
        try:
            _validated_update(
                variant,
                status="error",
                evaluation=dict(submission.evaluation) if submission.evaluation else None,
                artifacts_uri=submission.artifacts_uri,
                completed_at=self._ts(),
            )
        except ValidationError as exc:
            err = exc.errors()[0]
            field = ".".join(str(p) for p in err.get("loc", ())) or "field"
            return f"invalid variant update: {field}: {err['msg']}"
        return None

    def _require_submission_kind_matches(self, task: Task, submission: Submission) -> None:
        expected: type[Submission]
        if task.kind == "ideation":
            expected = IdeaSubmission
        elif task.kind == "execution":
            expected = VariantSubmission
        else:
            expected = EvaluationSubmission
        if not isinstance(submission, expected):
            raise IllegalTransition(
                f"task {task.task_id!r} (kind={task.kind!r}) requires "
                f"{expected.__name__}, got {type(submission).__name__}"
            )
