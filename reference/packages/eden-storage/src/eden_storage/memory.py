"""In-memory implementation of the ``Store`` Protocol.

Single-process, non-durable. Reads and writes are protected by an
``RLock`` so that concurrent callers in the same process see each
operation atomically. The shared transition logic lives in
[`_base.py`](_base.py); this module only supplies the backend
primitives.

This backend is the simplest thing that satisfies the transactional
invariant (``spec/v0/05-event-protocol.md`` §2) — a write and its
event both land in in-memory dicts together inside
``_apply_commit``. It does **not** satisfy chapter 8 §3 (durability):
process crash means data loss. Use ``SqliteStore`` when durability
matters.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from threading import RLock

from eden_contracts import Event, Proposal, Task, Trial

from ._base import _StoreBase, _Tx
from .submissions import Submission


class InMemoryStore(_StoreBase):
    """In-memory backend. Dict-backed, protected by an ``RLock``.

    The constructor forwards to ``_StoreBase``; see there for the
    full argument list.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._tasks: dict[str, Task] = {}
        self._proposals: dict[str, Proposal] = {}
        self._trials: dict[str, Trial] = {}
        self._submissions: dict[str, Submission] = {}
        self._events: list[Event] = []
        self._lock = RLock()

    # ------------------------------------------------------------------
    # Backend primitives
    # ------------------------------------------------------------------

    @contextmanager
    def _atomic_operation(self) -> Iterator[None]:
        with self._lock:
            yield

    def _get_task(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def _get_proposal(self, proposal_id: str) -> Proposal | None:
        return self._proposals.get(proposal_id)

    def _get_trial(self, trial_id: str) -> Trial | None:
        return self._trials.get(trial_id)

    def _get_submission(self, task_id: str) -> Submission | None:
        return self._submissions.get(task_id)

    def _iter_tasks(
        self, *, kind: str | None = None, state: str | None = None
    ) -> Iterable[Task]:
        for task in self._tasks.values():
            if kind is not None and task.kind != kind:
                continue
            if state is not None and task.state != state:
                continue
            yield task

    def _iter_proposals(self, *, state: str | None = None) -> Iterable[Proposal]:
        for proposal in self._proposals.values():
            if state is None or proposal.state == state:
                yield proposal

    def _iter_trials(self, *, status: str | None = None) -> Iterable[Trial]:
        for trial in self._trials.values():
            if status is None or trial.status == status:
                yield trial

    def _iter_events(self) -> Iterable[Event]:
        return list(self._events)

    def _apply_commit(self, tx: _Tx) -> None:
        for task_id, task in tx.tasks.items():
            self._tasks[task_id] = task
        for proposal_id, proposal in tx.proposals.items():
            self._proposals[proposal_id] = proposal
        for trial_id, trial in tx.trials.items():
            self._trials[trial_id] = trial
        for task_id, submission in tx.submissions.items():
            self._submissions[task_id] = submission
        for task_id in tx.task_deletes_submission:
            self._submissions.pop(task_id, None)
        self._events.extend(tx.events)
