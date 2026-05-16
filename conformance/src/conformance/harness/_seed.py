"""Test-fixture helpers that seed wire-protocol entities through the chapter-7 binding only.

Used by scenarios that need a precondition (e.g. a `claimed` task to
test claim-ownership semantics, a `success` variant to test integrate
idempotency). All helpers go through the WireClient — no shortcuts
through the adapter or the underlying store.

12a-1 wave 5: every claim against a non-registered worker_id is
rejected by the §3.5 step-2 registration check. Tests that need a
worker_id MUST first call :func:`register_worker` (or rely on the
default-worker fixture that pre-registers
:data:`DEFAULT_WORKER_IDS`). The wire's ``X-Eden-Worker-Id`` header
substitutes for the bearer when the IUT runs with auth disabled
(reference adapter does so by default; see
``adapters/reference/adapter.py``).
"""

from __future__ import annotations

import uuid
from typing import Any

from .wire_client import WireClient

_NOW = "2026-05-01T00:00:00Z"

# Worker_ids that the default-worker harness fixture pre-registers
# against every fresh IUT. Scenarios that need additional ids should
# call :func:`register_worker` explicitly.
DEFAULT_WORKER_IDS: tuple[str, ...] = (
    "test-worker",
    "impl-worker",
    "eval-worker",
    "worker-a",
    "worker-b",
)


def fresh_task_id(prefix: str = "task") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def fresh_idea_id(prefix: str = "p") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def fresh_variant_id(prefix: str = "tr") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def fresh_worker_id(prefix: str = "w") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def fresh_group_id(prefix: str = "g") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


# Worker registry ------------------------------------------------------


def register_worker(
    client: WireClient,
    worker_id: str,
    *,
    labels: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """POST /workers — register a worker for this experiment.

    Per chapter 02 §6.3 registration is idempotent: a second call with
    the same ``worker_id`` succeeds and returns the existing record.
    The response body is the worker record; the first registration
    additionally includes ``registration_token`` for bearer auth.
    """
    body: dict[str, Any] = {"worker_id": worker_id}
    if labels is not None:
        body["labels"] = labels
    resp = client.post(_workers_path(client), json=body)
    resp.raise_for_status()
    return resp.json()


def register_default_workers(client: WireClient) -> None:
    """Idempotently register every id in :data:`DEFAULT_WORKER_IDS`."""
    for wid in DEFAULT_WORKER_IDS:
        register_worker(client, wid)


def create_group(
    client: WireClient,
    group_id: str,
    *,
    members: list[str] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"group_id": group_id, "members": members or []}
    resp = client.post(_groups_path(client), json=body)
    resp.raise_for_status()
    return resp.json()


def add_to_group(
    client: WireClient,
    group_id: str,
    member_id: str,
) -> Any:
    return client.post(
        _groups_path(client, group_id, "/members"),
        json={"member_id": member_id},
    )


def _workers_path(client: WireClient) -> str:
    return f"{client.base_path}/workers"


def _groups_path(
    client: WireClient,
    group_id: str | None = None,
    suffix: str = "",
) -> str:
    base = f"{client.base_path}/groups"
    if group_id is None:
        return base
    return f"{base}/{group_id}{suffix}"


# Task seeding ---------------------------------------------------------


def create_ideation_task(
    client: WireClient,
    *,
    task_id: str | None = None,
    payload: dict[str, Any] | None = None,
    target: dict[str, str] | None = None,
) -> str:
    """POST /tasks for a `ideation` task. Returns the task_id."""
    tid = task_id or fresh_task_id("ideation")
    body: dict[str, Any] = {
        "task_id": tid,
        "kind": "ideation",
        "state": "pending",
        "payload": payload or {"experiment_id": client.experiment_id},
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    if target is not None:
        body["target"] = target
    resp = client.post(client.tasks_path(), json=body)
    resp.raise_for_status()
    return tid


def create_evaluation_task(
    client: WireClient,
    *,
    variant_id: str,
    task_id: str | None = None,
    target: dict[str, str] | None = None,
) -> str:
    """POST /tasks for an `evaluation` task referencing the given variant."""
    tid = task_id or fresh_task_id("eval")
    body: dict[str, Any] = {
        "task_id": tid,
        "kind": "evaluation",
        "state": "pending",
        "payload": {"variant_id": variant_id},
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    if target is not None:
        body["target"] = target
    resp = client.post(client.tasks_path(), json=body)
    resp.raise_for_status()
    return tid


def create_execution_task(
    client: WireClient,
    *,
    idea_id: str,
    task_id: str | None = None,
    target: dict[str, str] | None = None,
) -> str:
    """POST /tasks for an `execution` task referencing a `ready` idea.

    The composite-commit invariant ([`05-event-protocol.md`](05-event-protocol.md) §2.2)
    flips the idea `ready → dispatched` atomically with this insert.
    """
    tid = task_id or fresh_task_id("execution")
    body: dict[str, Any] = {
        "task_id": tid,
        "kind": "execution",
        "state": "pending",
        "payload": {"idea_id": idea_id},
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    if target is not None:
        body["target"] = target
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
    """Claim a pending task on behalf of ``worker_id``.

    Per chapter 04 §3.3 the claimant identity comes from the
    binding's authenticated principal. With the reference adapter
    running auth-disabled, the wire reads the ``X-Eden-Worker-Id``
    header to derive the worker_id; this helper sets that header for
    each call so scenarios drive multi-worker behavior without a
    full credential dance.

    Returns the claim object (``worker_id``, ``claimed_at``,
    optionally ``expires_at``).
    """
    body: dict[str, Any] = {}
    if expires_at is not None:
        body["expires_at"] = expires_at
    resp = client.post(
        client.tasks_path(task_id, "/claim"),
        json=body,
        headers={"X-Eden-Worker-Id": worker_id},
    )
    resp.raise_for_status()
    return resp.json()


def submit_idea(
    client: WireClient,
    task_id: str,
    *,
    worker_id: str = "test-worker",
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
        json={"payload": payload},
        headers={"X-Eden-Worker-Id": worker_id},
    )


def submit_variant(
    client: WireClient,
    task_id: str,
    *,
    worker_id: str = "impl-worker",
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
        json={"payload": payload},
        headers={"X-Eden-Worker-Id": worker_id},
    )


def submit_evaluation(
    client: WireClient,
    task_id: str,
    *,
    worker_id: str = "eval-worker",
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
    if evaluation is not None:
        payload["evaluation"] = evaluation
    elif status == "success":
        payload["evaluation"] = {"score": 1.0, "retries": 0}
    if artifacts_uri is not None:
        payload["artifacts_uri"] = artifacts_uri
    return client.post(
        client.tasks_path(task_id, "/submit"),
        json={"payload": payload},
        headers={"X-Eden-Worker-Id": worker_id},
    )


def accept(client: WireClient, task_id: str) -> Any:
    return client.post(client.tasks_path(task_id, "/accept"))


def reject(client: WireClient, task_id: str, *, reason: str) -> Any:
    return client.post(client.tasks_path(task_id, "/reject"), json={"reason": reason})


def reclaim(client: WireClient, task_id: str, *, cause: str) -> Any:
    return client.post(client.tasks_path(task_id, "/reclaim"), json={"cause": cause})


def reassign_task(
    client: WireClient,
    task_id: str,
    *,
    new_target: dict[str, str] | None,
    reason: str,
    actor_id: str = "admin-actor",
) -> Any:
    """POST /tasks/{task_id}/reassign (12a-2 wire §2.7).

    ``new_target`` is exactly the post-reassign value of ``task.target``:
    ``None`` (encoded as JSON ``null``) opens the task to any registered
    worker; an ``{kind, id}`` dict scopes it to a worker or group.
    ``reason`` is non-empty audit text carried into the
    ``task.reassigned`` event.

    With auth disabled the reference adapter reads
    ``X-Eden-Worker-Id`` to derive the authenticated principal and
    stamps it as ``reassigned_by`` on the emitted event. ``actor_id``
    sets that header — pass a §6.1-grammar value
    (``admin-actor`` matches by default).
    """
    body: dict[str, Any] = {"new_target": new_target, "reason": reason}
    return client.post(
        client.tasks_path(task_id, "/reassign"),
        json=body,
        headers={"X-Eden-Worker-Id": actor_id},
    )


def update_dispatch_mode(
    client: WireClient,
    patch: dict[str, str],
    *,
    actor_id: str = "admin-actor",
) -> Any:
    """PATCH /dispatch_mode (12a-2 wire §2.8).

    ``patch`` is a partial dispatch_mode object — any subset of the
    four normative keys (``ideation_creation`` / ``execution_dispatch``
    / ``evaluation_dispatch`` / ``integration``). Each value MUST be
    ``"auto"`` or ``"manual"``; unknown keys are tolerated per §2.5
    and round-trip through.

    The server stamps ``updated_by`` from the authenticated principal;
    with auth disabled it falls back to the ``X-Eden-Worker-Id``
    header set here.
    """
    return client.patch(
        client.dispatch_mode_path(),
        json=patch,
        headers={"X-Eden-Worker-Id": actor_id},
    )


def read_dispatch_mode(client: WireClient) -> dict[str, str]:
    """GET /dispatch_mode (12a-2 wire §2.8 companion read)."""
    resp = client.get(client.dispatch_mode_path())
    resp.raise_for_status()
    return resp.json()


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


def declare_variant_evaluation_error(client: WireClient, variant_id: str) -> Any:
    return client.post(client.variants_path(variant_id, "/declare-evaluation-error"))


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
    exec_tid = create_execution_task(client, idea_id=pid)
    _claim = claim(client, exec_tid, worker_id="impl-worker")
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
        exec_tid,
        worker_id=_claim["worker_id"],
        variant_id=variant_id,
        commit_sha=commit_sha,
    )
    r.raise_for_status()
    accepted = accept(client, exec_tid)
    accepted.raise_for_status()
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
    the chapter-3 §3.4 executor status=error path.
    """
    pid = idea_id or create_idea(client)
    mark_idea_ready(client, pid)
    exec_tid = create_execution_task(client, idea_id=pid)
    _claim = claim(client, exec_tid, worker_id="impl-worker")
    variant_id = fresh_variant_id()
    create_variant(
        client,
        variant_id=variant_id,
        idea_id=pid,
        status="starting",
    )
    r = submit_variant(
        client,
        exec_tid,
        worker_id=_claim["worker_id"],
        variant_id=variant_id,
        status="error",
    )
    r.raise_for_status()
    rejected = reject(client, exec_tid, reason="worker_error")
    rejected.raise_for_status()
    variant = read_variant(client, variant_id)
    assert variant.get("status") == "error", (
        f"setup precondition: variant {variant_id!r} should be 'error' after "
        f"implement /reject with status=error; got {variant.get('status')!r}"
    )
    return variant_id


def drive_to_evaluation_error_variant(
    client: WireClient,
    *,
    idea_id: str | None = None,
    commit_sha: str = "1" * 40,
) -> str:
    """Drive a fresh idea through to ``status="evaluation_error"``."""
    variant_id = drive_to_starting_variant(
        client, idea_id=idea_id, commit_sha=commit_sha
    )
    declared = declare_variant_evaluation_error(client, variant_id)
    declared.raise_for_status()
    variant = read_variant(client, variant_id)
    assert variant.get("status") == "evaluation_error", (
        f"setup precondition: variant {variant_id!r} should be 'evaluation_error' "
        f"after declare-evaluation-error; got {variant.get('status')!r}"
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
    _claim = claim(client, eval_tid, worker_id="eval-worker")
    r = submit_evaluation(
        client,
        eval_tid,
        worker_id=_claim["worker_id"],
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
