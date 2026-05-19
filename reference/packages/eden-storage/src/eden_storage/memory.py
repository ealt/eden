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

from eden_contracts import (
    Event,
    Experiment,
    Group,
    Idea,
    ImportProvenance,
    Task,
    Variant,
    Worker,
)

from ._base import (
    _DEFAULT_DISPATCH_MODE,
    _DEFAULT_EXPERIMENT_STATE,
    _StoreBase,
    _Tx,
)
from .submissions import Submission


class InMemoryStore(_StoreBase):
    """In-memory backend. Dict-backed, protected by an ``RLock``.

    The constructor forwards to ``_StoreBase``; see there for the
    full argument list.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._tasks: dict[str, Task] = {}
        self._ideas: dict[str, Idea] = {}
        self._variants: dict[str, Variant] = {}
        self._submissions: dict[str, Submission] = {}
        self._events: list[Event] = []
        # Worker registry (12a-1 wave 2). The wire-visible Worker shape
        # lives in `_workers`; the credential hash lives in
        # `_worker_credentials` keyed by the same `worker_id` so reads
        # of `_workers` never leak the credential.
        self._workers: dict[str, Worker] = {}
        self._worker_credentials: dict[str, str] = {}
        self._groups: dict[str, Group] = {}
        # 12a-2 dispatch_mode. Initialized to the four-operational-key
        # all-`auto` plus `termination` `manual` default from
        # `02-data-model.md` §2.4; mutated atomically via
        # `update_dispatch_mode`.
        self._dispatch_mode: dict[str, str] = dict(_DEFAULT_DISPATCH_MODE)
        # 12a-3 experiment lifecycle state (`02-data-model.md` §2.5).
        # `_experiment_state` defaults to "running" at construction;
        # `_experiment_created_at` is captured from the store's clock
        # so termination policies that key off wall-time work without
        # extra plumbing.
        self._experiment_state: str = _DEFAULT_EXPERIMENT_STATE
        self._experiment_created_at: str = self._ts()
        # 12b: import-provenance per `02-data-model.md` §2.5. `None` on
        # natively-created experiments; populated by `import_checkpoint`.
        self._imported_from: ImportProvenance | None = None
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

    def _get_idea(self, idea_id: str) -> Idea | None:
        return self._ideas.get(idea_id)

    def _get_variant(self, variant_id: str) -> Variant | None:
        return self._variants.get(variant_id)

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

    def _iter_ideas(self, *, state: str | None = None) -> Iterable[Idea]:
        for idea in self._ideas.values():
            if state is None or idea.state == state:
                yield idea

    def _iter_variants(self, *, status: str | None = None) -> Iterable[Variant]:
        for variant in self._variants.values():
            if status is None or variant.status == status:
                yield variant

    def _iter_events(self) -> Iterable[Event]:
        return list(self._events)

    def _get_worker(self, worker_id: str) -> Worker | None:
        return self._workers.get(worker_id)

    def _get_worker_credential_hash(self, worker_id: str) -> str | None:
        return self._worker_credentials.get(worker_id)

    def _iter_workers(self) -> Iterable[Worker]:
        return [self._workers[k] for k in sorted(self._workers)]

    def _get_group(self, group_id: str) -> Group | None:
        return self._groups.get(group_id)

    def _iter_groups(self) -> Iterable[Group]:
        return [self._groups[k] for k in sorted(self._groups)]

    def _get_dispatch_mode(self) -> dict[str, str]:
        return dict(self._dispatch_mode)

    def _get_experiment(self) -> Experiment:
        return Experiment(
            experiment_id=self._experiment_id,
            state=self._experiment_state,  # type: ignore[arg-type]
            created_at=self._experiment_created_at,
            imported_from=self._imported_from,
        )

    def _apply_commit(self, tx: _Tx) -> None:
        for task_id, task in tx.tasks.items():
            self._tasks[task_id] = task
        for idea_id, idea in tx.ideas.items():
            self._ideas[idea_id] = idea
        for variant_id, variant in tx.variants.items():
            self._variants[variant_id] = variant
        for task_id, submission in tx.submissions.items():
            self._submissions[task_id] = submission
        for task_id in tx.task_deletes_submission:
            self._submissions.pop(task_id, None)
        for worker_id, worker in tx.workers.items():
            self._workers[worker_id] = worker
        for worker_id, credential_hash in tx.worker_credentials.items():
            self._worker_credentials[worker_id] = credential_hash
        for worker_id in tx.worker_deletes:
            self._workers.pop(worker_id, None)
            self._worker_credentials.pop(worker_id, None)
        for group_id, group in tx.groups.items():
            self._groups[group_id] = group
        for group_id in tx.group_deletes:
            self._groups.pop(group_id, None)
        if tx.dispatch_mode is not None:
            self._dispatch_mode = dict(tx.dispatch_mode)
        if tx.experiment_state is not None:
            self._experiment_state = tx.experiment_state
        if tx.imported_from_update is not None:
            (new_imported_from,) = tx.imported_from_update
            self._imported_from = new_imported_from
        self._events.extend(tx.events)
