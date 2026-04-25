"""Typed form parsers for the planner module.

Form input arrives as a list of repeated field rows (one per
proposal). We parse it into a list of ``ProposalDraft`` objects
plus the planner-level status. Validation errors are accumulated
field-by-field so the form re-renders with the user's input intact.
"""

from __future__ import annotations

from dataclasses import dataclass, field


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
