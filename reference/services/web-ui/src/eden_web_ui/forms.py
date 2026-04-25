"""Typed form parsers for the planner and implementer modules.

Planner form input arrives as a list of repeated field rows (one per
proposal); we parse it into ``ProposalDraft`` objects plus the
planner-level status. Implementer form input is single-row (one
trial per task); we parse it into a single ``ImplementDraft``.
Validation errors are accumulated field-by-field so forms re-render
with the user's input intact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


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
