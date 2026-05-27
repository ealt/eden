"""Event-log read operations mixin (chapter 08 §2)."""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from eden_contracts import Event

from .._base import _StoreCore
from ._helpers import _deep


class _EventOpsMixin(_StoreCore):
    """Ordered event-log reads: events / replay / read_range."""

    def events(self) -> list[Event]:
        """Return an ordered snapshot of the full event log.

        Every returned event is a deep copy; mutation of the return
        value cannot rewrite log entries. Equivalent to ``replay()``;
        retained as the pre-Phase-6 convenience name.
        """
        return self.replay()

    def replay(self) -> list[Event]:
        """Return every event for this experiment in log order.

        Chapter 8 §2.1 / §4.4. Every returned event is a deep copy.
        """
        with self._atomic_operation():
            return [_deep(e) for e in self._iter_events()]

    def read_range(self, cursor: int | None = None) -> list[Event]:
        """Return events after ``cursor`` in log order (chapter 8 §2.1).

        ``cursor`` is the **cumulative** count of events the caller
        has already consumed — i.e. the total number of events the
        caller has observed, not the size of its last chunk. A
        caller polling in a loop advances ``cursor`` by the length
        of each returned chunk; passing the size of the last chunk
        alone would skip everything the caller has already read past
        that point.

        The reference backends' log order is total (chapter 8 §2.2),
        so indexing by cumulative count is stable. A ``None`` cursor
        (or ``0``) is equivalent to ``replay()``.
        """
        with self._atomic_operation():
            events = [_deep(e) for e in self._iter_events()]
        if cursor is None or cursor <= 0:
            return events
        return events[cursor:]


def iter_events_by_type(events: Iterable[Event], type_: str) -> Iterator[Event]:
    """Yield events whose ``type`` equals ``type_``. Convenience for tests."""
    for event in events:
        if event.type == type_:
            yield event
