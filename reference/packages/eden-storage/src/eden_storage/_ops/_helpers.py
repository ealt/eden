"""Module-level, ``self``-free helpers shared by the store mixins.

These are pure functions (no ``self``); they live in their own module
so each mixin imports only what it uses. ``_base`` re-exports
``_validated_update`` / ``_deep`` at module scope for the small number
of external callers that import them directly (see
[`docs/plans/refactor-f1-storebase-split.md`](../../../../../../docs/plans/refactor-f1-storebase-split.md)
§D.5, issue #114).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from eden_contracts import ExecutionTask, Idea, Task
from pydantic import BaseModel

from ..submissions import Submission, VariantSubmission


def _validated_update[M: BaseModel](model: M, **changes: Any) -> M:
    """Return a copy of ``model`` with ``changes`` applied and re-validated.

    Replaces Pydantic's ``model_copy(update=...)``, which does **not**
    re-run validators. Without re-validation a caller could stamp an
    invalid ``commit_sha``, ``artifacts_uri``, or ``metrics`` shape
    onto a stored variant. Re-validating on every update is how every
    backend honors ``03-roles.md`` §3.4, §4.4 and ``08-storage.md``
    §3.

    Passing ``None`` for a field removes it (matches the ``NotNone``
    rule on optional typed fields in ``eden_contracts._common``:
    absent is permitted, explicit null is not).
    """
    data = model.model_dump(mode="json", exclude_none=True)
    for key, value in changes.items():
        if value is None:
            data.pop(key, None)
        elif isinstance(value, BaseModel):
            data[key] = value.model_dump(mode="json", exclude_none=True)
        else:
            data[key] = value
    return type(model).model_validate(data)


def _deep[M: BaseModel](model: M) -> M:
    """Return a deep copy of a Pydantic model.

    Used to insulate readers (``read_*``, ``list_*``, ``events``)
    from in-place mutation of stored values, and to insulate the
    store from mutation of caller-supplied values at ``create_*``
    time. Backends whose read path inherently rehydrates a fresh
    instance (e.g. SQLite's JSON round-trip) still go through
    ``_deep`` for uniformity.
    """
    return model.model_copy(deep=True)


# ----------------------------------------------------------------------
# Helpers for the §3.3 non-no-op variant check (used by
# `_TaskOpsMixin._validate_non_no_op_variant`). Split out so each gate of
# the rule reads as a named predicate.
# ----------------------------------------------------------------------


def _no_op_check_inputs(
    task: Task,
    submission: Submission,
    get_idea: Callable[[str], Idea | None],
) -> tuple[str, list[str]] | None:
    """Return ``(commit_sha, parent_commits)`` when the §3.3 check should run.

    Returns ``None`` (silently skip the check) when the submission
    shape, idea attachment, or parent list rules out a no-op tree
    comparison.
    """
    if not isinstance(submission, VariantSubmission):
        return None
    if submission.status != "success":
        return None
    if submission.commit_sha is None:
        return None
    assert isinstance(task, ExecutionTask)
    idea = get_idea(task.payload.idea_id)
    if idea is None or not idea.parent_commits:
        return None
    return submission.commit_sha, list(idea.parent_commits)


def _all_parents_equal_sha(sha: str, parents: list[str]) -> bool:
    """True when ``sha`` is byte-equal to every parent (Layer 1 fast path)."""
    return all(p == sha for p in parents)


def _resolve_trees(
    resolver: Callable[[str], str | None],
    sha: str,
    parents: list[str],
) -> tuple[str, list[str]] | None:
    """Run the tree resolver against ``sha`` + every parent.

    Returns ``(submission_tree, parent_trees)`` when every resolver
    call yields a non-``None`` tree. Returns ``None`` when the
    resolver raises or returns ``None`` for any SHA — Layer 2 is
    silently disabled for this submission in that case (Layer 1's
    fast path still applies).
    """
    try:
        sub_tree = resolver(sha)
    except Exception:  # noqa: BLE001 — resolver is binding-supplied; contain errors
        return None
    if sub_tree is None:
        return None
    parent_trees: list[str] = []
    for p in parents:
        try:
            t = resolver(p)
        except Exception:  # noqa: BLE001
            return None
        if t is None:
            return None
        parent_trees.append(t)
    return sub_tree, parent_trees


def _sha_equality_message(task_id: str, sha: str) -> str:
    return (
        f"execution submission for task {task_id!r} has "
        f"commit_sha={sha!r} equal to every parent_commit; the "
        "variant tree is identical to the parent tree (no-op). "
        "spec/v0/03-roles.md §3.3 non-no-op invariant."
    )


def _tree_identity_message(task_id: str, sha: str, sub_tree: str) -> str:
    return (
        f"execution submission for task {task_id!r} has "
        f"commit_sha={sha!r} whose tree {sub_tree!r} is identical "
        "to the tree of every parent_commit; the variant "
        "contributes no change. spec/v0/03-roles.md §3.3 "
        "non-no-op invariant."
    )
