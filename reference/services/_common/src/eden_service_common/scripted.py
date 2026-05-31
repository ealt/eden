"""Canonical 8b scripted `ideation_fn` / `execution_fn` / `evaluation_fn`.

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
    EvaluationSchema,
    EvaluationTask,
    ExecutionTask,
    Idea,
    IdeationTask,
    Variant,
)
from eden_dispatch.workers import (
    EvaluationOutcome,
    ExecutionOutcome,
    IdeaTemplate,
)
from eden_git import GitRepo, Identity, TreeEntry

ScriptedPlanFn = Callable[[IdeationTask], list[IdeaTemplate]]
ScriptedImplementFn = Callable[[ExecutionTask, Idea], ExecutionOutcome]
ScriptedEvaluateFn = Callable[[EvaluationTask, Variant], EvaluationOutcome]


_IMPL_IDENTITY = Identity(
    name="EDEN Executor",
    email="executor@eden.invalid",
)
_IMPL_DATE = "2026-04-01T00:00:00+00:00"


def make_plan_fn(
    *, base_commit_sha: str, ideas_per_ideation: int = 1
) -> ScriptedPlanFn:
    """Build a ideation_fn that drafts ``ideas_per_ideation`` ideas per ideation task.

    Each idea carries ``parent_commits=(base_commit_sha,)``,
    satisfying the schema's ``min_length=1`` constraint.
    """

    def _plan(task: IdeationTask) -> list[IdeaTemplate]:
        templates: list[IdeaTemplate] = []
        for i in range(ideas_per_ideation):
            templates.append(
                IdeaTemplate(
                    slug=f"{task.task_id}-p{i}",
                    priority=float(ideas_per_ideation - i),
                    parent_commits=(base_commit_sha,),
                    # Fictional URI (scripted mode never writes bytes); shaped
                    # like the issue #168 layout for legible admin listings.
                    artifacts_uri=(
                        f"file:///tmp/artifacts/ideas/{task.task_id}-p{i}/content.md"
                    ),
                )
            )
        return templates

    return _plan


def make_implement_fn(
    *, repo_path: str, fail_every: int | None = None
) -> ScriptedImplementFn:
    """Build an execution_fn that writes a real git commit per task.

    The commit's parents come directly from ``idea.parent_commits``
    (supporting both single-parent and merge shapes). The commit's
    tree contains one deterministic blob keyed by idea slug +
    variant ID so each call produces a distinct commit.

    ``fail_every``, if set, returns ``status=error`` for every Nth
    task (1-indexed); useful for exercising rejection paths.
    """
    counter = itertools.count(1)

    def _implement(task: ExecutionTask, idea: Idea) -> ExecutionOutcome:
        index = next(counter)
        if fail_every is not None and fail_every > 0 and index % fail_every == 0:
            return ExecutionOutcome(status="error")

        repo = GitRepo(repo_path)
        variant_id = task.task_id.replace("execution-", "variant-")
        payload = f"variant={variant_id!r} slug={idea.slug!r}\n".encode()
        blob = repo.write_blob(payload)
        tree = repo.write_tree_from_entries(
            [
                TreeEntry(
                    mode="100644",
                    type="blob",
                    sha=blob,
                    path=f"{idea.slug}.txt",
                )
            ]
        )
        commit_sha = repo.commit_tree(
            tree,
            parents=list(idea.parent_commits),
            message=f"eden: {idea.slug} ({variant_id})\n",
            author=_IMPL_IDENTITY,
            committer=_IMPL_IDENTITY,
            author_date=_IMPL_DATE,
            committer_date=_IMPL_DATE,
        )
        branch_short = f"work/{variant_id}-{idea.slug}"
        repo.create_ref(f"refs/heads/{branch_short}", commit_sha)
        # Phase 10d follow-up B: when the local repo has an origin
        # remote (Forgejo cutover), publish the work/* ref so the
        # orchestrator's clone can fetch it. Push failure rolls back
        # the local ref + maps to ExecutionOutcome(status="error") —
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
                return ExecutionOutcome(status="error")
        return ExecutionOutcome(
            status="success",
            commit_sha=commit_sha,
            branch=branch_short,
        )

    return _implement


def make_evaluate_fn(
    *, evaluation_schema: EvaluationSchema, fail_every: int | None = None
) -> ScriptedEvaluateFn:
    """Build an evaluation_fn that emits deterministic metrics.

    Keys come from ``evaluation_schema`` so the metrics validate against
    the experiment's schema. Every real-valued key gets a fixed
    float; categorical keys get their first permitted value.

    ``fail_every``, if set, returns ``status=error`` for every Nth
    task (1-indexed).
    """
    counter = itertools.count(1)

    def _evaluate(task: EvaluationTask, variant: Variant) -> EvaluationOutcome:
        index = next(counter)
        if fail_every is not None and fail_every > 0 and index % fail_every == 0:
            return EvaluationOutcome(
                status="error",
                artifacts_uri=_scripted_eval_uri(variant.variant_id),
            )
        evaluation: dict[str, Any] = {}
        for name, kind in evaluation_schema.root.items():
            evaluation[name] = _default_for_kind(kind, index)
        return EvaluationOutcome(
            status="success",
            evaluation=evaluation,
            artifacts_uri=_scripted_eval_uri(variant.variant_id),
        )

    return _evaluate


def _scripted_eval_uri(variant_id: str) -> str:
    # Fictional URI (scripted mode never writes bytes); shaped like the issue
    # #168 layout (variants/<variant_id>/evaluator/) for legible admin listings.
    return f"file:///tmp/artifacts/variants/{variant_id}/evaluator/evaluation.md"


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
