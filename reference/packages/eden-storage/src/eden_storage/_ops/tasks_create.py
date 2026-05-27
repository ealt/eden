"""Task-creation operations mixin (chapters 04 §1, 05 §2.2).

Split out of the task mixin under the plan §3.7 fallback when the
combined ``_ops/tasks.py`` crossed the 800-SLOC file gate. Owns
``create_task`` + the typed ``create_*_task`` builders + the
per-kind ``_insert_*`` composite-commit bodies + the §6.4
at-most-one-live-task guards. Pairs with ``_TaskLifecycleOpsMixin``.
"""

from __future__ import annotations

from typing import Any

from eden_contracts import (
    EvaluationPayload,
    EvaluationTask,
    ExecutionPayload,
    ExecutionTask,
    IdeationPayload,
    IdeationTask,
    Task,
    TaskTarget,
)

from .._base import _LIVE_TASK_STATES, _StoreCore, _Tx
from ..errors import (
    AlreadyExists,
    InvalidPrecondition,
    NotFound,
)
from ._helpers import (
    _deep,
    _validated_update,
)


class _TaskCreateOpsMixin(_StoreCore):
    """Task creation + dispatch composite-commits + live-task guards."""

    def create_task(self, task: Task) -> Task:
        """Spec-literal ``create_task`` per chapter 8 §1.1.

        Inserts a fully-formed task object in ``state="pending"``.
        For ``execution`` tasks the composite-commit from
        ``05-event-protocol.md`` §2.2 also transitions the referenced
        idea from ``ready`` to ``dispatched``; for ``evaluation``
        tasks the referenced variant's ``starting``/``commit_sha``
        precondition is enforced.

        The typed helpers ``create_ideation_task`` / ``create_execution_task``
        / ``create_evaluation_task`` build the task payload for the
        caller; both paths converge on the same commit.
        """
        if task.state != "pending":
            raise InvalidPrecondition(
                f"new task must be in 'pending' state, not {task.state!r}"
            )
        if task.claim is not None:
            raise InvalidPrecondition(
                "new task must not carry a claim (08-storage.md §1.1)"
            )
        if task.kind == "ideation":
            assert isinstance(task, IdeationTask)
            return self._insert_ideation_task(task)
        if task.kind == "execution":
            assert isinstance(task, ExecutionTask)
            return self._insert_execution_task(task)
        assert isinstance(task, EvaluationTask)
        return self._insert_evaluation_task(task)

    def _insert_ideation_task(self, task: IdeationTask) -> IdeationTask:
        if task.payload.experiment_id != self._experiment_id:
            raise InvalidPrecondition(
                f"ideation task payload.experiment_id={task.payload.experiment_id!r} "
                f"does not match store experiment {self._experiment_id!r}"
            )
        with self._atomic_operation():
            self._require_running()
            self._require_no_task(task.task_id)
            tx = _Tx()
            tx.tasks[task.task_id] = _deep(task)
            tx.events.append(
                self._event("task.created", {"task_id": task.task_id, "kind": "ideation"})
            )
            self._apply_commit(tx)
            return _deep(task)

    def _insert_execution_task(self, task: ExecutionTask) -> ExecutionTask:
        with self._atomic_operation():
            self._require_running()
            self._require_no_task(task.task_id)
            idea_id = task.payload.idea_id
            idea = self._get_idea(idea_id)
            if idea is None:
                raise NotFound(f"idea {idea_id!r}")
            if idea.state != "ready":
                raise InvalidPrecondition(
                    f"idea {idea_id!r} must be 'ready' "
                    f"to dispatch, not {idea.state!r}"
                )
            self._require_no_live_execution_task_for_idea(idea_id)
            # 12a-3 intended_executor flow-through (`03-roles.md` §6.2
            # decision-type 2): when the caller does not supply an
            # explicit task.target on the create payload, the resulting
            # task inherits the idea's routing hint. An explicit
            # task.target wins over the idea's hint (admin override).
            insert_task = task
            if task.target is None and idea.intended_executor is not None:
                insert_task = _validated_update(
                    task, target=idea.intended_executor
                )
            tx = _Tx()
            tx.tasks[insert_task.task_id] = _deep(insert_task)
            tx.ideas[idea_id] = _validated_update(idea, state="dispatched")
            tx.events.append(
                self._event(
                    "task.created",
                    {"task_id": insert_task.task_id, "kind": "execution"},
                )
            )
            tx.events.append(
                self._event(
                    "idea.dispatched",
                    {"idea_id": idea_id, "task_id": insert_task.task_id},
                )
            )
            self._apply_commit(tx)
            return _deep(insert_task)

    def _insert_evaluation_task(self, task: EvaluationTask) -> EvaluationTask:
        with self._atomic_operation():
            self._require_running()
            self._require_no_task(task.task_id)
            variant_id = task.payload.variant_id
            variant = self._get_variant(variant_id)
            if variant is None:
                raise NotFound(f"variant {variant_id!r}")
            if variant.status != "starting":
                raise InvalidPrecondition(
                    f"variant {variant_id!r} must be 'starting' to dispatch eval, "
                    f"not {variant.status!r}"
                )
            if variant.commit_sha is None:
                raise InvalidPrecondition(
                    f"variant {variant_id!r} has no commit_sha; executor must set it first"
                )
            self._require_no_live_evaluation_task_for_variant(variant_id)
            # intended_evaluator flow-through (`03-roles.md` §6.2
            # decision-type 3): when the caller does not supply an
            # explicit task.target on the create payload, the resulting
            # task inherits the originating idea's intended_evaluator.
            insert_task = task
            if task.target is None:
                idea = self._get_idea(variant.idea_id)
                if idea is not None and idea.intended_evaluator is not None:
                    insert_task = _validated_update(
                        task, target=idea.intended_evaluator
                    )
            tx = _Tx()
            tx.tasks[insert_task.task_id] = _deep(insert_task)
            tx.events.append(
                self._event("task.created", {"task_id": insert_task.task_id, "kind": "evaluation"})
            )
            self._apply_commit(tx)
            return _deep(insert_task)

    def create_ideation_task(self, task_id: str) -> IdeationTask:
        """Create an ``ideation`` task. Emits ``task.created`` atomically."""
        with self._atomic_operation():
            self._require_running()
            self._require_no_task(task_id)
            now = self._ts()
            task = IdeationTask(
                task_id=task_id,
                kind="ideation",
                state="pending",
                payload=IdeationPayload(experiment_id=self._experiment_id),
                created_at=now,
                updated_at=now,
            )
            tx = _Tx()
            tx.tasks[task_id] = task
            tx.events.append(self._event("task.created", {"task_id": task_id, "kind": "ideation"}))
            self._apply_commit(tx)
            return _deep(task)

    def create_execution_task(
        self,
        task_id: str,
        idea_id: str,
        *,
        target: TaskTarget | None = None,
    ) -> ExecutionTask:
        """Create an ``execution`` task; composite-commits ``idea.dispatched``.

        Per ``05-event-protocol.md`` §2.2: creating the execution task
        and transitioning the referenced idea from ``ready`` to
        ``dispatched`` land in one atomic commit.

        Per ``03-roles.md`` §6.4 the Store enforces at-most-one live
        execution task per idea; a duplicate concurrent attempt
        observes the first commit and raises ``AlreadyExists`` here
        (in practice the idea-state precondition catches it first,
        since the first create transitions the idea to ``dispatched``,
        but the explicit live-task check makes the §6.4 invariant
        unambiguous in code).

        12a-3: when ``target`` is supplied, the resulting task uses it
        verbatim (admin override path per ``03-roles.md`` §6.5). When
        ``target`` is ``None``, the task inherits
        ``idea.intended_executor`` (the auto-orchestrator path per
        ``03-roles.md`` §6.2 decision-type 2). When both are unset
        the task is open to any registered executor-class worker.
        """
        with self._atomic_operation():
            self._require_running()
            self._require_no_task(task_id)
            idea = self._get_idea(idea_id)
            if idea is None:
                raise NotFound(f"idea {idea_id!r}")
            if idea.state != "ready":
                raise InvalidPrecondition(
                    f"idea {idea_id!r} must be 'ready' "
                    f"to dispatch, not {idea.state!r}"
                )
            self._require_no_live_execution_task_for_idea(idea_id)
            now = self._ts()
            # Explicit caller-supplied target wins; otherwise inherit
            # `idea.intended_executor` per the 12a-3 §6.2 decision-type 2
            # flow-through rule. The `NotNone` validator on
            # `_TaskBase.target` rejects explicit None — keep the kwarg
            # absent when no target applies.
            effective_target = (
                target if target is not None else idea.intended_executor
            )
            task_kwargs: dict[str, Any] = {
                "task_id": task_id,
                "kind": "execution",
                "state": "pending",
                "payload": ExecutionPayload(idea_id=idea_id),
                "created_at": now,
                "updated_at": now,
            }
            if effective_target is not None:
                task_kwargs["target"] = effective_target
            task = ExecutionTask(**task_kwargs)
            tx = _Tx()
            tx.tasks[task_id] = task
            tx.ideas[idea_id] = _validated_update(idea, state="dispatched")
            tx.events.append(
                self._event("task.created", {"task_id": task_id, "kind": "execution"})
            )
            tx.events.append(
                self._event(
                    "idea.dispatched",
                    {"idea_id": idea_id, "task_id": task_id},
                )
            )
            self._apply_commit(tx)
            return _deep(task)

    def create_evaluation_task(
        self,
        task_id: str,
        variant_id: str,
        *,
        target: TaskTarget | None = None,
    ) -> EvaluationTask:
        """Create an ``evaluation`` task against a starting variant with ``commit_sha``.

        Per ``03-roles.md`` §6.4 the Store enforces at-most-one live
        evaluation task per starting variant; a duplicate concurrent
        attempt raises ``AlreadyExists``. A previously-failed
        evaluation task for the same variant does NOT block a new one
        (terminal states are not live).

        When ``target`` is supplied, the resulting task uses it verbatim
        (admin override path). When ``target`` is ``None``, the task
        inherits the originating idea's ``intended_evaluator`` (the
        auto-orchestrator path per ``03-roles.md`` §6.2 decision-type 3).
        """
        with self._atomic_operation():
            self._require_running()
            self._require_no_task(task_id)
            variant = self._get_variant(variant_id)
            if variant is None:
                raise NotFound(f"variant {variant_id!r}")
            if variant.status != "starting":
                raise InvalidPrecondition(
                    f"variant {variant_id!r} must be 'starting' to dispatch eval, "
                    f"not {variant.status!r}"
                )
            if variant.commit_sha is None:
                raise InvalidPrecondition(
                    f"variant {variant_id!r} has no commit_sha; executor must set it first"
                )
            self._require_no_live_evaluation_task_for_variant(variant_id)
            now = self._ts()
            # Explicit caller-supplied target wins; otherwise inherit
            # `idea.intended_evaluator` per the §6.2 decision-type 3
            # flow-through rule.
            effective_target: TaskTarget | None = target
            if effective_target is None:
                idea = self._get_idea(variant.idea_id)
                if idea is not None:
                    effective_target = idea.intended_evaluator
            task_kwargs: dict[str, Any] = {
                "task_id": task_id,
                "kind": "evaluation",
                "state": "pending",
                "payload": EvaluationPayload(variant_id=variant_id),
                "created_at": now,
                "updated_at": now,
            }
            if effective_target is not None:
                task_kwargs["target"] = effective_target
            task = EvaluationTask(**task_kwargs)
            tx = _Tx()
            tx.tasks[task_id] = task
            tx.events.append(
                self._event("task.created", {"task_id": task_id, "kind": "evaluation"})
            )
            self._apply_commit(tx)
            return _deep(task)

    def _require_no_live_execution_task_for_idea(self, idea_id: str) -> None:
        """Raise ``AlreadyExists`` if a live execution task already targets ``idea_id``.

        ``03-roles.md`` §6.4: the Store enforces at-most-one live
        execution task per idea so concurrent orchestrator invocations
        collapse to a single dispatch. "Live" ≡ state in
        ``_LIVE_TASK_STATES``; terminal tasks do not block.
        """
        for existing in self._iter_tasks(kind="execution"):
            if existing.state in _LIVE_TASK_STATES and (
                isinstance(existing, ExecutionTask)
                and existing.payload.idea_id == idea_id
            ):
                raise AlreadyExists(
                    f"a live execution task already exists for idea {idea_id!r} "
                    f"(task_id={existing.task_id!r}, state={existing.state!r})"
                )

    def _require_no_live_evaluation_task_for_variant(self, variant_id: str) -> None:
        """Raise ``AlreadyExists`` if a live evaluation task already targets ``variant_id``.

        ``03-roles.md`` §6.4: at-most-one live evaluation task per
        starting variant. A previously-failed (terminal) evaluation
        task does NOT block a retry.
        """
        for existing in self._iter_tasks(kind="evaluation"):
            if existing.state in _LIVE_TASK_STATES and (
                isinstance(existing, EvaluationTask)
                and existing.payload.variant_id == variant_id
            ):
                raise AlreadyExists(
                    f"a live evaluation task already exists for variant "
                    f"{variant_id!r} (task_id={existing.task_id!r}, "
                    f"state={existing.state!r})"
                )
