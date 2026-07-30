"""Cost-ledger operations mixin — reference-only (issue #343).

See [`cost.py`](../cost.py) for why the ledger is a reference extension
rather than a spec field. Like an artifact-metadata row (and unlike every
protocol-owned write), a cost entry carries **no event**: it is not bound
to any task / idea / variant transition, so the
[`spec/v0/05-event-protocol.md`](../../../../../../spec/v0/05-event-protocol.md) §2
transactional invariant has nothing to pair it with. Cost is bookkeeping
*about* an attempt, not a state change *of* one — recording it MUST NOT
be able to fail a worker's submission path.
"""

from __future__ import annotations

from .._base import _StoreCore, _Tx
from ..cost import CostEntry
from ..errors import InvalidPrecondition
from ._helpers import _deep, _validated_update


class _CostOpsMixin(_StoreCore):
    """Cost-entry write + read (reference-only)."""

    def record_cost(self, entry: CostEntry) -> None:
        """Record one spend event; first-write-wins on ``entry.entry_id``.

        A repeat ``entry_id`` is a silent no-op so a host that re-records
        after a transport failure cannot double-count. ``recorded_at`` is
        stamped from the store's clock — a caller-supplied value is
        ignored, so entry timestamps are comparable across hosts with
        skewed clocks.

        Raises ``InvalidPrecondition`` when ``entry.experiment_id`` does
        not match the store's, mirroring ``create_variant``.
        """
        if entry.experiment_id != self._experiment_id:
            raise InvalidPrecondition(
                f"cost entry experiment_id {entry.experiment_id!r} does not "
                f"match store experiment {self._experiment_id!r}"
            )
        with self._atomic_operation():
            if self._get_cost_entry(entry.entry_id) is not None:
                return
            tx = _Tx()
            tx.cost_entries[entry.entry_id] = _validated_update(
                entry, recorded_at=self._ts()
            )
            self._apply_commit(tx)

    def list_cost_entries(
        self,
        *,
        role: str | None = None,
        variant_id: str | None = None,
    ) -> list[CostEntry]:
        """Return recorded entries with optional filters.

        Ordered by ``(recorded_at, entry_id)`` — the store-stamped
        timestamp, with the id as a deterministic tie-break for entries
        recorded inside the same clock tick. Every backend returns the
        same sequence for the same ledger contents.
        """
        with self._atomic_operation():
            return [
                _deep(entry)
                for entry in self._iter_cost_entries(
                    role=role, variant_id=variant_id
                )
            ]
