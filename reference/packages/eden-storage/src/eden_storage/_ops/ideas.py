"""Idea-store operations mixin (chapter 02 §3, chapter 08 §1)."""

from __future__ import annotations

from eden_contracts import Idea

from .._base import _StoreCore, _Tx
from ..errors import AlreadyExists, IllegalTransition, InvalidPrecondition, NotFound
from ._helpers import _deep, _validated_update


class _IdeaOpsMixin(_StoreCore):
    """Idea creation, drafting→ready transition, and reads."""

    def read_idea(self, idea_id: str) -> Idea:
        """Return a snapshot of the current idea, or raise ``NotFound``."""
        with self._atomic_operation():
            idea = self._get_idea(idea_id)
            if idea is None:
                raise NotFound(f"idea {idea_id!r}")
            return _deep(idea)

    def list_ideas(self, *, state: str | None = None) -> list[Idea]:
        """Return snapshots of ideas matching an optional ``state`` filter."""
        with self._atomic_operation():
            return [_deep(p) for p in self._iter_ideas(state=state)]

    def create_idea(self, idea: Idea) -> None:
        """Persist a new idea in ``drafting``. Emits ``idea.drafted``."""
        with self._atomic_operation():
            if self._get_idea(idea.idea_id) is not None:
                raise AlreadyExists(f"idea {idea.idea_id!r}")
            if idea.experiment_id != self._experiment_id:
                raise InvalidPrecondition(
                    f"idea experiment_id {idea.experiment_id!r} "
                    f"does not match store experiment {self._experiment_id!r}"
                )
            if idea.state != "drafting":
                raise InvalidPrecondition(
                    f"new idea must start in 'drafting', not {idea.state!r}"
                )
            tx = _Tx()
            tx.ideas[idea.idea_id] = _deep(idea)
            tx.events.append(
                self._event("idea.drafted", {"idea_id": idea.idea_id})
            )
            self._apply_commit(tx)

    def mark_idea_ready(self, idea_id: str) -> None:
        """Transition an idea ``drafting → ready``. Emits ``idea.ready``."""
        with self._atomic_operation():
            idea = self._require_idea(idea_id)
            if idea.state != "drafting":
                raise IllegalTransition(
                    f"cannot mark idea ready from state {idea.state!r}"
                )
            tx = _Tx()
            tx.ideas[idea_id] = _validated_update(idea, state="ready")
            tx.events.append(self._event("idea.ready", {"idea_id": idea_id}))
            self._apply_commit(tx)
