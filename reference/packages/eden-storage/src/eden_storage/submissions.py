"""Role-specific submission payloads passed to ``Store.submit``.

The wire-format task schemas (``spec/v0/schemas/task.schema.json``)
pin the *task* envelope; the *submission* payload is role-specific
and defined by the role contracts in ``spec/v0/03-roles.md`` §2.4,
§3.4, and §4.4. These dataclasses are the store's typed view of
those payloads — they are **not** wire objects; a cross-process
deployment (Phase 8) will carry them as JSON over the task-protocol
binding.

Fields are pinned by the spec:

- ``PlanSubmission``        — status + proposal_ids (set-equivalent
  per 04 §4.2).
- ``ImplementSubmission``   — status + trial_id + commit_sha (03 §3.4).
- ``EvaluateSubmission``    — status + trial_id + metrics + optional
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
class PlanSubmission:
    """Planner submission result. See ``spec/v0/03-roles.md`` §2.4."""

    status: PlanStatus
    proposal_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ImplementSubmission:
    """Implementer submission result. See ``spec/v0/03-roles.md`` §3.4."""

    status: ImplementStatus
    trial_id: str
    commit_sha: str | None = None


@dataclass(frozen=True)
class EvaluateSubmission:
    """Evaluator submission result. See ``spec/v0/03-roles.md`` §4.4."""

    status: EvaluateStatus
    trial_id: str
    metrics: dict[str, Any] | None = None
    artifacts_uri: str | None = None


Submission = PlanSubmission | ImplementSubmission | EvaluateSubmission


def submissions_equivalent(a: Submission, b: Submission) -> bool:
    """Content equivalence per ``spec/v0/04-task-protocol.md`` §4.2.

    The normative fields per role (§4.2):

    - plan      — status + set of proposal_ids (order not significant).
    - implement — status + trial_id + commit_sha.
    - evaluate  — status + trial_id + metrics (as JSON values).

    ``artifacts_uri`` is deliberately absent from evaluate equivalence:
    §4.2 does not list it, so two submissions that agree on the
    normative fields are equivalent even if they differ in
    artifacts_uri, and the first submission's artifacts_uri is the
    committed one.
    """
    if type(a) is not type(b):
        return False
    if isinstance(a, PlanSubmission) and isinstance(b, PlanSubmission):
        return a.status == b.status and set(a.proposal_ids) == set(b.proposal_ids)
    if isinstance(a, ImplementSubmission) and isinstance(b, ImplementSubmission):
        return (
            a.status == b.status
            and a.trial_id == b.trial_id
            and a.commit_sha == b.commit_sha
        )
    if isinstance(a, EvaluateSubmission) and isinstance(b, EvaluateSubmission):
        return (
            a.status == b.status
            and a.trial_id == b.trial_id
            and a.metrics == b.metrics
        )
    return False
