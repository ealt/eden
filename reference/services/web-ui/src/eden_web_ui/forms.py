"""Typed form parsers for the ideator, executor, and evaluator modules.

Ideator form input arrives as a list of repeated field rows (one per
idea); we parse it into ``IdeaDraft`` objects plus the
ideator-level status. Executor form input is single-row (one
variant per task); we parse it into a single ``ExecutionDraft``.
Evaluator form input is single-row (one evaluation task → one
submission) with one input per declared metric; we parse it into a
single ``EvaluationDraft``. Validation errors are accumulated
field-by-field so forms re-render with the user's input intact.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from eden_contracts import EvaluationSchema, TaskTarget

# Registry-id grammar per spec/v0/02-data-model.md §6.1; reused for
# the 12a-3 `intended_executor` field's worker_id / group_id slot.
_REGISTRY_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class IdeaDraft:
    """Validated ideator-form input for one idea.

    The canonical ``Idea`` model is constructed by the route
    handler from this draft plus the server-generated
    ``idea_id`` and ``artifacts_uri`` from the artifact writer.

    The optional ``intended_executor`` is a 12a-3 routing hint: when
    set, the orchestrator's ``execution_dispatch`` decision copies it
    to the resulting execution task's ``target`` field per
    ``03-roles.md`` §6.2 decision-type 2. ``None`` means "no hint"
    (the resulting execution task is open to any registered
    executor-class worker).
    """

    slug: str
    priority: float
    parent_commits: tuple[str, ...]
    content: str
    intended_executor: TaskTarget | None = None


@dataclass
class FormErrors:
    """Accumulated per-field error messages, indexed by row then field."""

    by_row: dict[int, dict[str, str]] = field(default_factory=dict)
    overall: list[str] = field(default_factory=list)

    def add(self, row: int, field_name: str, message: str) -> None:
        """Record a field-level error for ``row`` / ``field_name``."""
        self.by_row.setdefault(row, {})[field_name] = message

    def add_overall(self, message: str) -> None:
        """Record a form-level (not row-specific) error."""
        self.overall.append(message)

    def __bool__(self) -> bool:
        return bool(self.by_row) or bool(self.overall)


def parse_idea_rows(
    slugs: list[str],
    priorities: list[str],
    parent_commits_csv: list[str],
    contents: list[str],
    intended_executor_kinds: list[str] | None = None,
    intended_executor_ids: list[str] | None = None,
) -> tuple[list[IdeaDraft], FormErrors]:
    """Parse parallel-list form input into validated drafts + accumulated errors.

    Each row is one idea. Fields are validated independently so a
    bad row 1 still yields an error report covering rows 2..N.

    12a-3: ``intended_executor_kinds`` (``"none"`` / ``"worker"`` /
    ``"group"``) + ``intended_executor_ids`` are an optional parallel
    list of routing hints per row. ``None`` (or ``"none"`` in the
    kinds list) yields a draft with no hint; ``"worker"`` /
    ``"group"`` with a registry-id-grammar-valid id yields a tagged
    ``TaskTarget``.
    """
    errors = FormErrors()
    n = max(len(slugs), len(priorities), len(parent_commits_csv), len(contents))
    if n == 0:
        errors.add_overall("at least one idea row is required")
        return [], errors

    kinds = intended_executor_kinds or []
    ids = intended_executor_ids or []

    drafts: list[IdeaDraft] = []
    parsed_count = 0
    for i in range(n):
        slug = (slugs[i] if i < len(slugs) else "").strip()
        priority_raw = (priorities[i] if i < len(priorities) else "").strip()
        parents_raw = (
            parent_commits_csv[i] if i < len(parent_commits_csv) else ""
        ).strip()
        content = (contents[i] if i < len(contents) else "").strip()

        # Skip fully-empty rows (priority defaults to "1.0" so a true
        # empty row has slug+parents+content all blank). The "add
        # another row" path adds blank trailing rows; the user
        # shouldn't be forced to fill every one before submitting.
        if not slug and not parents_raw and not content:
            continue

        if not slug:
            errors.add(i, "slug", "slug is required")
        elif not all(c.isalnum() or c in "-_" for c in slug):
            errors.add(i, "slug", "slug must be alphanumeric / dash / underscore")

        try:
            priority = float(priority_raw)
        except ValueError:
            errors.add(i, "priority", "priority must be a number")
            priority = 0.0

        parents = tuple(p.strip() for p in parents_raw.split(",") if p.strip())
        if not parents:
            errors.add(i, "parent_commits", "at least one parent commit SHA is required")
        else:
            for parent in parents:
                if not (len(parent) == 40 and all(c in "0123456789abcdef" for c in parent.lower())):
                    errors.add(i, "parent_commits", f"invalid SHA: {parent!r}")
                    break

        if not content:
            errors.add(i, "content", "content markdown is required")

        intended_executor: TaskTarget | None = None
        kind_raw = (kinds[i] if i < len(kinds) else "").strip().lower() or "none"
        id_raw = (ids[i] if i < len(ids) else "").strip()
        if kind_raw not in ("none", "worker", "group"):
            errors.add(
                i,
                "intended_executor",
                "intended_executor kind must be 'none', 'worker', or 'group'",
            )
        elif kind_raw != "none":
            if not id_raw:
                errors.add(
                    i,
                    "intended_executor",
                    f"intended_executor id is required for kind={kind_raw!r}",
                )
            elif not _REGISTRY_ID_PATTERN.fullmatch(id_raw):
                errors.add(
                    i,
                    "intended_executor",
                    "intended_executor id must match the §6.1 registry-id grammar",
                )
            else:
                intended_executor = TaskTarget(kind=kind_raw, id=id_raw)
        elif id_raw:
            # Operator typed an id but selected kind=none — surface
            # this as an error rather than silently dropping the id,
            # since the most likely cause is a mis-click.
            errors.add(
                i,
                "intended_executor",
                "intended_executor id supplied but kind is 'none'; "
                "pick a kind or clear the id",
            )

        parsed_count += 1
        if not errors.by_row.get(i):
            drafts.append(
                IdeaDraft(
                    slug=slug,
                    priority=priority,
                    parent_commits=parents,
                    content=content,
                    intended_executor=intended_executor,
                )
            )

    if parsed_count == 0:
        errors.add_overall("at least one idea row must be filled in")

    return drafts, errors


@dataclass(frozen=True)
class ExecutionDraft:
    """Validated executor-form input for one variant.

    The route handler combines this with the server-owned
    ``variant_id`` (from ``_CLAIMS``) and the idea's
    ``parent_commits`` to construct the ``Variant`` and
    ``VariantSubmission`` objects.
    """

    status: Literal["success", "error"]
    commit_sha: str | None
    description: str | None


def parse_implement_form(
    *,
    status_raw: str,
    commit_sha_raw: str,
    description_raw: str,
) -> tuple[ExecutionDraft | None, FormErrors]:
    """Parse the executor draft form into a validated draft.

    Returns ``(None, errors)`` if validation fails, otherwise
    ``(draft, FormErrors())``. ``commit_sha`` is required when
    ``status == "success"`` and must be 40 lowercase hex; on
    ``status == "error"`` it is ignored. ``description`` is
    optional free-form text; the route handler trims and
    converts the empty string to ``None``.
    """
    errors = FormErrors()
    status = status_raw.strip().lower()
    if status not in ("success", "error"):
        errors.add(0, "status", "status must be one of: success, error")
        return None, errors

    commit_sha_input = commit_sha_raw.strip().lower()
    description = description_raw.strip()

    commit_sha: str | None = None
    if status == "success":
        if not commit_sha_input:
            errors.add(0, "commit_sha", "commit_sha is required for status=success")
        elif len(commit_sha_input) != 40 or not all(
            c in "0123456789abcdef" for c in commit_sha_input
        ):
            errors.add(0, "commit_sha", "commit_sha must be 40 lowercase hex characters")
        else:
            commit_sha = commit_sha_input

    if errors:
        return None, errors

    return (
        ExecutionDraft(
            status=status,
            commit_sha=commit_sha,
            description=description or None,
        ),
        errors,
    )


@dataclass(frozen=True)
class EvaluationDraft:
    """Validated evaluator-form input for one variant.

    The route handler combines this with the server-pinned
    ``variant_id`` (read from ``task.payload.variant_id`` at claim time
    and stashed in ``_CLAIMS``) to construct the
    ``EvaluationSubmission`` object. There is no ``description``
    field — the evaluator's submission shape per
    ``spec/v0/03-roles.md`` §4.4 does not carry one; the operator's
    free-form notes belong with their own diagnostic artifacts.
    """

    status: Literal["success", "error", "evaluation_error"]
    evaluation: dict[str, int | float | str]
    artifacts_uri: str | None


_BOOL_LITERALS = ("true", "false")


def _parse_metric_value(
    raw: str, mtype: str
) -> tuple[int | float | str | None, str | None]:
    """Parse a single metric value per ``02-data-model.md`` §1.3.

    Returns ``(value, error_message)``. ``value is None`` with a
    non-None error means a parse failure. ``value is None`` with no
    error means the field was effectively empty (and should be
    omitted from the evaluation dict). The integer parser accepts the
    wire-legal form ``1.0`` per spec §1.3 (parses as float, then
    rejects non-integer values).
    """
    s = raw.strip()
    if mtype == "text":
        if not s:
            return None, None
        return s, None

    if not s:
        return None, None

    if mtype == "integer":
        if s.lower() in _BOOL_LITERALS:
            return None, "boolean is not an integer"
        try:
            f = float(s)
        except ValueError:
            return None, "value is not a number"
        if not math.isfinite(f):
            return None, "value is not finite"
        if not f.is_integer():
            return None, "value is not an integer"
        return int(f), None

    if mtype == "real":
        if s.lower() in _BOOL_LITERALS:
            return None, "boolean is not a real"
        try:
            f = float(s)
        except ValueError:
            return None, "value is not a number"
        if not math.isfinite(f):
            return None, "value is not finite"
        return f, None

    return None, f"unknown metric type {mtype!r}"


def parse_evaluate_form(
    *,
    evaluation_schema: EvaluationSchema,
    status_raw: str,
    metric_inputs: Mapping[str, str],
    artifacts_uri_raw: str,
) -> tuple[EvaluationDraft | None, FormErrors]:
    """Parse the evaluator draft form into a validated draft.

    Returns ``(None, errors)`` if validation fails, otherwise
    ``(draft, FormErrors())``.

    - ``status`` must be one of ``success``, ``error``, ``evaluation_error``.
    - For each metric in ``evaluation_schema.root``: the raw form value
      is parsed per :func:`_parse_metric_value`; an empty/whitespace
      input is treated as "metric omitted" (not an error). Per-metric
      type errors are accumulated under that metric's name.
    - ``status="success"`` requires at least one metric value
      (UI-side guardrail; the wire allows empty).
    - Any key in ``metric_inputs`` outside ``evaluation_schema.root``
      is rejected (only reachable from a hand-crafted POST; the
      template generates inputs only for declared metrics).
    - ``artifacts_uri_raw.strip()`` → ``None`` if empty.
    """
    errors = FormErrors()
    status = status_raw.strip().lower()
    if status not in ("success", "error", "evaluation_error"):
        errors.add(0, "status", "status must be one of: success, error, evaluation_error")
        return None, errors

    schema = evaluation_schema.root
    evaluation: dict[str, int | float | str] = {}

    for name, raw in metric_inputs.items():
        if name not in schema:
            # Unknown metric keys can only arrive via a hand-crafted
            # POST (the template emits inputs only for declared
            # metrics). Surface the rejection as an overall banner
            # so the user — already on the form for some other
            # reason — sees it; the field doesn't exist on the page
            # to render an inline error against.
            errors.add_overall(f"metric {name!r} not in schema")
            continue
        value, err = _parse_metric_value(raw, schema[name])
        if err is not None:
            errors.add(0, name, err)
            continue
        if value is None:
            continue
        evaluation[name] = value

    if status == "success" and not evaluation:
        errors.add_overall(
            "status=success requires at least one evaluation value"
        )

    if errors:
        return None, errors

    artifacts_uri = artifacts_uri_raw.strip() or None

    return (
        EvaluationDraft(
            status=status,
            evaluation=evaluation,
            artifacts_uri=artifacts_uri,
        ),
        errors,
    )
