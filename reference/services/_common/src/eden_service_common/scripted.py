"""Canonical 8b scripted `plan_fn` / `implement_fn` / `evaluate_fn`.

These fixed profiles are what the reference worker hosts use when no
other behavior is configured. They match the deterministic shape the
in-process Phase 5 / 7b tests use so the 8b subprocess E2E reaches
the same end state.

Knobs exposed as plain Python parameters — each worker host's CLI
layer picks them up and constructs the relevant factory. A `--profile`
switch is deliberately not introduced; swapping to LLM-backed hosts
is a separate binary at Phase 10.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from typing import Any

from eden_contracts import (
    EvaluateTask,
    ImplementTask,
    MetricsSchema,
    PlanTask,
    Proposal,
    Trial,
)
from eden_dispatch.workers import (
    EvaluateOutcome,
    ImplementOutcome,
    ProposalTemplate,
)
from eden_git import GitRepo, Identity, TreeEntry

ScriptedPlanFn = Callable[[PlanTask], list[ProposalTemplate]]
ScriptedImplementFn = Callable[[ImplementTask, Proposal], ImplementOutcome]
ScriptedEvaluateFn = Callable[[EvaluateTask, Trial], EvaluateOutcome]


_IMPL_IDENTITY = Identity(
    name="EDEN Implementer",
    email="implementer@eden.invalid",
)
_IMPL_DATE = "2026-04-01T00:00:00+00:00"


def make_plan_fn(
    *, base_commit_sha: str, proposals_per_plan: int = 1
) -> ScriptedPlanFn:
    """Build a plan_fn that drafts ``proposals_per_plan`` proposals per plan task.

    Each proposal carries ``parent_commits=(base_commit_sha,)``,
    satisfying the schema's ``min_length=1`` constraint.
    """

    def _plan(task: PlanTask) -> list[ProposalTemplate]:
        templates: list[ProposalTemplate] = []
        for i in range(proposals_per_plan):
            templates.append(
                ProposalTemplate(
                    slug=f"{task.task_id}-p{i}",
                    priority=float(proposals_per_plan - i),
                    parent_commits=(base_commit_sha,),
                    artifacts_uri=f"file:///tmp/artifacts/{task.task_id}/{i}",
                )
            )
        return templates

    return _plan


def make_implement_fn(
    *, repo_path: str, fail_every: int | None = None
) -> ScriptedImplementFn:
    """Build an implement_fn that writes a real git commit per task.

    The commit's parents come directly from ``proposal.parent_commits``
    (supporting both single-parent and merge shapes). The commit's
    tree contains one deterministic blob keyed by proposal slug +
    trial ID so each call produces a distinct commit.

    ``fail_every``, if set, returns ``status=error`` for every Nth
    task (1-indexed); useful for exercising rejection paths.
    """
    counter = itertools.count(1)

    def _implement(task: ImplementTask, proposal: Proposal) -> ImplementOutcome:
        index = next(counter)
        if fail_every is not None and fail_every > 0 and index % fail_every == 0:
            return ImplementOutcome(status="error")

        repo = GitRepo(repo_path)
        trial_id = task.task_id.replace("implement-", "trial-")
        payload = f"trial={trial_id!r} slug={proposal.slug!r}\n".encode()
        blob = repo.write_blob(payload)
        tree = repo.write_tree_from_entries(
            [
                TreeEntry(
                    mode="100644",
                    type="blob",
                    sha=blob,
                    path=f"{proposal.slug}.txt",
                )
            ]
        )
        commit_sha = repo.commit_tree(
            tree,
            parents=list(proposal.parent_commits),
            message=f"eden: {proposal.slug} ({trial_id})\n",
            author=_IMPL_IDENTITY,
            committer=_IMPL_IDENTITY,
            author_date=_IMPL_DATE,
            committer_date=_IMPL_DATE,
        )
        branch_short = f"work/{proposal.slug}-{trial_id}"
        repo.create_ref(f"refs/heads/{branch_short}", commit_sha)
        # Phase 10d follow-up B: when the local repo has an origin
        # remote (Gitea cutover), publish the work/* ref so the
        # orchestrator's clone can fetch it. Push failure rolls back
        # the local ref + maps to ImplementOutcome(status="error") —
        # mirrors the production subprocess flow per chapter 3 §3.3.
        if "origin" in repo._run(["remote"], check=False).stdout.split():
            try:
                repo.push_ref(f"refs/heads/{branch_short}")
            except Exception:  # noqa: BLE001 — git/transport-shaped
                import contextlib
                with contextlib.suppress(Exception):
                    repo.delete_ref(
                        f"refs/heads/{branch_short}",
                        expected_old_sha=commit_sha,
                    )
                return ImplementOutcome(status="error")
        return ImplementOutcome(
            status="success",
            commit_sha=commit_sha,
            branch=branch_short,
        )

    return _implement


def make_evaluate_fn(
    *, metrics_schema: MetricsSchema, fail_every: int | None = None
) -> ScriptedEvaluateFn:
    """Build an evaluate_fn that emits deterministic metrics.

    Keys come from ``metrics_schema`` so the metrics validate against
    the experiment's schema. Every real-valued key gets a fixed
    float; categorical keys get their first permitted value.

    ``fail_every``, if set, returns ``status=error`` for every Nth
    task (1-indexed).
    """
    counter = itertools.count(1)

    def _evaluate(task: EvaluateTask, trial: Trial) -> EvaluateOutcome:
        index = next(counter)
        if fail_every is not None and fail_every > 0 and index % fail_every == 0:
            return EvaluateOutcome(
                status="error",
                artifacts_uri=f"file:///tmp/artifacts/{trial.trial_id}",
            )
        metrics: dict[str, Any] = {}
        for name, kind in metrics_schema.root.items():
            metrics[name] = _default_for_kind(kind, index)
        return EvaluateOutcome(
            status="success",
            metrics=metrics,
            artifacts_uri=f"file:///tmp/artifacts/{trial.trial_id}",
        )

    return _evaluate


def _default_for_kind(kind: str, index: int) -> Any:
    """Return a deterministic metric value matching a MetricType literal."""
    if kind == "real":
        return 0.5 + (index % 3) * 0.1
    if kind == "integer":
        return index
    if kind == "text":
        return f"value-{index}"
    # MetricType is a closed Literal, so this should be unreachable.
    raise ValueError(f"unknown metric kind {kind!r}")
