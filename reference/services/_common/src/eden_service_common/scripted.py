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
from pathlib import Path
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

# Canonical URI prefix the Compose web-ui bind-mounts the artifacts
# substrate at. Real fixture files are written under
# ``--artifacts-dir`` (the worker host's view of the same bytes) and
# stamped onto submissions with this URI prefix so the web-ui's
# ``/artifacts?uri=...`` route resolves them to the bind-mount.
_FIXTURE_URI_PREFIX = "file:///var/lib/eden/artifacts"


def _emit_fixture(
    artifacts_dir: Path | None,
    relative_path: str,
    body: str,
    *,
    fallback_uri: str,
) -> str:
    """Write a fixture placeholder file and return its ``file://`` URI.

    When ``artifacts_dir`` is ``None`` the helper is a no-op and
    returns ``fallback_uri`` unchanged — that's the default-OFF mode
    that preserves the historical fictional ``file:///tmp/artifacts/...``
    pointers existing tests + smoke baselines depend on.
    """
    if artifacts_dir is None:
        return fallback_uri
    target = Path(artifacts_dir) / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)
    return f"{_FIXTURE_URI_PREFIX}/{relative_path}"


def make_plan_fn(
    *,
    base_commit_sha: str,
    ideas_per_ideation: int = 1,
    artifacts_dir: Path | None = None,
) -> ScriptedPlanFn:
    """Build a ideation_fn that drafts ``ideas_per_ideation`` ideas per ideation task.

    Each idea carries ``parent_commits=(base_commit_sha,)``,
    satisfying the schema's ``min_length=1`` constraint.

    When ``artifacts_dir`` is set, write a placeholder content file
    per idea-template under ``ideas/<task_id>/<i>/content.txt`` and
    stamp the matching ``file:///var/lib/eden/artifacts/...`` URI
    onto the template; ``None`` (the default) preserves the
    historical ``file:///tmp/artifacts/...`` fictional pointer.
    """

    def _plan(task: IdeationTask) -> list[IdeaTemplate]:
        templates: list[IdeaTemplate] = []
        for i in range(ideas_per_ideation):
            relative = f"ideas/{task.task_id}/{i}/content.txt"
            body = (
                f"Fixture idea content for {task.task_id}, slot {i}.\n"
                "A real ideator would emit the idea's rationale / prompt / blueprint here.\n"
            )
            uri = _emit_fixture(
                artifacts_dir,
                relative,
                body,
                fallback_uri=f"file:///tmp/artifacts/{task.task_id}/{i}",
            )
            templates.append(
                IdeaTemplate(
                    slug=f"{task.task_id}-p{i}",
                    priority=float(ideas_per_ideation - i),
                    parent_commits=(base_commit_sha,),
                    artifacts_uri=uri,
                )
            )
        return templates

    return _plan


def make_implement_fn(
    *,
    repo_path: str,
    fail_every: int | None = None,
    artifacts_dir: Path | None = None,
) -> ScriptedImplementFn:
    """Build an execution_fn that writes a real git commit per task.

    The commit's parents come directly from ``idea.parent_commits``
    (supporting both single-parent and merge shapes). The commit's
    tree contains one deterministic blob keyed by idea slug +
    variant ID so each call produces a distinct commit.

    ``fail_every``, if set, returns ``status=error`` for every Nth
    task (1-indexed); useful for exercising rejection paths.

    When ``artifacts_dir`` is set, write a placeholder notes file
    under ``variants/<variant_id>/notes.txt`` and stamp the matching
    URI onto the outcome (propagated to the Variant by
    ``ScriptedExecutor``); ``None`` (the default) leaves
    ``ExecutionOutcome.artifacts_uri`` unset, matching the
    historical scripted behavior.
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
        artifacts_uri: str | None = None
        if artifacts_dir is not None:
            relative = f"variants/{variant_id}/notes.txt"
            body = (
                f"Fixture executor notes for {variant_id} "
                f"(idea slug: {idea.slug!r}).\n"
                "A real executor would emit build logs / diffs / rationale here.\n"
            )
            artifacts_uri = _emit_fixture(
                artifacts_dir,
                relative,
                body,
                fallback_uri="",  # unreachable: artifacts_dir is not None
            )
        return ExecutionOutcome(
            status="success",
            commit_sha=commit_sha,
            branch=branch_short,
            artifacts_uri=artifacts_uri,
        )

    return _implement


def make_evaluate_fn(
    *,
    evaluation_schema: EvaluationSchema,
    fail_every: int | None = None,
    artifacts_dir: Path | None = None,
) -> ScriptedEvaluateFn:
    """Build an evaluation_fn that emits deterministic metrics.

    Keys come from ``evaluation_schema`` so the metrics validate against
    the experiment's schema. Every real-valued key gets a fixed
    float; categorical keys get their first permitted value.

    ``fail_every``, if set, returns ``status=error`` for every Nth
    task (1-indexed).

    When ``artifacts_dir`` is set, write a placeholder evaluation
    file under ``evaluations/<variant_id>/evaluation.txt`` and stamp
    the matching URI onto the outcome; ``None`` (the default)
    preserves the historical ``file:///tmp/artifacts/<variant_id>``
    fictional pointer.
    """
    counter = itertools.count(1)

    def _evaluate(task: EvaluationTask, variant: Variant) -> EvaluationOutcome:
        index = next(counter)
        relative = f"evaluations/{variant.variant_id}/evaluation.txt"
        body = (
            f"Fixture evaluation output for {variant.variant_id}.\n"
            "A real evaluator would emit metric traces / plots / logs here.\n"
        )
        artifacts_uri = _emit_fixture(
            artifacts_dir,
            relative,
            body,
            fallback_uri=f"file:///tmp/artifacts/{variant.variant_id}",
        )
        if fail_every is not None and fail_every > 0 and index % fail_every == 0:
            return EvaluationOutcome(
                status="error",
                artifacts_uri=artifacts_uri,
            )
        evaluation: dict[str, Any] = {}
        for name, kind in evaluation_schema.root.items():
            evaluation[name] = _default_for_kind(kind, index)
        return EvaluationOutcome(
            status="success",
            evaluation=evaluation,
            artifacts_uri=artifacts_uri,
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
