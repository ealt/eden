"""Role-specific submission payloads passed to ``Store.submit``.

The wire-format task schemas (``spec/v0/schemas/task.schema.json``)
pin the *task* envelope; the *submission* payload is role-specific
and defined by the role contracts in ``spec/v0/03-roles.md`` §2.4,
§3.4, and §4.4. These dataclasses are the store's typed view of
those payloads — they are **not** wire objects; a cross-process
deployment (Phase 8) will carry them as JSON over the task-protocol
binding.

Fields are pinned by the spec:

- ``IdeaSubmission``        — status + idea_ids (set-equivalent
  per 04 §4.2).
- ``VariantSubmission``   — status + variant_id + commit_sha (03 §3.4).
- ``EvaluationSubmission``    — status + variant_id + metrics + optional
  artifacts_uri (03 §4.4, 04 §4.2 on metrics equivalence).

All three are ``frozen=True`` so callers cannot rebind fields after
construction. ``submit`` still deep-copies on entry and
``read_submission`` on exit, because the ``metrics`` dict on
``EvaluationSubmission`` is not itself frozen.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

PlanStatus = Literal["success", "error"]
ImplementStatus = Literal["success", "error"]
EvaluateStatus = Literal["success", "error", "evaluation_error"]


@dataclass(frozen=True)
class IdeaSubmission:
    """Ideator submission result. See ``spec/v0/03-roles.md`` §2.4."""

    status: PlanStatus
    idea_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class VariantSubmission:
    """Executor submission result. See ``spec/v0/03-roles.md`` §3.4.

    ``artifacts_uri`` is the executor's optional supporting-artifacts URI
    (build logs, coverage reports, generated screenshots, etc.). At
    execution-task terminal time the orchestrator writes this URI onto
    the variant's ``executor_artifacts_uri`` field
    (``spec/v0/02-data-model.md`` §9.1, ``03-roles.md`` §3.4), disjoint
    from the evaluator-written ``artifacts_uri`` set in §4.4. Per §3.4 +
    ``04-task-protocol.md`` §4.2, ``artifacts_uri`` is NOT part of
    submission equivalence; only ``status``, ``variant_id``, and
    ``commit_sha`` are.
    """

    status: ImplementStatus
    variant_id: str
    commit_sha: str | None = None
    artifacts_uri: str | None = None


@dataclass(frozen=True)
class EvaluationSubmission:
    """Evaluator submission result. See ``spec/v0/03-roles.md`` §4.4."""

    status: EvaluateStatus
    variant_id: str
    evaluation: dict[str, Any] | None = None
    artifacts_uri: str | None = None


Submission = IdeaSubmission | VariantSubmission | EvaluationSubmission


def submission_to_payload(submission: Submission) -> tuple[str, dict[str, Any]]:
    """Return ``(kind, payload)`` for one submission.

    Canonical de/serialization helper shared by every binding that has
    to round-trip a submission to/from a JSON-shaped representation —
    the SQL backends (sqlite + postgres), the wire endpoints
    (client + server), and the checkpoint JSONL writer. Each binding
    keeps the ``kind`` discriminator in a different place: SQL backends
    store it in a separate column; the wire payload inlines it into the
    JSON body; the checkpoint row inlines it too. Returning ``kind``
    separately lets each caller choose where to put it without
    repeating the field-mapping conditional five times.

    ``payload`` contains only the spec-normative fields per role with
    ``None`` values omitted — matches the on-the-wire shape.
    """
    if isinstance(submission, IdeaSubmission):
        return "ideation", {
            "status": submission.status,
            "idea_ids": list(submission.idea_ids),
        }
    if isinstance(submission, VariantSubmission):
        payload: dict[str, Any] = {
            "status": submission.status,
            "variant_id": submission.variant_id,
        }
        if submission.commit_sha is not None:
            payload["commit_sha"] = submission.commit_sha
        if submission.artifacts_uri is not None:
            payload["artifacts_uri"] = submission.artifacts_uri
        return "execution", payload
    if isinstance(submission, EvaluationSubmission):
        payload = {
            "status": submission.status,
            "variant_id": submission.variant_id,
        }
        if submission.evaluation is not None:
            payload["evaluation"] = submission.evaluation
        if submission.artifacts_uri is not None:
            payload["artifacts_uri"] = submission.artifacts_uri
        return "evaluation", payload
    raise TypeError(f"unknown submission type {type(submission).__name__}")


def submission_from_payload(kind: str, payload: dict[str, Any]) -> Submission:
    """Inverse of :func:`submission_to_payload` — strict.

    Required keys: ``status`` (always), ``variant_id`` (execution +
    evaluation). Raises ``KeyError`` on absent keys so wire + storage
    callers surface schema violations cleanly. The pre-refactor wire
    deserializers (server.py / client.py) raised ``KeyError`` on
    missing ``status``; this helper preserves that contract.

    Checkpoint imports want to accept legacy archives that omit
    ``status``; they call :func:`submission_from_payload_lenient`,
    which injects ``status="success"`` before delegating here.
    """
    if kind == "ideation":
        return IdeaSubmission(
            status=payload["status"],
            idea_ids=tuple(payload.get("idea_ids") or ()),
        )
    if kind == "execution":
        return VariantSubmission(
            status=payload["status"],
            variant_id=payload["variant_id"],
            commit_sha=payload.get("commit_sha"),
            artifacts_uri=payload.get("artifacts_uri"),
        )
    if kind == "evaluation":
        return EvaluationSubmission(
            status=payload["status"],
            variant_id=payload["variant_id"],
            evaluation=payload.get("evaluation"),
            artifacts_uri=payload.get("artifacts_uri"),
        )
    raise ValueError(f"unknown submission kind {kind!r}")


def submission_from_payload_lenient(
    kind: str, payload: dict[str, Any]
) -> Submission:
    """Lenient variant of :func:`submission_from_payload`.

    Defaults missing ``status`` to ``"success"``. Used by the
    checkpoint reader so legacy archives that omit ``status`` (the
    pre-refactor reader used ``row.get("status", "success")``) still
    import successfully. Wire and SQL backends MUST use the strict
    :func:`submission_from_payload` so a malformed payload surfaces.
    """
    if "status" not in payload:
        payload = {"status": "success", **payload}
    return submission_from_payload(kind, payload)


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
    if isinstance(a, IdeaSubmission) and isinstance(b, IdeaSubmission):
        return a.status == b.status and set(a.idea_ids) == set(b.idea_ids)
    if isinstance(a, VariantSubmission) and isinstance(b, VariantSubmission):
        return (
            a.status == b.status
            and a.variant_id == b.variant_id
            and a.commit_sha == b.commit_sha
        )
    if isinstance(a, EvaluationSubmission) and isinstance(b, EvaluationSubmission):
        return (
            a.status == b.status
            and a.variant_id == b.variant_id
            and a.evaluation == b.evaluation
        )
    return False
