"""Per-page lineage view models for the task-transparency UI (phase 12a-1c).

Each function ``lineage_for_<kind>`` consumes the subject artifact +
a ``Store`` and returns the one-hop neighbors of the subject — never
two hops; never a transitive walk (see plan §2 decision 4).

These helpers are intentionally **plain Python** with no FastAPI
coupling so they can be exercised against a fixture Store in unit
tests (plan §10 wave 1).

Transport-shaped read failures during the lineage build are caught
per-call: the corresponding ``LineageLink`` slot is left ``None`` (or
the entry is dropped from the collection) and the view model's
``transport_errors`` counter is incremented so the template can
render a "lineage may be incomplete — N transport errors" banner.

``StorageNotFound`` is NOT a transport error — it indicates the
upstream artifact genuinely no longer exists, and the lineage
section renders ``(unknown)`` for that slot without bumping the
counter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from eden_contracts import (
    EvaluationTask,
    ExecutionTask,
    Idea,
    IdeationTask,
    Task,
    Variant,
)
from eden_storage.errors import NotFound as StorageNotFound
from eden_storage.submissions import IdeaSubmission, VariantSubmission

# Hard cap per collection; matches chunk-9e's _TRIAL_DETAIL_EVENT_CAP
# discipline (smaller cap because lineage rows are denser than event
# rows).
_LINEAGE_COLLECTION_CAP = 20


# ---------------------------------------------------------------------
# View models
# ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LineageLink:
    """One lineage entry — a hyperlink with a short context line."""

    label: str
    href: str
    descriptor: str


@dataclass(frozen=True, slots=True)
class IdeationTaskLineage:
    """Subject: an ideation task. One hop forward only (no upstream)."""

    ideas: tuple[LineageLink, ...] = ()
    ideas_total: int = 0
    transport_errors: int = 0


@dataclass(frozen=True, slots=True)
class ExecutionTaskLineage:
    """Subject: an execution task. One hop back, one hop forward."""

    idea: LineageLink | None = None
    variants: tuple[LineageLink, ...] = ()
    variants_total: int = 0
    transport_errors: int = 0


@dataclass(frozen=True, slots=True)
class EvaluationTaskLineage:
    """Subject: an evaluation task. One hop back; no forward link."""

    variant: LineageLink | None = None
    transport_errors: int = 0


@dataclass(frozen=True, slots=True)
class IdeaLineage:
    """Subject: an idea. One hop back to ideation task, one hop forward to variants."""

    ideation_task: LineageLink | None = None
    variants: tuple[LineageLink, ...] = ()
    variants_total: int = 0
    transport_errors: int = 0


@dataclass(frozen=True, slots=True)
class VariantLineage:
    """Subject: a variant. One hop back to exec task + idea, one hop forward to eval tasks."""

    execution_task: LineageLink | None = None
    idea: LineageLink | None = None
    evaluation_tasks: tuple[LineageLink, ...] = ()
    evaluation_tasks_total: int = 0
    transport_errors: int = 0


# ---------------------------------------------------------------------
# Internal accumulator — used during build to thread transport counts
# without re-constructing the frozen view model on every step.
# ---------------------------------------------------------------------


@dataclass
class _Accumulator:
    transport_errors: int = 0
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------
# Public builders
# ---------------------------------------------------------------------


def lineage_for_ideation_task(
    store: Any, task: IdeationTask
) -> IdeationTaskLineage:
    """Forward lineage of an ideation task — the ideas its submission produced."""
    acc = _Accumulator()
    submission = _safe_read_submission(store, task.task_id, acc)
    if not isinstance(submission, IdeaSubmission):
        return IdeationTaskLineage(transport_errors=acc.transport_errors)

    links: list[LineageLink] = []
    total = len(submission.idea_ids)
    for idea_id in submission.idea_ids[:_LINEAGE_COLLECTION_CAP]:
        link = _idea_link(store, idea_id, acc)
        if link is not None:
            links.append(link)
    return IdeationTaskLineage(
        ideas=tuple(links),
        ideas_total=total,
        transport_errors=acc.transport_errors,
    )


def lineage_for_execution_task(
    store: Any, task: ExecutionTask
) -> ExecutionTaskLineage:
    """Lineage for an execution task — back to the idea, forward to variants."""
    acc = _Accumulator()
    idea_link = _idea_link(store, task.payload.idea_id, acc)

    try:
        variants = [
            v
            for v in store.list_variants()
            if v.idea_id == task.payload.idea_id
        ]
    except Exception:  # noqa: BLE001 — transport-shaped
        acc.transport_errors += 1
        variants = []
    variants.sort(key=lambda v: v.started_at or "")

    total = len(variants)
    capped = variants[:_LINEAGE_COLLECTION_CAP]
    var_links = tuple(_variant_link_from_obj(v) for v in capped)
    return ExecutionTaskLineage(
        idea=idea_link,
        variants=var_links,
        variants_total=total,
        transport_errors=acc.transport_errors,
    )


def lineage_for_evaluation_task(
    store: Any, task: EvaluationTask
) -> EvaluationTaskLineage:
    """Lineage for an evaluation task — back to the variant it targets."""
    acc = _Accumulator()
    variant_link = _variant_link(store, task.payload.variant_id, acc)
    return EvaluationTaskLineage(
        variant=variant_link,
        transport_errors=acc.transport_errors,
    )


def lineage_for_idea(store: Any, idea: Idea) -> IdeaLineage:
    """Lineage for an idea — back to the originating ideation task, forward to variants."""
    acc = _Accumulator()
    ideation_link = _ideation_task_link_for_idea(store, idea.idea_id, acc)

    try:
        variants = [
            v for v in store.list_variants() if v.idea_id == idea.idea_id
        ]
    except Exception:  # noqa: BLE001 — transport-shaped
        acc.transport_errors += 1
        variants = []
    variants.sort(key=lambda v: v.started_at or "")

    total = len(variants)
    capped = variants[:_LINEAGE_COLLECTION_CAP]
    var_links = tuple(_variant_link_from_obj(v) for v in capped)
    return IdeaLineage(
        ideation_task=ideation_link,
        variants=var_links,
        variants_total=total,
        transport_errors=acc.transport_errors,
    )


def lineage_for_variant(store: Any, variant: Variant) -> VariantLineage:
    """Lineage for a variant — back to execution task + parent idea, forward to evaluations."""
    acc = _Accumulator()
    idea_link = _idea_link(store, variant.idea_id, acc)
    exec_task_id = _producing_execution_task(store, variant, acc)
    exec_link: LineageLink | None = None
    if exec_task_id is not None:
        exec_link = _execution_task_link(store, exec_task_id, acc)

    try:
        eval_tasks = [
            t
            for t in store.list_tasks(kind="evaluation")
            if t.payload.variant_id == variant.variant_id
        ]
    except Exception:  # noqa: BLE001 — transport-shaped
        acc.transport_errors += 1
        eval_tasks = []
    eval_tasks.sort(key=lambda t: t.created_at or "")

    total = len(eval_tasks)
    capped = eval_tasks[:_LINEAGE_COLLECTION_CAP]
    eval_links = tuple(_eval_task_link_from_obj(t) for t in capped)
    return VariantLineage(
        execution_task=exec_link,
        idea=idea_link,
        evaluation_tasks=eval_links,
        evaluation_tasks_total=total,
        transport_errors=acc.transport_errors,
    )


# ---------------------------------------------------------------------
# Link builders
# ---------------------------------------------------------------------


def _idea_link(
    store: Any, idea_id: str, acc: _Accumulator
) -> LineageLink | None:
    try:
        idea = store.read_idea(idea_id)
    except StorageNotFound:
        return None
    except Exception:  # noqa: BLE001 — transport-shaped
        acc.transport_errors += 1
        return None
    return LineageLink(
        label="idea",
        href=f"/admin/ideas/{idea.idea_id}/",
        descriptor=f"slug={idea.slug}, state={idea.state}",
    )


def _variant_link(
    store: Any, variant_id: str, acc: _Accumulator
) -> LineageLink | None:
    try:
        variant = store.read_variant(variant_id)
    except StorageNotFound:
        return None
    except Exception:  # noqa: BLE001 — transport-shaped
        acc.transport_errors += 1
        return None
    return _variant_link_from_obj(variant)


def _variant_link_from_obj(variant: Variant) -> LineageLink:
    branch = variant.branch or "—"
    return LineageLink(
        label="variant",
        href=f"/admin/variants/{variant.variant_id}/",
        descriptor=f"branch={branch}, status={variant.status}",
    )


def _execution_task_link(
    store: Any, task_id: str, acc: _Accumulator
) -> LineageLink | None:
    try:
        task = store.read_task(task_id)
    except StorageNotFound:
        return None
    except Exception:  # noqa: BLE001 — transport-shaped
        acc.transport_errors += 1
        return None
    return LineageLink(
        label="execution task",
        href=f"/admin/tasks/{task.task_id}/",
        descriptor=f"state={task.state}",
    )


def _eval_task_link_from_obj(task: Task) -> LineageLink:
    return LineageLink(
        label="evaluation task",
        href=f"/admin/tasks/{task.task_id}/",
        descriptor=f"state={task.state}",
    )


def _ideation_task_link_for_idea(
    store: Any, idea_id: str, acc: _Accumulator
) -> LineageLink | None:
    """Reverse-walk ideation tasks to find the one whose submission produced ``idea_id``.

    See plan §D.9. Pre-submit ideation tasks have no submission to
    read and are skipped; status=error submissions carry empty
    ``idea_ids`` and don't match. The walk is best-effort: a
    transport-shaped failure on any single ``read_submission`` bumps
    the counter and continues with the remaining candidates.
    """
    try:
        tasks = store.list_tasks(kind="ideation")
    except Exception:  # noqa: BLE001 — transport-shaped
        acc.transport_errors += 1
        return None
    for t in tasks:
        if t.state not in {"submitted", "completed", "failed"}:
            continue
        try:
            submission = store.read_submission(t.task_id)
        except StorageNotFound:
            continue
        except Exception:  # noqa: BLE001 — transport-shaped
            acc.transport_errors += 1
            continue
        if not isinstance(submission, IdeaSubmission):
            continue
        if idea_id in submission.idea_ids:
            return LineageLink(
                label="ideation task",
                href=f"/admin/tasks/{t.task_id}/",
                descriptor=f"state={t.state}",
            )
    return None


def _producing_execution_task(
    store: Any, variant: Variant, acc: _Accumulator
) -> str | None:
    """Resolve which execution task produced ``variant`` (plan §D.9).

    Strategy:

    1. Walk execution tasks whose ``payload.idea_id == variant.idea_id``.
    2. Prefer the candidate whose submission's
       :class:`VariantSubmission` has matching ``variant_id``.
    3. Otherwise fall back to attribution match (``task.submitted_by ==
       variant.executed_by``) ONLY when exactly one candidate matches —
       multiple candidates with the same attribution means we cannot
       disambiguate without guessing, so return ``None``.
    """
    try:
        candidates = [
            t
            for t in store.list_tasks(kind="execution")
            if t.payload.idea_id == variant.idea_id
        ]
    except Exception:  # noqa: BLE001 — transport-shaped
        acc.transport_errors += 1
        return None

    for t in candidates:
        if t.state not in {"submitted", "completed", "failed"}:
            continue
        try:
            sub = store.read_submission(t.task_id)
        except StorageNotFound:
            continue
        except Exception:  # noqa: BLE001 — transport-shaped
            acc.transport_errors += 1
            continue
        if (
            isinstance(sub, VariantSubmission)
            and sub.variant_id == variant.variant_id
        ):
            return t.task_id

    if variant.executed_by is not None:
        attr_matches = [
            t for t in candidates if t.submitted_by == variant.executed_by
        ]
        if len(attr_matches) == 1:
            return attr_matches[0].task_id
    return None


def _safe_read_submission(
    store: Any, task_id: str, acc: _Accumulator
) -> object | None:
    try:
        return store.read_submission(task_id)
    except StorageNotFound:
        return None
    except Exception:  # noqa: BLE001 — transport-shaped
        acc.transport_errors += 1
        return None


__all__ = [
    "EvaluationTaskLineage",
    "ExecutionTaskLineage",
    "IdeaLineage",
    "IdeationTaskLineage",
    "LineageLink",
    "VariantLineage",
    "lineage_for_evaluation_task",
    "lineage_for_execution_task",
    "lineage_for_idea",
    "lineage_for_ideation_task",
    "lineage_for_variant",
]
