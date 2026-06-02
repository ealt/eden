"""Group-registry operations mixin (chapter 02 §7).

Part of the ``_StoreBase`` mixin family; see
[`.._base`](../_base.py) for ``_StoreCore`` and the composite.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from eden_contracts import Group, mint_opaque_id

from .._base import _StoreCore, _Tx
from ..errors import CycleDetected, NotFound
from ._helpers import _deep, _validated_update


class _GroupOpsMixin(_StoreCore):
    """Group registration, membership mutation, and transitive resolution."""

    def register_group(
        self,
        name: str | None = None,
        *,
        members: Iterable[str] | None = None,
        created_by: str | None = None,
        allow_reserved: bool = False,
    ) -> Group:
        """Register a new group, optionally with initial members (issue #128).

        Mints an opaque ``group_id`` (``grp_<ulid>``) and takes an
        optional operator-supplied display ``name``. Each entry in
        ``members`` MUST be a well-formed opaque member id
        (``wkr_*`` / ``grp_*``); a member that does not resolve to a
        registered worker / group is retained as a dangling reference
        (resolution treats it as membership=false per §7.1), exactly as
        before the rename. Cycles are detected at write time per
        [`spec/v0/02-data-model.md`](../../../../spec/v0/02-data-model.md)
        §7.3 and raise ``CycleDetected``.

        Raises ``InvalidName`` when ``name`` violates the display-name
        grammar (§1.7). Reserved group names (``admins`` /
        ``orchestrators``) raise ``ReservedIdentifier`` UNLESS
        ``allow_reserved=True`` — the privileged seed path
        (setup-experiment) passes ``allow_reserved=True`` to mint the
        auto-created ``admins`` / ``orchestrators`` groups whose name is
        the reserved literal. A later operator ``register_group`` with
        that name (default ``allow_reserved=False``) is rejected.
        """
        if name is not None:
            if allow_reserved:
                # Privileged seed path: skip the reserved-name guard but
                # still enforce the display-name grammar.
                from eden_contracts._common import _check_display_name

                from ..errors import InvalidName

                try:
                    _check_display_name(name)
                except ValueError as exc:
                    raise InvalidName(
                        f"group name {name!r} is not a well-formed "
                        f"display name: {exc}"
                    ) from exc
            else:
                self._validate_display_name(name, kind="group")
        group_id = mint_opaque_id("grp")
        # §7 "group is a recursively-resolved set": dedup the input
        # members in stable order so the durable store's
        # `(group_id, member_id)` uniqueness constraint never sees a
        # duplicate (chapter 02 §7 + R9-1). Preserves first-occurrence
        # order so the resolver's walk is deterministic.
        member_list: list[str] = []
        seen: set[str] = set()
        for member in members or ():
            if member in seen:
                continue
            seen.add(member)
            member_list.append(member)
        for member in member_list:
            self._validate_member_id(member)
        with self._atomic_operation():
            group_data: dict[str, Any] = {
                "group_id": group_id,
                "experiment_id": self._experiment_id,
                "members": member_list,
                "created_at": self._ts(),
            }
            if name is not None:
                group_data["name"] = name
            if created_by is not None:
                group_data["created_by"] = created_by
            group = Group.model_validate(group_data)
            self._require_no_cycle_after({group_id: group})
            tx = _Tx()
            tx.groups[group_id] = _deep(group)
            self._apply_commit(tx)
            return _deep(group)

    def add_to_group(self, group_id: str, member_id: str) -> Group:
        """Add ``member_id`` to ``group_id``. Idempotent on already-member."""
        self._validate_member_id(member_id)
        with self._atomic_operation():
            group = self._get_group(group_id)
            if group is None:
                raise NotFound(f"group {group_id!r}")
            if member_id in group.members:
                return _deep(group)
            new_members = [*group.members, member_id]
            updated = _validated_update(group, members=new_members)
            self._require_no_cycle_after({group_id: updated})
            tx = _Tx()
            tx.groups[group_id] = _deep(updated)
            self._apply_commit(tx)
            return _deep(updated)

    def remove_from_group(self, group_id: str, member_id: str) -> Group:
        """Remove ``member_id`` from ``group_id``. Idempotent on absent member."""
        with self._atomic_operation():
            group = self._get_group(group_id)
            if group is None:
                raise NotFound(f"group {group_id!r}")
            if member_id not in group.members:
                return _deep(group)
            new_members = [m for m in group.members if m != member_id]
            updated = _validated_update(group, members=new_members)
            tx = _Tx()
            tx.groups[group_id] = _deep(updated)
            self._apply_commit(tx)
            return _deep(updated)

    def delete_group(self, group_id: str) -> None:
        """Delete ``group_id``.

        Other groups that reference it as a member retain the dangling
        reference; resolution simply treats the missing id as ``False``
        per §7.1.
        """
        with self._atomic_operation():
            if self._get_group(group_id) is None:
                raise NotFound(f"group {group_id!r}")
            tx = _Tx()
            tx.group_deletes.add(group_id)
            self._apply_commit(tx)

    def read_group(self, group_id: str) -> Group:
        """Return the group, or raise ``NotFound``."""
        with self._atomic_operation():
            group = self._get_group(group_id)
            if group is None:
                raise NotFound(f"group {group_id!r}")
            return _deep(group)

    def list_groups(self, name: str | None = None) -> list[Group]:
        """Return groups (deep copies, sorted by ``group_id``).

        When ``name`` is supplied, returns only groups whose display
        ``name`` matches exactly (case-sensitive, against the persisted
        NFC form) — 0..N matches. ``name=None`` returns all groups
        (issue #128).
        """
        with self._atomic_operation():
            return [
                _deep(g)
                for g in self._iter_groups()
                if name is None or g.name == name
            ]

    def resolve_worker_in_group(self, worker_id: str, group_id: str) -> bool:
        """Return ``True`` iff ``worker_id`` is transitively in ``group_id``.

        Implements [`spec/v0/02-data-model.md`](../../../../spec/v0/02-data-model.md)
        §7.2: a worker is a member of a group if it appears directly in
        ``members``, or appears in any group that is itself a member
        (transitive closure). Cycles cannot exist (§7.3 forbids them at
        write time), so a topo-walk over the group DAG is safe.

        Per §7.1 "a reference to a non-existent worker / group
        resolves to membership=false", short-circuit when the
        candidate ``worker_id`` is not itself a registered worker:
        an unregistered name in some group's ``members`` does NOT
        make that name a member, even though the literal §7.2 first
        bullet would otherwise admit it. Dangling group references
        in ``members`` are likewise skipped (the walk just doesn't
        descend through them).
        """
        with self._atomic_operation():
            if self._get_worker(worker_id) is None:
                return False
            visited: set[str] = set()
            stack: list[str] = [group_id]
            while stack:
                current = stack.pop()
                if current in visited:
                    continue
                visited.add(current)
                group = self._get_group(current)
                if group is None:
                    # Dangling reference; treat as empty membership.
                    continue
                if worker_id in group.members:
                    return True
                for member in group.members:
                    if self._get_group(member) is not None and member not in visited:
                        stack.append(member)
            return False

    def _require_no_cycle_after(self, staged_groups: dict[str, Group]) -> None:
        """Raise ``CycleDetected`` if ``staged_groups`` would close a cycle.

        ``staged_groups`` is the post-mutation membership for any groups
        about to be written; persisted groups not in ``staged_groups``
        are read from the store. The DFS treats edges as
        group → group-member; a worker member is a leaf.
        """

        def members_of(gid: str) -> list[str]:
            if gid in staged_groups:
                return list(staged_groups[gid].members)
            persisted = self._get_group(gid)
            return list(persisted.members) if persisted is not None else []

        def dfs(
            node: str,
            visited: set[str],
            on_stack: set[str],
        ) -> bool:
            if node in on_stack:
                return True
            if node in visited:
                return False
            visited.add(node)
            on_stack.add(node)
            for member in members_of(node):
                # Only traverse member ids that name another GROUP,
                # not a worker. A worker member is a leaf in this
                # graph; no group-id edges leave it.
                is_group = (
                    member in staged_groups or self._get_group(member) is not None
                )
                if is_group and dfs(member, visited, on_stack):
                    return True
            on_stack.discard(node)
            return False

        # DFS from every staged group looking for a back-edge to itself
        # or to another node we're currently exploring (a cycle).
        for start in staged_groups:
            if dfs(start, set(), set()):
                raise CycleDetected(
                    f"group mutation on {start!r} would introduce a cycle"
                )
