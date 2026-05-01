"""Test-fixture helpers that seed wire-protocol entities through the chapter-7 binding only.

Used by scenarios that need a precondition (e.g. a `claimed` task to
test claim-token semantics, a `success` trial to test integrate
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


def fresh_proposal_id(prefix: str = "p") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def fresh_trial_id(prefix: str = "tr") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def create_plan_task(
    client: WireClient,
    *,
    task_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> str:
    """POST /tasks for a `plan` task. Returns the task_id."""
    tid = task_id or fresh_task_id("plan")
    body = {
        "task_id": tid,
        "kind": "plan",
        "state": "pending",
        "payload": payload or {"experiment_id": client.experiment_id},
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    resp = client.post(client.tasks_path(), json=body)
    resp.raise_for_status()
    return tid


def create_evaluate_task(
    client: WireClient,
    *,
    trial_id: str,
    task_id: str | None = None,
) -> str:
    """POST /tasks for an `evaluate` task referencing the given trial."""
    tid = task_id or fresh_task_id("eval")
    body = {
        "task_id": tid,
        "kind": "evaluate",
        "state": "pending",
        "payload": {"trial_id": trial_id},
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    resp = client.post(client.tasks_path(), json=body)
    resp.raise_for_status()
    return tid


def create_implement_task(
    client: WireClient,
    *,
    proposal_id: str,
    task_id: str | None = None,
) -> str:
    """POST /tasks for an `implement` task referencing a `ready` proposal.

    The composite-commit invariant ([`05-event-protocol.md`](05-event-protocol.md) §2.2)
    flips the proposal `ready → dispatched` atomically with this insert.
    """
    tid = task_id or fresh_task_id("impl")
    body = {
        "task_id": tid,
        "kind": "implement",
        "state": "pending",
        "payload": {"proposal_id": proposal_id},
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


def submit_plan(
    client: WireClient,
    task_id: str,
    *,
    token: str,
    proposal_ids: list[str] | None = None,
    status: str = "success",
) -> Any:
    payload: dict[str, Any] = {"kind": "plan", "status": status}
    if status == "success":
        payload["proposal_ids"] = proposal_ids if proposal_ids is not None else []
    return client.post(
        client.tasks_path(task_id, "/submit"),
        json={"token": token, "payload": payload},
    )


def submit_implement(
    client: WireClient,
    task_id: str,
    *,
    token: str,
    trial_id: str,
    status: str = "success",
    commit_sha: str = "0" * 40,
) -> Any:
    payload: dict[str, Any] = {
        "kind": "implement",
        "status": status,
        "trial_id": trial_id,
    }
    if status == "success":
        payload["commit_sha"] = commit_sha
    return client.post(
        client.tasks_path(task_id, "/submit"),
        json={"token": token, "payload": payload},
    )


def submit_evaluate(
    client: WireClient,
    task_id: str,
    *,
    token: str,
    trial_id: str,
    status: str = "success",
    metrics: dict[str, Any] | None = None,
) -> Any:
    payload: dict[str, Any] = {
        "kind": "evaluate",
        "status": status,
        "trial_id": trial_id,
    }
    if status == "success":
        payload["metrics"] = metrics or {"score": 1.0, "retries": 0}
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


# Proposal / trial seeding -----------------------------------------


def create_proposal(
    client: WireClient,
    *,
    proposal_id: str | None = None,
    parent_commits: list[str] | None = None,
    slug: str = "test",
    artifacts_uri: str = "file:///tmp/eden-conformance-stub",
) -> str:
    pid = proposal_id or fresh_proposal_id()
    body = {
        "proposal_id": pid,
        "experiment_id": client.experiment_id,
        "slug": slug,
        "priority": 0.5,
        "state": "drafting",
        "parent_commits": parent_commits or ["0" * 40],
        "artifacts_uri": artifacts_uri,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    resp = client.post(client.proposals_path(), json=body)
    resp.raise_for_status()
    return pid


def mark_proposal_ready(client: WireClient, proposal_id: str) -> Any:
    return client.post(client.proposals_path(proposal_id, "/mark-ready"))


def create_trial(
    client: WireClient,
    *,
    proposal_id: str,
    trial_id: str | None = None,
    branch: str | None = None,
    commit_sha: str | None = None,
    status: str = "starting",
    parent_commits: list[str] | None = None,
) -> str:
    tid = trial_id or fresh_trial_id()
    body: dict[str, Any] = {
        "trial_id": tid,
        "experiment_id": client.experiment_id,
        "proposal_id": proposal_id,
        "status": status,
        "parent_commits": parent_commits or ["0" * 40],
        "started_at": _NOW,
    }
    if branch is not None:
        body["branch"] = branch
    if commit_sha is not None:
        body["commit_sha"] = commit_sha
    resp = client.post(client.trials_path(), json=body)
    resp.raise_for_status()
    return tid


def integrate_trial(
    client: WireClient,
    trial_id: str,
    *,
    trial_commit_sha: str,
) -> Any:
    return client.post(
        client.trials_path(trial_id, "/integrate"),
        json={"trial_commit_sha": trial_commit_sha},
    )


def declare_trial_eval_error(client: WireClient, trial_id: str) -> Any:
    return client.post(client.trials_path(trial_id, "/declare-eval-error"))


def read_task(client: WireClient, task_id: str) -> dict[str, Any]:
    resp = client.get(client.tasks_path(task_id))
    resp.raise_for_status()
    return resp.json()


def drive_to_starting_trial(
    client: WireClient,
    *,
    proposal_id: str | None = None,
    commit_sha: str = "1" * 40,
) -> str:
    """Drive a fresh proposal through implement-accept; trial is `starting` with commit_sha.

    The trial is NOT in `success` yet — that requires the evaluator
    cycle to also complete. Returns trial_id.
    """
    pid = proposal_id or create_proposal(client)
    mark_proposal_ready(client, pid)
    impl_tid = create_implement_task(client, proposal_id=pid)
    impl_claim = claim(client, impl_tid, worker_id="impl-worker")
    trial_id = fresh_trial_id()
    # Implementer creates the starting trial before submitting (chapter 3 §3.2 step 1).
    create_trial(
        client,
        trial_id=trial_id,
        proposal_id=pid,
        status="starting",
    )
    r = submit_implement(
        client,
        impl_tid,
        token=impl_claim["token"],
        trial_id=trial_id,
        commit_sha=commit_sha,
    )
    r.raise_for_status()
    accept(client, impl_tid)
    return trial_id


def drive_to_success_trial(
    client: WireClient,
    *,
    proposal_id: str | None = None,
    commit_sha: str = "1" * 40,
    metrics: dict[str, Any] | None = None,
) -> str:
    """Drive a fresh proposal through full implement → evaluate cycle.

    Returns trial_id of a trial in `status="success"` (the evaluator
    accept-step transitions the trial atomically per chapter 05 §2.2).
    """
    trial_id = drive_to_starting_trial(
        client, proposal_id=proposal_id, commit_sha=commit_sha
    )
    eval_tid = create_evaluate_task(client, trial_id=trial_id)
    eval_claim = claim(client, eval_tid, worker_id="eval-worker")
    r = submit_evaluate(
        client,
        eval_tid,
        token=eval_claim["token"],
        trial_id=trial_id,
        metrics=metrics,
    )
    r.raise_for_status()
    accept(client, eval_tid)
    return trial_id


def read_proposal(client: WireClient, proposal_id: str) -> dict[str, Any]:
    resp = client.get(client.proposals_path(proposal_id))
    resp.raise_for_status()
    return resp.json()


def read_trial(client: WireClient, trial_id: str) -> dict[str, Any]:
    resp = client.get(client.trials_path(trial_id))
    resp.raise_for_status()
    return resp.json()
