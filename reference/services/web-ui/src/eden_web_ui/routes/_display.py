"""Display-convention helpers for the identity rename (#128).

Per the rename contract §8 / glossary §8 / plan §5.6:

- An entity (worker / group / experiment) with a display ``name`` renders
  as ``<name> (<id>)``; with no name it renders as the bare opaque id.
- Log lines / structured events use the opaque id ONLY (never routed
  through these helpers).
- Attribution fields (``created_by`` / ``submitted_by`` / ``executed_by``
  / ``evaluated_by`` / ``reassigned_by`` …) carry an opaque ``wkr_*`` id;
  the UI resolves it to ``<name> (<id>)`` when a name is known, falling
  back to the bare id otherwise.

The ``admin`` literal (the deployment-admin bearer principal) has no
``Worker`` row and therefore no name; it resolves to itself.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def name_id(name: str | None, opaque_id: str) -> str:
    """Render ``<name> (<id>)`` when ``name`` is set, else the bare id."""
    if name:
        return f"{name} ({opaque_id})"
    return opaque_id


def worker_name_map(workers: Iterable[Any]) -> dict[str, str]:
    """Build an id→name map over workers that carry a non-empty ``name``.

    Workers registered without a name are omitted (callers fall back to
    the bare id via :func:`resolve_attribution`).
    """
    out: dict[str, str] = {}
    for w in workers:
        wid = getattr(w, "worker_id", None)
        wname = getattr(w, "name", None)
        if wid is not None and wname:
            out[wid] = wname
    return out


def best_effort_worker_names(store: Any) -> dict[str, str]:
    """Build the attribution id → name map for ``store``, degrading to ``{}``.

    A transport blip on ``list_workers`` yields an empty map (attribution
    falls back to bare ids) rather than failing the whole page render.
    """
    try:
        return worker_name_map(store.list_workers())
    except Exception:  # noqa: BLE001 — transport/store-domain; degrade to bare ids
        return {}


def resolve_attribution(
    worker_id: str | None, names: dict[str, str]
) -> str | None:
    """Resolve an attribution id to ``<name> (<id>)`` (fall back to bare id).

    ``None`` in → ``None`` out (the field was absent). An id with no
    known name (unregistered, or the ``admin`` principal) renders as the
    bare id.
    """
    if worker_id is None:
        return None
    return name_id(names.get(worker_id), worker_id)


def sort_by_name_then_id(
    entities: Iterable[Any],
    *,
    id_attr: str = "worker_id",
) -> list[Any]:
    """Sort entities by ``name`` (case-folded) then opaque id.

    Named rows sort before nameless rows; within each band the sort is
    stable on the lower-cased name then the opaque id. Nameless rows sort
    among themselves by id. ``id_attr`` selects the id field
    (``worker_id`` / ``group_id``).
    """

    def key(e: Any) -> tuple[int, str, str]:
        opaque = getattr(e, id_attr)
        name = getattr(e, "name", None)
        if name:
            return (0, name.casefold(), opaque)
        return (1, "", opaque)

    return sorted(entities, key=key)
