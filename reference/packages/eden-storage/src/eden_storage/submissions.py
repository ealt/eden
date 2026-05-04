"""Role-specific submission payloads passed to ``Store.submit``.

The wire-format task schemas (``spec/v0/schemas/task.schema.json``)
pin the *task* envelope; the *submission* payload is role-specific
and defined by the role contracts in ``spec/v0/03-roles.md`` §2.4,
§3.4, and §4.4. These dataclasses are the store's typed view of
those payloads — they are **not** wire objects; a cross-process
deployment (Phase 8) will carry them as JSON over the task-protocol
binding.

Fields are pinned by the spec:

- ``IdeateSubmission``        — status + idea_ids (set-equivalent
  per 04 §4.2).
- ``ExecuteSubmission``   — status + variant_id + commit_sha (03 §3.4).
- ``EvaluateSubmission``    — status + variant_id + metrics + optional
  artifacts_uri (03 §4.4, 04 §4.2 on metrics equivalence).

All three are ``frozen=True`` so callers cannot rebind fields after
construction. ``submit`` still deep-copies on entry and
``read_submission`` on exit, because the ``metrics`` dict on
``EvaluateSubmission`` is not itself frozen.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

PlanStatus = Literal["success", "error"]
ImplementStatus = Literal["success", "error"]
EvaluateStatus = Literal["success", "error", "eval_error"]


@dataclass(frozen=True)
class IdeateSubmission:
    """Ideator submission result. See ``spec/v0/03-roles.md`` §2.4."""

    status: PlanStatus
    idea_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExecuteSubmission:
    """Executor submission result. See ``spec/v0/03-roles.md`` §3.4."""

    status: ImplementStatus
    variant_id: str
    commit_sha: str | None = None


@dataclass(frozen=True)
class EvaluateSubmission:
    """Evaluator submission result. See ``spec/v0/03-roles.md`` §4.4."""

    status: EvaluateStatus
    variant_id: str
    evaluation: dict[str, Any] | None = None
    artifacts_uri: str | None = None


Submission = IdeateSubmission | ExecuteSubmission | EvaluateSubmission


def submissions_equivalent(a: Submission, b: Submission) -> bool:
    """Content equivalence per ``spec/v0/04-task-protocol.md`` §4.2.

    The normative fields per role (§4.2):

    - plan      — status + set of idea_ids (order not significant).
    - implement — status + variant_id + commit_sha.
    - evaluate  — status + variant_id + metrics (as JSON values).

    ``artifacts_uri`` is deliberately absent from evaluate equivalence:
    §4.2 does not list it, so two submissions that agree on the
    normative fields are equivalent even if they differ in
    artifacts_uri, and the first submission's artifacts_uri is the
    committed one.
    """
    if type(a) is not type(b):
        return False
    if isinstance(a, IdeateSubmission) and isinstance(b, IdeateSubmission):
        return a.status == b.status and set(a.idea_ids) == set(b.idea_ids)
    if isinstance(a, ExecuteSubmission) and isinstance(b, ExecuteSubmission):
        return (
            a.status == b.status
            and a.variant_id == b.variant_id
            and a.commit_sha == b.commit_sha
        )
    if isinstance(a, EvaluateSubmission) and isinstance(b, EvaluateSubmission):
        return (
            a.status == b.status
            and a.variant_id == b.variant_id
            and a.evaluation == b.evaluation
        )
    return False
