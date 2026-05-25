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
from pydantic import ValidationError

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


def format_validation_errors(
    exc: ValidationError,
    *,
    row: int = 0,
    errors: FormErrors | None = None,
) -> FormErrors:
    """Translate a Pydantic ``ValidationError`` into a ``FormErrors``.

    Each pydantic error's ``loc[0]`` is treated as the form field name and
    the error's ``msg`` becomes the user-visible detail. Multi-element
    ``loc`` tuples (e.g. ``("parent_commits", 0)``) collapse to the
    top-level field so the template's by-field renderer picks them up.
    Errors with an empty ``loc`` fall through to ``add_overall`` so they
    still surface in the form-level banner.

    ``row`` selects which row the errors land under (multi-row ideator
    form). ``errors`` lets the caller accumulate into an existing
    ``FormErrors`` instance — useful when the route already started
    collecting non-Pydantic field errors.
    """
    if errors is None:
        errors = FormErrors()
    for err in exc.errors():
        loc = err.get("loc") or ()
        msg = err.get("msg") or "invalid value"
        if loc:
            field_name = str(loc[0])
            errors.add(row, field_name, msg)
        else:
            errors.add_overall(msg)
    return errors


def _validate_slug(i: int, slug: str, errors: FormErrors) -> None:
    if not slug:
        errors.add(i, "slug", "slug is required")
    elif not all(c.isalnum() or c in "-_" for c in slug):
        errors.add(i, "slug", "slug must be alphanumeric / dash / underscore")


def _parse_priority(i: int, raw: str, errors: FormErrors) -> float:
    try:
        return float(raw)
    except ValueError:
        errors.add(i, "priority", "priority must be a number")
        return 0.0


def _parse_parents(i: int, parents_raw: str, errors: FormErrors) -> tuple[str, ...]:
    parents = tuple(p.strip() for p in parents_raw.split(",") if p.strip())
    if not parents:
        errors.add(i, "parent_commits", "at least one parent commit SHA is required")
        return parents
    for parent in parents:
        if not (len(parent) == 40 and all(c in "0123456789abcdef" for c in parent.lower())):
            errors.add(i, "parent_commits", f"invalid SHA: {parent!r}")
            break
    return parents


def _parse_intended_executor(
    i: int,
    kind_raw: str,
    id_raw: str,
    errors: FormErrors,
) -> TaskTarget | None:
    if kind_raw not in ("none", "worker", "group"):
        errors.add(
            i,
            "intended_executor",
            "intended_executor kind must be 'none', 'worker', or 'group'",
        )
        return None
    if kind_raw == "none":
        if id_raw:
            # Operator typed an id but selected kind=none — surface
            # this as an error rather than silently dropping the id,
            # since the most likely cause is a mis-click.
            errors.add(
                i,
                "intended_executor",
                "intended_executor id supplied but kind is 'none'; "
                "pick a kind or clear the id",
            )
        return None
    if not id_raw:
        errors.add(
            i,
            "intended_executor",
            f"intended_executor id is required for kind={kind_raw!r}",
        )
        return None
    if not _REGISTRY_ID_PATTERN.fullmatch(id_raw):
        errors.add(
            i,
            "intended_executor",
            "intended_executor id must match the §6.1 registry-id grammar",
        )
        return None
    return TaskTarget(kind=kind_raw, id=id_raw)


def _parse_idea_row(
    i: int,
    *,
    slug: str,
    priority_raw: str,
    parents_raw: str,
    content: str,
    kind_raw: str,
    id_raw: str,
    has_uploads: bool,
    errors: FormErrors,
) -> IdeaDraft | None:
    """Validate one non-empty idea row; return the draft when row has no errors.

    Issue #120: ``content`` is required only when no file uploads
    are attached to the row. A row with uploads (but no markdown
    body) is a valid multi-file artifact — the bundler stores the
    uploads alone without a synthetic ``idea.md``.
    """
    _validate_slug(i, slug, errors)
    priority = _parse_priority(i, priority_raw, errors)
    parents = _parse_parents(i, parents_raw, errors)
    if not content and not has_uploads:
        errors.add(
            i,
            "content",
            "content markdown is required (or attach at least one file)",
        )
    intended_executor = _parse_intended_executor(i, kind_raw, id_raw, errors)
    if errors.by_row.get(i):
        return None
    return IdeaDraft(
        slug=slug,
        priority=priority,
        parent_commits=parents,
        content=content,
        intended_executor=intended_executor,
    )


def parse_idea_rows(
    slugs: list[str],
    priorities: list[str],
    parent_commits_csv: list[str],
    contents: list[str],
    intended_executor_kinds: list[str] | None = None,
    intended_executor_ids: list[str] | None = None,
    has_uploads_per_row: list[bool] | None = None,
) -> tuple[list[IdeaDraft], FormErrors, list[int]]:
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
        return [], errors, []

    kinds = intended_executor_kinds or []
    ids = intended_executor_ids or []
    uploads_present = has_uploads_per_row or []

    drafts: list[IdeaDraft] = []
    draft_rows: list[int] = []
    parsed_count = 0
    for i in range(n):
        slug = (slugs[i] if i < len(slugs) else "").strip()
        priority_raw = (priorities[i] if i < len(priorities) else "").strip()
        parents_raw = (
            parent_commits_csv[i] if i < len(parent_commits_csv) else ""
        ).strip()
        content = (contents[i] if i < len(contents) else "").strip()
        has_uploads = uploads_present[i] if i < len(uploads_present) else False

        # Skip fully-empty rows (priority defaults to "1.0" so a true
        # empty row has slug+parents+content all blank, and no
        # uploads). The "add another row" path adds blank trailing
        # rows; the user shouldn't be forced to fill every one
        # before submitting.
        if not slug and not parents_raw and not content and not has_uploads:
            continue

        kind_raw = (kinds[i] if i < len(kinds) else "").strip().lower() or "none"
        id_raw = (ids[i] if i < len(ids) else "").strip()

        parsed_count += 1
        draft = _parse_idea_row(
            i,
            slug=slug,
            priority_raw=priority_raw,
            parents_raw=parents_raw,
            content=content,
            kind_raw=kind_raw,
            id_raw=id_raw,
            has_uploads=has_uploads,
            errors=errors,
        )
        if draft is not None:
            drafts.append(draft)
            draft_rows.append(i)

    if parsed_count == 0:
        errors.add_overall("at least one idea row must be filled in")

    return drafts, errors, draft_rows


@dataclass(frozen=True)
class ExecutionDraft:
    """Validated executor-form input for one variant.

    The route handler combines this with the server-owned
    ``variant_id`` (from ``_CLAIMS``) and the idea's
    ``parent_commits`` to construct the ``Variant`` and
    ``VariantSubmission`` objects.

    ``artifacts_uri`` is the executor's optional supporting-artifacts
    URI (build logs, coverage reports, generated screenshots,
    profiling captures). When set, it flows through the submission
    onto ``Variant.executor_artifacts_uri`` per
    ``spec/v0/03-roles.md`` §3.4.
    """

    status: Literal["success", "error"]
    commit_sha: str | None
    description: str | None
    artifacts_uri: str | None = None


def parse_implement_form(
    *,
    status_raw: str,
    commit_sha_raw: str,
    description_raw: str,
    artifacts_uri_raw: str = "",
) -> tuple[ExecutionDraft | None, FormErrors]:
    """Parse the executor draft form into a validated draft.

    Returns ``(None, errors)`` if validation fails, otherwise
    ``(draft, FormErrors())``. ``commit_sha`` is required when
    ``status == "success"`` and must be 40 lowercase hex; on
    ``status == "error"`` it is ignored. ``description`` is
    optional free-form text; the route handler trims and
    converts the empty string to ``None``. ``artifacts_uri`` is
    optional; trimmed empty → ``None``. The wire-side Pydantic
    layer applies the RFC 3986 URI check.
    """
    errors = FormErrors()
    status = status_raw.strip().lower()
    if status not in ("success", "error"):
        errors.add(0, "status", "status must be one of: success, error")
        return None, errors

    commit_sha_input = commit_sha_raw.strip().lower()
    description = description_raw.strip()
    artifacts_uri = artifacts_uri_raw.strip()

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
            artifacts_uri=artifacts_uri or None,
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
