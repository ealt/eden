"""Typed form parsers for the planner, implementer, and evaluator modules.

Planner form input arrives as a list of repeated field rows (one per
proposal); we parse it into ``ProposalDraft`` objects plus the
planner-level status. Implementer form input is single-row (one
trial per task); we parse it into a single ``ImplementDraft``.
Evaluator form input is single-row (one evaluate task → one
submission) with one input per declared metric; we parse it into a
single ``EvaluateDraft``. Validation errors are accumulated
field-by-field so forms re-render with the user's input intact.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from eden_contracts import MetricsSchema


@dataclass(frozen=True)
class ProposalDraft:
    """Validated planner-form input for one proposal.

    The canonical ``Proposal`` model is constructed by the route
    handler from this draft plus the server-generated
    ``proposal_id`` and ``artifacts_uri`` from the artifact writer.
    """

    slug: str
    priority: float
    parent_commits: tuple[str, ...]
    rationale: str


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


def parse_proposal_rows(
    slugs: list[str],
    priorities: list[str],
    parent_commits_csv: list[str],
    rationales: list[str],
) -> tuple[list[ProposalDraft], FormErrors]:
    """Parse parallel-list form input into validated drafts + accumulated errors.

    Each row is one proposal. Fields are validated independently so a
    bad row 1 still yields an error report covering rows 2..N.
    """
    errors = FormErrors()
    n = max(len(slugs), len(priorities), len(parent_commits_csv), len(rationales))
    if n == 0:
        errors.add_overall("at least one proposal row is required")
        return [], errors

    drafts: list[ProposalDraft] = []
    parsed_count = 0
    for i in range(n):
        slug = (slugs[i] if i < len(slugs) else "").strip()
        priority_raw = (priorities[i] if i < len(priorities) else "").strip()
        parents_raw = (
            parent_commits_csv[i] if i < len(parent_commits_csv) else ""
        ).strip()
        rationale = (rationales[i] if i < len(rationales) else "").strip()

        # Skip fully-empty rows (priority defaults to "1.0" so a true
        # empty row has slug+parents+rationale all blank). The "add
        # another row" path adds blank trailing rows; the user
        # shouldn't be forced to fill every one before submitting.
        if not slug and not parents_raw and not rationale:
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

        if not rationale:
            errors.add(i, "rationale", "rationale markdown is required")

        parsed_count += 1
        if not errors.by_row.get(i):
            drafts.append(
                ProposalDraft(
                    slug=slug,
                    priority=priority,
                    parent_commits=parents,
                    rationale=rationale,
                )
            )

    if parsed_count == 0:
        errors.add_overall("at least one proposal row must be filled in")

    return drafts, errors


@dataclass(frozen=True)
class ImplementDraft:
    """Validated implementer-form input for one trial.

    The route handler combines this with the server-owned
    ``trial_id`` (from ``_CLAIMS``) and the proposal's
    ``parent_commits`` to construct the ``Trial`` and
    ``ImplementSubmission`` objects.
    """

    status: Literal["success", "error"]
    commit_sha: str | None
    description: str | None


def parse_implement_form(
    *,
    status_raw: str,
    commit_sha_raw: str,
    description_raw: str,
) -> tuple[ImplementDraft | None, FormErrors]:
    """Parse the implementer draft form into a validated draft.

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
        ImplementDraft(
            status=status,
            commit_sha=commit_sha,
            description=description or None,
        ),
        errors,
    )


@dataclass(frozen=True)
class EvaluateDraft:
    """Validated evaluator-form input for one trial.

    The route handler combines this with the server-pinned
    ``trial_id`` (read from ``task.payload.trial_id`` at claim time
    and stashed in ``_CLAIMS``) to construct the
    ``EvaluateSubmission`` object. There is no ``description``
    field — the evaluator's submission shape per
    ``spec/v0/03-roles.md`` §4.4 does not carry one; the operator's
    free-form notes belong with their own diagnostic artifacts.
    """

    status: Literal["success", "error", "eval_error"]
    metrics: dict[str, int | float | str]
    artifacts_uri: str | None


_BOOL_LITERALS = ("true", "false")


def _parse_metric_value(
    raw: str, mtype: str
) -> tuple[int | float | str | None, str | None]:
    """Parse a single metric value per ``02-data-model.md`` §1.3.

    Returns ``(value, error_message)``. ``value is None`` with a
    non-None error means a parse failure. ``value is None`` with no
    error means the field was effectively empty (and should be
    omitted from the metrics dict). The integer parser accepts the
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
    metrics_schema: MetricsSchema,
    status_raw: str,
    metric_inputs: Mapping[str, str],
    artifacts_uri_raw: str,
) -> tuple[EvaluateDraft | None, FormErrors]:
    """Parse the evaluator draft form into a validated draft.

    Returns ``(None, errors)`` if validation fails, otherwise
    ``(draft, FormErrors())``.

    - ``status`` must be one of ``success``, ``error``, ``eval_error``.
    - For each metric in ``metrics_schema.root``: the raw form value
      is parsed per :func:`_parse_metric_value`; an empty/whitespace
      input is treated as "metric omitted" (not an error). Per-metric
      type errors are accumulated under that metric's name.
    - ``status="success"`` requires at least one metric value
      (UI-side guardrail; the wire allows empty).
    - Any key in ``metric_inputs`` outside ``metrics_schema.root``
      is rejected (only reachable from a hand-crafted POST; the
      template generates inputs only for declared metrics).
    - ``artifacts_uri_raw.strip()`` → ``None`` if empty.
    """
    errors = FormErrors()
    status = status_raw.strip().lower()
    if status not in ("success", "error", "eval_error"):
        errors.add(0, "status", "status must be one of: success, error, eval_error")
        return None, errors

    schema = metrics_schema.root
    metrics: dict[str, int | float | str] = {}

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
        metrics[name] = value

    if status == "success" and not metrics:
        errors.add_overall(
            "status=success requires at least one metric value"
        )

    if errors:
        return None, errors

    artifacts_uri = artifacts_uri_raw.strip() or None

    return (
        EvaluateDraft(
            status=status,
            metrics=metrics,
            artifacts_uri=artifacts_uri,
        ),
        errors,
    )
