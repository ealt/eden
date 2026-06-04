"""Test-fixture helpers that seed wire-protocol entities through the chapter-7 binding only.

Used by scenarios that need a precondition (e.g. a `claimed` task to
test claim-ownership semantics, a `success` variant to test integrate
idempotency). All helpers go through the WireClient — no shortcuts
through the adapter or the underlying store.

12a-1 wave 5: every claim against a non-registered worker_id is
rejected by the §3.5 step-2 registration check. Tests that need a
worker_id MUST first call :func:`register_worker` (or rely on the
default-worker fixture that pre-registers
:data:`DEFAULT_WORKER_IDS`). The reference adapter runs auth-enabled
(`--admin-token`); :func:`register_worker` captures the §6.3-issued
registration token and stashes it on the ``WireClient`` so subsequent
``as_worker=<wid>`` calls authenticate via the §13 per-worker bearer.
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
    name: str | None = None,
    *,
    labels: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """POST /workers — register a worker for this experiment.

    Since the identity rename (#128) the server MINTS the opaque
    ``worker_id`` (chapter 02 §1.6/§6.1); the caller supplies only an
    OPTIONAL display ``name`` (§1.7). The first ``name`` argument is a
    stable display-name handle the scenario reuses; this helper records
    the handle -> minted-id mapping on the client so later
    ``as_worker=<handle>`` / ``member_ref(..., <handle>)`` calls resolve
    to the minted ``wkr_*`` id. Returns the worker record (its
    ``worker_id`` is the minted opaque id; the first registration also
    carries ``registration_token`` for bearer auth).
    """
    body: dict[str, Any] = {}
    if name is not None:
        body["name"] = name
    if labels is not None:
        body["labels"] = labels
    resp = client.post(_workers_path(client), json=body)
    resp.raise_for_status()
    record = resp.json()
    worker_id = record["worker_id"]
    token = record.get("registration_token")
    if isinstance(token, str) and token:
        client.register_worker_bearer(worker_id, f"{worker_id}:{token}")
    client.record_worker_identity(name, worker_id)
    return record


def register_default_workers(client: WireClient) -> None:
    """Idempotently register every id in :data:`DEFAULT_WORKER_IDS`.

    Also registers the auxiliary ``admins`` / ``orchestrators`` groups
    used by the wave-3 wire surfaces (``POST /tasks`` kind-keyed
    authority; ``terminate`` / ``reassign`` / ``dispatch_mode``
    admins-gating) and the conventional admin / orchestrator actor
    identities the scenario files name. The reference adapter runs
    auth-enabled, so every group-gated route hits the chapter 07 §13.3
    enforcement ladder; without these registrations the bulk of the
    suite would 403 on first contact.
    """
    for wid in DEFAULT_WORKER_IDS:
        register_worker(client, wid)
    for wid in ADMIN_ACTOR_IDS:
        register_worker(client, wid)
    for wid in ORCHESTRATOR_ACTOR_IDS:
        register_worker(client, wid)
    # Each handle above is registered as a display NAME; the minted
    # wkr_* id is recorded against the handle so the group-membership
    # adds and `as_worker=<handle>` calls below resolve correctly.
    # The §3.7 wire-side authority groups; chapter 07 §13.3 specifies
    # these as the canonical names of the principal groups the wire
    # consults. We seed them as empty and then add the actor IDs as
    # members; ``register_group(members=[...])`` could populate them
    # in one call but the two-step flow surfaces a clearer failure
    # mode if a member id isn't registered first.
    _ensure_group(client, "admins")
    _ensure_group(client, "orchestrators")
    for wid in ADMIN_ACTOR_IDS:
        _ensure_group_member(client, "admins", wid)
    for wid in ORCHESTRATOR_ACTOR_IDS:
        _ensure_group_member(client, "orchestrators", wid)


# Actor identities used by the conformance scenarios for admin-gated
# operations (``terminate_experiment`` / ``reassign_task`` /
# ``update_dispatch_mode``). All are pre-registered + added to the
# ``admins`` group by :func:`register_default_workers`.
ADMIN_ACTOR_IDS: tuple[str, ...] = (
    "admin-actor",
    "admin-eric",
    "orchestrator",
    "other-actor",
)

# Actor identities used by the conformance scenarios for orchestrators-
# gated operations (``accept`` / ``reject`` / ``policy-errors`` and the
# §6.3 attribution probes). Pre-registered + added to the
# ``orchestrators`` group.
ORCHESTRATOR_ACTOR_IDS: tuple[str, ...] = (
    "orchestrator-actor",
    "different-actor",
)


def _ensure_group(client: WireClient, name: str) -> None:
    """Idempotently create a group named ``name``; record handle -> minted id.

    The server mints the opaque ``grp_*`` id (#128); ``name`` is the
    stable display-name handle (e.g. the reserved ``admins`` /
    ``orchestrators`` names — created via the admin bearer that the
    client defaults to). A second create of an already-existing
    reserved name returns 409, which is fine here.
    """
    if client.group_id_for(name) != name:
        return  # already recorded for this client
    resp = client.post(_groups_path(client), json={"name": name})
    if resp.status_code not in (200, 409):
        resp.raise_for_status()
    if resp.status_code == 200:
        client.record_group_identity(name, resp.json()["group_id"])
        return
    # 409: the reserved group already exists (pre-seeded, or imported via
    # checkpoint into this receiver). Its opaque id was minted elsewhere,
    # so resolve it by name (§7.3 ?name= lookup) and record the handle ->
    # id mapping; otherwise later member-adds would address it by the bare
    # name and 404.
    lookup = client.request("GET", _groups_path(client), params={"name": name})
    lookup.raise_for_status()
    groups = lookup.json().get("groups", [])
    if groups:
        client.record_group_identity(name, groups[0]["group_id"])


def _ensure_group_member(
    client: WireClient, group_handle: str, member_handle: str
) -> None:
    """Idempotently add ``member_handle`` to ``group_handle`` (409 is fine).

    Both handles are resolved to their minted opaque ids: the group id
    in the URL path, the member id (``wkr_*`` or ``grp_*``) in the body.
    """
    resp = client.post(
        _groups_path(client, group_handle, "/members"),
        json={"member_id": _resolve_member_id(client, member_handle)},
    )
    if resp.status_code not in (200, 409):
        resp.raise_for_status()


def create_group(
    client: WireClient,
    name: str | None = None,
    *,
    members: list[str] | None = None,
) -> dict[str, Any]:
    """POST /groups — server mints the opaque ``grp_*`` id (#128).

    ``name`` is an optional display-name handle (recorded handle ->
    minted id); ``members`` are handles resolved to minted member ids.
    """
    body: dict[str, Any] = {
        "members": [_resolve_member_id(client, m) for m in (members or [])],
    }
    if name is not None:
        body["name"] = name
    resp = client.post(_groups_path(client), json=body)
    resp.raise_for_status()
    record = resp.json()
    client.record_group_identity(name, record["group_id"])
    return record


def add_to_group(
    client: WireClient,
    group_handle: str,
    member_handle: str,
) -> Any:
    return client.post(
        _groups_path(client, group_handle, "/members"),
        json={"member_id": _resolve_member_id(client, member_handle)},
    )


def _resolve_member_id(client: WireClient, handle: str) -> str:
    """Resolve a worker/group handle to its minted opaque id (else verbatim)."""
    resolved = client.worker_id_for(handle)
    if resolved != handle:
        return resolved
    return client.group_id_for(handle)


def _resolve_target(
    client: WireClient, target: dict[str, str]
) -> dict[str, str]:
    """Resolve a ``{kind, id}`` target's id handle to its minted opaque id.

    Leaves a target already carrying an opaque id (or an unknown
    handle, e.g. a deliberate "no such worker" probe) unchanged.
    """
    if "id" not in target or "kind" not in target:
        return target
    if target["kind"] == "group":
        resolved = client.group_id_for(target["id"])
    else:
        resolved = client.worker_id_for(target["id"])
    return {**target, "id": resolved}


def _workers_path(client: WireClient) -> str:
    return f"{client.base_path}/workers"


def _groups_path(
    client: WireClient,
    group_handle: str | None = None,
    suffix: str = "",
) -> str:
    base = f"{client.base_path}/groups"
    if group_handle is None:
        return base
    return f"{base}/{client.group_id_for(group_handle)}{suffix}"


# Task seeding ---------------------------------------------------------


def create_ideation_task(
    client: WireClient,
    *,
    task_id: str | None = None,
    payload: dict[str, Any] | None = None,
    target: dict[str, str] | None = None,
    actor_id: str = "admin-actor",
) -> str:
    """POST /tasks for a `ideation` task. Returns the task_id.

    ``POST /tasks`` with kind in {ideation, execution, evaluation} is
    gated on ``admins`` OR ``orchestrators`` group membership (chapter
    07 §3.7); ``actor_id`` MUST resolve to a worker in either group.
    The default ``admin-actor`` is pre-registered into ``admins`` by
    :func:`register_default_workers`.
    """
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
        body["target"] = _resolve_target(client, target)
    resp = client.post(client.tasks_path(), json=body, as_worker=actor_id)
    resp.raise_for_status()
    return tid


def create_evaluation_task(
    client: WireClient,
    *,
    variant_id: str,
    task_id: str | None = None,
    target: dict[str, str] | None = None,
    actor_id: str = "admin-actor",
) -> str:
    """POST /tasks for an `evaluation` task referencing the given variant.

    Same admins-or-orchestrators authority as ``create_ideation_task``.
    """
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
        body["target"] = _resolve_target(client, target)
    resp = client.post(client.tasks_path(), json=body, as_worker=actor_id)
    resp.raise_for_status()
    return tid


def create_execution_task(
    client: WireClient,
    *,
    idea_id: str,
    task_id: str | None = None,
    target: dict[str, str] | None = None,
    actor_id: str = "admin-actor",
) -> str:
    """POST /tasks for an `execution` task referencing a `ready` idea.

    The composite-commit invariant ([`05-event-protocol.md`](05-event-protocol.md) §2.2)
    flips the idea `ready → dispatched` atomically with this insert.

    Same admins-or-orchestrators authority as ``create_ideation_task``.
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
        body["target"] = _resolve_target(client, target)
    resp = client.post(client.tasks_path(), json=body, as_worker=actor_id)
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
    binding's authenticated principal. The reference adapter runs
    auth-enabled, so this helper resolves ``worker_id`` to the
    bearer registered on the client by :func:`register_worker` and
    authenticates as that worker for this single call.

    Returns the claim object (``worker_id``, ``claimed_at``,
    optionally ``expires_at``).
    """
    body: dict[str, Any] = {}
    if expires_at is not None:
        body["expires_at"] = expires_at
    resp = client.post(
        client.tasks_path(task_id, "/claim"),
        json=body,
        as_worker=worker_id,
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
        as_worker=worker_id,
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
        as_worker=worker_id,
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
        as_worker=worker_id,
    )


def accept(
    client: WireClient, task_id: str, *, actor_id: str = "orchestrator-actor"
) -> Any:
    """POST /tasks/{task_id}/accept — orchestrators-gated.

    ``actor_id`` MUST be a member of the ``orchestrators`` group;
    :func:`register_default_workers` pre-registers
    ``orchestrator-actor`` into that group.
    """
    return client.post(
        client.tasks_path(task_id, "/accept"), as_worker=actor_id
    )


def reject(
    client: WireClient,
    task_id: str,
    *,
    reason: str,
    actor_id: str = "orchestrator-actor",
) -> Any:
    """POST /tasks/{task_id}/reject — orchestrators-gated."""
    return client.post(
        client.tasks_path(task_id, "/reject"),
        json={"reason": reason},
        as_worker=actor_id,
    )


def reclaim(
    client: WireClient,
    task_id: str,
    *,
    cause: str,
    actor_id: str = "orchestrator-actor",
) -> Any:
    """POST /tasks/{task_id}/reclaim — worker-gated."""
    return client.post(
        client.tasks_path(task_id, "/reclaim"),
        json={"cause": cause},
        as_worker=actor_id,
    )


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

    Admins-gated per §13.3: ``actor_id`` MUST be a member of the
    ``admins`` group. The server stamps ``reassigned_by`` from the
    authenticated principal.
    """
    body: dict[str, Any] = {"new_target": new_target, "reason": reason}
    return client.post(
        client.tasks_path(task_id, "/reassign"),
        json=body,
        as_worker=actor_id,
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

    Admins-gated; the server stamps ``updated_by`` from the
    authenticated principal (the bearer registered for ``actor_id``).
    """
    return client.patch(
        client.dispatch_mode_path(),
        json=patch,
        as_worker=actor_id,
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
    actor_id: str = "test-worker",
) -> str:
    """POST /ideas — worker-gated; ``actor_id`` MUST be a registered worker."""
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
    resp = client.post(client.ideas_path(), json=body, as_worker=actor_id)
    resp.raise_for_status()
    return pid


def mark_idea_ready(
    client: WireClient, idea_id: str, *, actor_id: str = "test-worker"
) -> Any:
    """POST /ideas/{id}/mark-ready — worker-gated."""
    return client.post(
        client.ideas_path(idea_id, "/mark-ready"), as_worker=actor_id
    )


def create_variant(
    client: WireClient,
    *,
    idea_id: str,
    variant_id: str | None = None,
    branch: str | None = None,
    commit_sha: str | None = None,
    status: str = "starting",
    parent_commits: list[str] | None = None,
    actor_id: str = "impl-worker",
) -> str:
    """POST /variants — worker-gated; ``actor_id`` MUST be a registered worker."""
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
    resp = client.post(client.variants_path(), json=body, as_worker=actor_id)
    resp.raise_for_status()
    return tid


def integrate_variant(
    client: WireClient,
    variant_id: str,
    *,
    variant_commit_sha: str,
    actor_id: str = "orchestrator-actor",
) -> Any:
    """POST /variants/{id}/integrate — orchestrators-gated."""
    return client.post(
        client.variants_path(variant_id, "/integrate"),
        json={"variant_commit_sha": variant_commit_sha},
        as_worker=actor_id,
    )


def declare_variant_evaluation_error(
    client: WireClient, variant_id: str, *, actor_id: str = "eval-worker"
) -> Any:
    """POST /variants/{id}/declare-evaluation-error — worker-gated."""
    return client.post(
        client.variants_path(variant_id, "/declare-evaluation-error"),
        as_worker=actor_id,
    )


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


# Experiment lifecycle ops (12a-3 wire §2.9) ---------------------------


def terminate_experiment(
    client: WireClient,
    *,
    reason: str,
    actor_id: str = "admin-actor",
) -> Any:
    """POST /v0/experiments/{E}/terminate (12a-3 wire §2.9).

    Gated on ``admins`` OR ``orchestrators`` (issue #256); the server
    stamps ``terminated_by`` from the authenticated principal (the
    bearer registered for ``actor_id``). The body MUST NOT carry
    ``terminated_by`` (the request schema rejects unknown keys); pass
    only ``reason``.

    Returns the raw httpx response so the caller decides how to
    interpret status codes (e.g. 200 happy-path vs 200 idempotent
    re-terminate vs 4xx envelope assertions).
    """
    return client.post(
        client.terminate_path(),
        json={"reason": reason},
        as_worker=actor_id,
    )


def read_experiment_state(client: WireClient) -> dict[str, Any]:
    """GET /v0/experiments/{E}/state (12a-3 wire §2.9 companion read).

    Returns ``{"state": "running" | "terminated"}``. Either-auth on
    the reference adapter; no special header needed.
    """
    resp = client.get(client.state_path())
    resp.raise_for_status()
    return resp.json()


# Portable-checkpoint helpers (12b wire §14) --------------------------


def read_experiment(client: WireClient) -> dict[str, Any]:
    """GET /v0/experiments/{E} (12b chapter 07 §14.3 full read).

    Returns ``{"experiment_id", "state", "created_at", "imported_from"}``.
    Admin-gated server-side; with auth disabled the reference adapter
    accepts the unauthenticated read.
    """
    resp = client.get(client.experiment_path())
    resp.raise_for_status()
    return resp.json()


def export_checkpoint(client: WireClient) -> bytes:
    """POST /v0/experiments/{E}/checkpoint (12b chapter 07 §14.1).

    Returns the tar archive bytes. Raises if the server emits a
    non-2xx response.
    """
    resp = client.post(client.export_checkpoint_path())
    resp.raise_for_status()
    return resp.content


def import_checkpoint(
    client: WireClient,
    archive_bytes: bytes,
    *,
    as_experiment_id: str | None = None,
    omit_experiment_header: bool = True,
    extra_headers: dict[str, str] | None = None,
) -> Any:
    """POST /v0/checkpoints/import (12b chapter 07 §14.2).

    Returns the raw httpx response so the caller decides how to
    interpret status codes. Per the §1.3 carve-out, this binding
    defaults to OMITTING the ``X-Eden-Experiment-Id`` header (the
    endpoint accepts the body's manifest as the source of truth).
    """
    params: dict[str, str] = {}
    if as_experiment_id is not None:
        params["as_experiment_id"] = as_experiment_id
    return client.request_bytes(
        "POST",
        client.import_checkpoint_path(),
        content=archive_bytes,
        content_type="application/x-eden-checkpoint+tar",
        params=params or None,
        headers=extra_headers,
        omit_experiment_header=omit_experiment_header,
    )


def list_events(client: WireClient, **params: Any) -> list[dict[str, Any]]:
    """GET /v0/experiments/{E}/events; return the raw event list.

    Used by scenarios that assert event-log cardinality / shape after
    a sequence of writes (e.g. "exactly one experiment.terminated
    event" or "no second event from an idempotent re-call").
    """
    resp = client.get(client.events_path(), params=params)
    resp.raise_for_status()
    body = resp.json()
    if isinstance(body, dict) and "events" in body:
        return list(body["events"])
    return list(body)
