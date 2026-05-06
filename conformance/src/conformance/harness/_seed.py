"""Test-fixture helpers that seed wire-protocol entities through the chapter-7 binding only.

Used by scenarios that need a precondition (e.g. a `claimed` task to
test claim-token semantics, a `success` variant to test integrate
idempotency). All helpers go through the WireClient — no shortcuts
through the adapter or the underlying store.
"""

from __future__ import annotations

import uuid
from typing import Any

from .wire_client import WireClient

_NOW = "2026-05-01T00:00:00Z"


def fresh_task_id(prefix: str = "task") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def fresh_idea_id(prefix: str = "p") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def fresh_variant_id(prefix: str = "tr") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def create_ideation_task(
    client: WireClient,
    *,
    task_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> str:
    """POST /tasks for a `ideation` task. Returns the task_id."""
    tid = task_id or fresh_task_id("ideation")
    body = {
        "task_id": tid,
        "kind": "ideation",
        "state": "pending",
        "payload": payload or {"experiment_id": client.experiment_id},
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    resp = client.post(client.tasks_path(), json=body)
    resp.raise_for_status()
    return tid


def create_evaluation_task(
    client: WireClient,
    *,
    variant_id: str,
    task_id: str | None = None,
) -> str:
    """POST /tasks for an `evaluate` task referencing the given variant."""
    tid = task_id or fresh_task_id("eval")
    body = {
        "task_id": tid,
        "kind": "evaluation",
        "state": "pending",
        "payload": {"variant_id": variant_id},
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    resp = client.post(client.tasks_path(), json=body)
    resp.raise_for_status()
    return tid


def create_execution_task(
    client: WireClient,
    *,
    idea_id: str,
    task_id: str | None = None,
) -> str:
    """POST /tasks for an `execution` task referencing a `ready` idea.

    The composite-commit invariant ([`05-event-protocol.md`](05-event-protocol.md) §2.2)
    flips the idea `ready → dispatched` atomically with this insert.
    """
    tid = task_id or fresh_task_id("execution")
    body = {
        "task_id": tid,
        "kind": "execution",
        "state": "pending",
        "payload": {"idea_id": idea_id},
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    resp = client.post(client.tasks_path(), json=body)
    resp.raise_for_status()
    return tid


def claim(
    client: WireClient,
    task_id: str,
    *,
    worker_id: str = "test-worker",
    expires_at: str | None = None,
) -> dict[str, Any]:
    """Claim a pending task; return the claim object (token, worker_id, ...)."""
    body: dict[str, Any] = {"worker_id": worker_id}
    if expires_at is not None:
        body["expires_at"] = expires_at
    resp = client.post(client.tasks_path(task_id, "/claim"), json=body)
    resp.raise_for_status()
    return resp.json()


def submit_idea(
    client: WireClient,
    task_id: str,
    *,
    token: str,
    idea_ids: list[str] | None = None,
    status: str = "success",
) -> Any:
    payload: dict[str, Any] = {
        "kind": "ideation",
        "status": status,
        "idea_ids": idea_ids if idea_ids is not None else [],
    }
    return client.post(
        client.tasks_path(task_id, "/submit"),
        json={"token": token, "payload": payload},
    )


def submit_variant(
    client: WireClient,
    task_id: str,
    *,
    token: str,
    variant_id: str,
    status: str = "success",
    commit_sha: str = "0" * 40,
) -> Any:
    payload: dict[str, Any] = {
        "kind": "execution",
        "status": status,
        "variant_id": variant_id,
    }
    if status == "success":
        payload["commit_sha"] = commit_sha
    return client.post(
        client.tasks_path(task_id, "/submit"),
        json={"token": token, "payload": payload},
    )


def submit_evaluation(
    client: WireClient,
    task_id: str,
    *,
    token: str,
    variant_id: str,
    status: str = "success",
    evaluation: dict[str, Any] | None = None,
    artifacts_uri: str | None = None,
) -> Any:
    payload: dict[str, Any] = {
        "kind": "evaluation",
        "status": status,
        "variant_id": variant_id,
    }
    # status=success defaults an evaluation object so well-formed
    # successes don't need to repeat the boilerplate. Other statuses
    # only get an evaluation when the caller explicitly passes one, so
    # eval_error/error tests can drive the §4.4 "discard
    # submission-carried evaluation" rule with a real wire payload.
    if evaluation is not None:
        payload["evaluation"] = evaluation
    elif status == "success":
        payload["evaluation"] = {"score": 1.0, "retries": 0}
    if artifacts_uri is not None:
        payload["artifacts_uri"] = artifacts_uri
    return client.post(
        client.tasks_path(task_id, "/submit"),
        json={"token": token, "payload": payload},
    )


def accept(client: WireClient, task_id: str) -> Any:
    return client.post(client.tasks_path(task_id, "/accept"))


def reject(client: WireClient, task_id: str, *, reason: str) -> Any:
    return client.post(client.tasks_path(task_id, "/reject"), json={"reason": reason})


def reclaim(client: WireClient, task_id: str, *, cause: str) -> Any:
    return client.post(client.tasks_path(task_id, "/reclaim"), json={"cause": cause})


# Idea / variant seeding -----------------------------------------


def create_idea(
    client: WireClient,
    *,
    idea_id: str | None = None,
    parent_commits: list[str] | None = None,
    slug: str = "test",
    artifacts_uri: str = "file:///tmp/eden-conformance-stub",
) -> str:
    pid = idea_id or fresh_idea_id()
    body = {
        "idea_id": pid,
        "experiment_id": client.experiment_id,
        "slug": slug,
        "priority": 0.5,
        "state": "drafting",
        "parent_commits": parent_commits or ["0" * 40],
        "artifacts_uri": artifacts_uri,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    resp = client.post(client.ideas_path(), json=body)
    resp.raise_for_status()
    return pid


def mark_idea_ready(client: WireClient, idea_id: str) -> Any:
    return client.post(client.ideas_path(idea_id, "/mark-ready"))


def create_variant(
    client: WireClient,
    *,
    idea_id: str,
    variant_id: str | None = None,
    branch: str | None = None,
    commit_sha: str | None = None,
    status: str = "starting",
    parent_commits: list[str] | None = None,
) -> str:
    tid = variant_id or fresh_variant_id()
    body: dict[str, Any] = {
        "variant_id": tid,
        "experiment_id": client.experiment_id,
        "idea_id": idea_id,
        "status": status,
        "parent_commits": parent_commits or ["0" * 40],
        "started_at": _NOW,
    }
    if branch is not None:
        body["branch"] = branch
    if commit_sha is not None:
        body["commit_sha"] = commit_sha
    resp = client.post(client.variants_path(), json=body)
    resp.raise_for_status()
    return tid


def integrate_variant(
    client: WireClient,
    variant_id: str,
    *,
    variant_commit_sha: str,
) -> Any:
    return client.post(
        client.variants_path(variant_id, "/integrate"),
        json={"variant_commit_sha": variant_commit_sha},
    )


def declare_variant_eval_error(client: WireClient, variant_id: str) -> Any:
    return client.post(client.variants_path(variant_id, "/declare-eval-error"))


def read_task(client: WireClient, task_id: str) -> dict[str, Any]:
    resp = client.get(client.tasks_path(task_id))
    resp.raise_for_status()
    return resp.json()


def drive_to_starting_variant(
    client: WireClient,
    *,
    idea_id: str | None = None,
    commit_sha: str = "1" * 40,
) -> str:
    """Drive a fresh idea through implement-accept; variant is `starting` with commit_sha.

    The variant is NOT in `success` yet — that requires the evaluator
    cycle to also complete. Returns variant_id.
    """
    pid = idea_id or create_idea(client)
    mark_idea_ready(client, pid)
    impl_tid = create_execution_task(client, idea_id=pid)
    impl_claim = claim(client, impl_tid, worker_id="impl-worker")
    variant_id = fresh_variant_id()
    # Executor creates the starting variant before submitting (chapter 3 §3.2 step 1).
    create_variant(
        client,
        variant_id=variant_id,
        idea_id=pid,
        status="starting",
    )
    r = submit_variant(
        client,
        impl_tid,
        token=impl_claim["token"],
        variant_id=variant_id,
        commit_sha=commit_sha,
    )
    r.raise_for_status()
    accepted = accept(client, impl_tid)
    accepted.raise_for_status()
    # Sanity-check that the accept actually wrote commit_sha onto the
    # variant — a setup regression that silently leaves the variant in an
    # unexpected shape would otherwise surface as a misleading
    # downstream test failure.
    variant = read_variant(client, variant_id)
    assert variant.get("commit_sha") == commit_sha, (
        f"setup precondition: variant {variant_id!r} should carry "
        f"commit_sha={commit_sha!r} after implement /accept; got "
        f"{variant.get('commit_sha')!r}"
    )
    return variant_id


def drive_to_error_variant(
    client: WireClient,
    *,
    idea_id: str | None = None,
) -> str:
    """Drive a fresh idea through implement-status-error.

    Returns variant_id of a variant that landed at ``status="error"`` via
    the chapter-3 §3.4 executor status=error path: executor
    creates the starting variant, submits ``status="error"``, then the
    orchestrator's reject path terminalizes the variant as error
    atomically with the task.failed event (chapter 05 §2.2 composite
    commit).
    """
    pid = idea_id or create_idea(client)
    mark_idea_ready(client, pid)
    impl_tid = create_execution_task(client, idea_id=pid)
    impl_claim = claim(client, impl_tid, worker_id="impl-worker")
    variant_id = fresh_variant_id()
    create_variant(
        client,
        variant_id=variant_id,
        idea_id=pid,
        status="starting",
    )
    r = submit_variant(
        client,
        impl_tid,
        token=impl_claim["token"],
        variant_id=variant_id,
        status="error",
    )
    r.raise_for_status()
    rejected = reject(client, impl_tid, reason="worker_error")
    rejected.raise_for_status()
    variant = read_variant(client, variant_id)
    assert variant.get("status") == "error", (
        f"setup precondition: variant {variant_id!r} should be 'error' after "
        f"implement /reject with status=error; got {variant.get('status')!r}"
    )
    return variant_id


def drive_to_eval_error_variant(
    client: WireClient,
    *,
    idea_id: str | None = None,
    commit_sha: str = "1" * 40,
) -> str:
    """Drive a fresh idea through to ``status="eval_error"``.

    Drives implement-accept (so the variant reaches ``starting`` with
    ``commit_sha``) and then calls ``declare_variant_eval_error`` to
    terminalize the variant as ``eval_error`` per chapter 04 §4.3 retry-
    exhaustion / chapter 05 §3.3 ``variant.eval_errored``. Returns
    variant_id.
    """
    variant_id = drive_to_starting_variant(
        client, idea_id=idea_id, commit_sha=commit_sha
    )
    declared = declare_variant_eval_error(client, variant_id)
    declared.raise_for_status()
    variant = read_variant(client, variant_id)
    assert variant.get("status") == "eval_error", (
        f"setup precondition: variant {variant_id!r} should be 'eval_error' "
        f"after declare-eval-error; got {variant.get('status')!r}"
    )
    return variant_id


def drive_to_success_variant(
    client: WireClient,
    *,
    idea_id: str | None = None,
    commit_sha: str = "1" * 40,
    evaluation: dict[str, Any] | None = None,
) -> str:
    """Drive a fresh idea through full implement → evaluate cycle.

    Returns variant_id of a variant in `status="success"` (the evaluator
    accept-step transitions the variant atomically per chapter 05 §2.2).
    """
    variant_id = drive_to_starting_variant(
        client, idea_id=idea_id, commit_sha=commit_sha
    )
    eval_tid = create_evaluation_task(client, variant_id=variant_id)
    eval_claim = claim(client, eval_tid, worker_id="eval-worker")
    r = submit_evaluation(
        client,
        eval_tid,
        token=eval_claim["token"],
        variant_id=variant_id,
        evaluation=evaluation,
    )
    r.raise_for_status()
    accepted = accept(client, eval_tid)
    accepted.raise_for_status()
    variant = read_variant(client, variant_id)
    assert variant.get("status") == "success", (
        f"setup precondition: variant {variant_id!r} should be 'success' after "
        f"evaluate /accept; got {variant.get('status')!r}"
    )
    return variant_id


def read_idea(client: WireClient, idea_id: str) -> dict[str, Any]:
    resp = client.get(client.ideas_path(idea_id))
    resp.raise_for_status()
    return resp.json()


def read_variant(client: WireClient, variant_id: str) -> dict[str, Any]:
    resp = client.get(client.variants_path(variant_id))
    resp.raise_for_status()
    return resp.json()
