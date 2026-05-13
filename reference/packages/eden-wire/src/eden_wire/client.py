"""``StoreClient`` — a ``Store``-Protocol-compatible HTTP client.

``StoreClient`` makes the EDEN wire binding look exactly like a
direct ``Store`` to callers. The dispatch driver, integrator, and
conformance scenarios all work against it unchanged: structural
Protocol conformance means "talks to a store" doesn't commit to a
transport.

Transport-indeterminate reconciliation on ``integrate_variant``
follows ``spec/v0/07-wire-protocol.md`` §5 — read-back resolves to
confirmed success, confirmed divergence (raise
``AtomicityViolation`` surfaced as ``InvalidPrecondition`` at the
store boundary so ``Integrator`` can distinguish it), or
``IndeterminateIntegration``.

The client does **not** retry other mutations (claim, submit,
reject, reclaim, accept) on indeterminate failures; those are the
caller's responsibility per the binding's §8.3.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

import httpx
from eden_contracts import (
    DispatchMode,
    EvaluationTask,
    Event,
    ExecutionTask,
    FailReason,
    Group,
    Idea,
    IdeationTask,
    ReclaimCause,
    Task,
    TaskAdapter,
    TaskClaim,
    TaskTarget,
    Variant,
    Worker,
)
from eden_storage.errors import InvalidPrecondition, NotFound
from eden_storage.submissions import (
    EvaluationSubmission,
    IdeaSubmission,
    Submission,
    VariantSubmission,
)

from .errors import raise_for_envelope

__all__ = ["IndeterminateIntegration", "StoreClient"]


class IndeterminateIntegration(RuntimeError):
    """An ``integrate_variant`` call's outcome cannot be determined.

    Raised by :meth:`StoreClient.integrate_variant` when a transport-
    indeterminate failure cannot be resolved by a read-back of the
    variant (read-back itself fails, or shows no
    ``variant_commit_sha``). The caller (typically
    ``Integrator.integrate``) MUST NOT assume the server has not
    committed, and MUST NOT compensate the ref. Operator
    intervention is required.
    """


class StoreClient:
    """HTTP client that satisfies the ``eden_storage.Store`` Protocol."""

    def __init__(
        self,
        base_url: str,
        experiment_id: str,
        *,
        bearer: str | None = None,
        token: str | None = None,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
        read_back_attempts: int = 3,
    ) -> None:
        """Construct a wire-binding client.

        Authentication: ``bearer`` is the §13 bearer in
        ``"<principal>:<secret>"`` form (e.g. ``"admin:<admin_token>"``
        or ``"<worker_id>:<registration_token>"``). The ``token``
        keyword is a backward-compatible alias for ``bearer`` retained
        for callers that haven't yet adopted the new naming; if both
        are provided, ``bearer`` wins.
        """
        self._experiment_id = experiment_id
        self._base_url = base_url.rstrip("/")
        self._base = f"{self._base_url}/v0/experiments/{experiment_id}"
        self._ref_base = f"{self._base_url}/_reference/experiments/{experiment_id}"
        self._headers: dict[str, str] = {"X-Eden-Experiment-Id": experiment_id}
        effective_bearer = bearer or token
        self._bearer: str | None = effective_bearer
        if effective_bearer is not None:
            self._headers["Authorization"] = f"Bearer {effective_bearer}"
        self._owns_client = client is None
        self._timeout = timeout
        self._client = client if client is not None else httpx.Client(timeout=timeout)
        self._read_back_attempts = read_back_attempts

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> StoreClient:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    @property
    def experiment_id(self) -> str:
        return self._experiment_id

    # ------------------------------------------------------------------
    # Request plumbing
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        extra_headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        headers = self._headers
        if extra_headers:
            headers = {**headers, **extra_headers}
        resp = self._client.request(
            method,
            path,
            params=params,
            json=json,
            headers=headers,
        )
        if 400 <= resp.status_code < 600:
            body = self._maybe_json(resp)
            if isinstance(body, dict) and "type" in body:
                raise_for_envelope(body)
            resp.raise_for_status()
        return resp

    @staticmethod
    def _maybe_json(resp: httpx.Response) -> Any:
        try:
            return resp.json()
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def read_task(self, task_id: str) -> Task:
        resp = self._request("GET", f"{self._base}/tasks/{task_id}")
        return TaskAdapter.validate_python(resp.json())

    def read_idea(self, idea_id: str) -> Idea:
        resp = self._request("GET", f"{self._base}/ideas/{idea_id}")
        return Idea.model_validate(resp.json())

    def read_variant(self, variant_id: str) -> Variant:
        resp = self._request("GET", f"{self._base}/variants/{variant_id}")
        return Variant.model_validate(resp.json())

    def read_submission(self, task_id: str) -> Submission | None:
        resp = self._request("GET", f"{self._base}/tasks/{task_id}/submission")
        if resp.status_code == 204:
            return None
        body = resp.json()
        return _submission_from_wire(body["kind"], body)

    def list_tasks(
        self,
        *,
        kind: str | None = None,
        state: str | None = None,
    ) -> list[Task]:
        params: dict[str, Any] = {}
        if kind is not None:
            params["kind"] = kind
        if state is not None:
            params["state"] = state
        resp = self._request("GET", f"{self._base}/tasks", params=params)
        return [TaskAdapter.validate_python(item) for item in resp.json()]

    def list_ideas(self, *, state: str | None = None) -> list[Idea]:
        params = {"state": state} if state is not None else None
        resp = self._request("GET", f"{self._base}/ideas", params=params)
        return [Idea.model_validate(item) for item in resp.json()]

    def list_variants(self, *, status: str | None = None) -> list[Variant]:
        params = {"status": status} if status is not None else None
        resp = self._request("GET", f"{self._base}/variants", params=params)
        return [Variant.model_validate(item) for item in resp.json()]

    def events(self) -> list[Event]:
        return self.replay()

    def replay(self) -> list[Event]:
        return self.read_range()

    def read_range(self, cursor: int | None = None) -> list[Event]:
        params: dict[str, Any] = {"cursor": cursor or 0}
        resp = self._request("GET", f"{self._base}/events", params=params)
        body = resp.json()
        return [Event.model_validate(e) for e in body["events"]]

    def subscribe(
        self, cursor: int | None = None, *, timeout: float | None = None
    ) -> list[Event]:
        """Long-poll one batch from the subscribe endpoint.

        ``timeout`` overrides the server-side long-poll window
        (``07-wire-protocol.md`` §6.2). When omitted, the server's
        configured default applies (30s in the reference impl).
        """
        params: dict[str, Any] = {"cursor": cursor or 0}
        if timeout is not None:
            params["timeout"] = timeout
        resp = self._request("GET", f"{self._base}/events/subscribe", params=params)
        body = resp.json()
        return [Event.model_validate(e) for e in body["events"]]

    # ------------------------------------------------------------------
    # Task lifecycle
    # ------------------------------------------------------------------

    def create_task(self, task: Task) -> Task:
        resp = self._request(
            "POST",
            f"{self._base}/tasks",
            json=task.model_dump(mode="json", exclude_none=True),
        )
        return TaskAdapter.validate_python(resp.json())

    def create_ideation_task(self, task_id: str) -> IdeationTask:
        task = IdeationTask.model_validate(
            {
                "task_id": task_id,
                "kind": "ideation",
                "state": "pending",
                "created_at": _now(),
                "updated_at": _now(),
                "payload": {"experiment_id": self._experiment_id},
            }
        )
        created = self.create_task(task)
        assert isinstance(created, IdeationTask)
        return created

    def create_execution_task(self, task_id: str, idea_id: str) -> ExecutionTask:
        task = ExecutionTask.model_validate(
            {
                "task_id": task_id,
                "kind": "execution",
                "state": "pending",
                "created_at": _now(),
                "updated_at": _now(),
                "payload": {"idea_id": idea_id},
            }
        )
        created = self.create_task(task)
        assert isinstance(created, ExecutionTask)
        return created

    def create_evaluation_task(self, task_id: str, variant_id: str) -> EvaluationTask:
        task = EvaluationTask.model_validate(
            {
                "task_id": task_id,
                "kind": "evaluation",
                "state": "pending",
                "created_at": _now(),
                "updated_at": _now(),
                "payload": {"variant_id": variant_id},
            }
        )
        created = self.create_task(task)
        assert isinstance(created, EvaluationTask)
        return created

    def _assert_bearer_matches_worker_id(self, worker_id: str) -> None:
        """Preflight that the call-supplied worker_id matches the bearer's principal.

        Per chapter 04 §3.3, authentication is a binding-layer concern
        and the §4.1 / §3.5 enforcement runs against the authenticated
        ``worker_id``. The server in auth-enabled mode reads the
        principal from the bearer and ignores any forwarded
        ``X-Eden-Worker-Id`` header; without this client-side check, a
        caller passing a mismatched ``worker_id`` would be silently
        re-bound to the bearer's identity at the server. The check is
        a no-op when no bearer is set (auth-disabled / TestClient
        posture) or when the bearer principal is ``admin``: admin
        bearers cannot reach worker-gated routes — the §13.3
        dispatcher 403s them — so any disagreement is rejected at the
        wire instead.
        """
        if self._bearer is None:
            return
        principal = self._bearer.split(":", 1)[0]
        if principal == "admin":
            return
        if principal != worker_id:
            raise ValueError(
                f"StoreClient call-supplied worker_id={worker_id!r} disagrees "
                f"with bearer principal {principal!r}; per chapter 04 §3.3 the "
                f"authenticated identity is load-bearing — instantiate a "
                f"separate StoreClient with the matching bearer to act as a "
                f"different worker"
            )

    def claim(
        self,
        task_id: str,
        worker_id: str,
        *,
        expires_at: datetime | str | None = None,
    ) -> TaskClaim:
        # Per §2.3 + §13: the server takes the claimant worker_id from
        # the authenticated bearer, not the request body. The
        # ``worker_id`` parameter survives on the client API for
        # symmetry with the in-process Store contract; the client
        # forwards it through ``X-Eden-Worker-Id`` so test deployments
        # without auth still see the right caller (auth-enabled
        # deployments ignore the header in favor of the bearer).
        self._assert_bearer_matches_worker_id(worker_id)
        body: dict[str, Any] = {}
        if expires_at is not None:
            body["expires_at"] = _as_wire_datetime(expires_at)
        resp = self._request(
            "POST",
            f"{self._base}/tasks/{task_id}/claim",
            json=body,
            extra_headers={"X-Eden-Worker-Id": worker_id},
        )
        return TaskClaim.model_validate(resp.json())

    def submit(
        self, task_id: str, worker_id: str, submission: Submission
    ) -> None:
        # Per §2.4 + §13: server forwards the authenticated worker_id
        # to Store.submit; client passes the same identity for
        # symmetry. ``X-Eden-Worker-Id`` carries the value when auth
        # is disabled (test posture); under §13 auth the bearer wins.
        self._assert_bearer_matches_worker_id(worker_id)
        payload = _submission_to_wire(submission)
        self._request(
            "POST",
            f"{self._base}/tasks/{task_id}/submit",
            json={"payload": payload},
            extra_headers={"X-Eden-Worker-Id": worker_id},
        )

    def accept(self, task_id: str) -> None:
        self._request("POST", f"{self._base}/tasks/{task_id}/accept")

    def reject(self, task_id: str, reason: FailReason) -> None:
        self._request("POST", f"{self._base}/tasks/{task_id}/reject", json={"reason": reason})

    def reclaim(self, task_id: str, cause: ReclaimCause) -> None:
        self._request(
            "POST",
            f"{self._base}/tasks/{task_id}/reclaim",
            json={"cause": cause},
        )

    def validate_acceptance(self, task_id: str) -> str | None:
        decision, reason = self.validate_terminal(task_id)
        if decision == "accept":
            return None
        return reason

    def validate_terminal(self, task_id: str) -> tuple[str, str | None]:
        resp = self._request(
            "GET",
            f"{self._ref_base}/tasks/{task_id}/validate-terminal",
        )
        body = resp.json()
        return body["decision"], body.get("reason")

    # ------------------------------------------------------------------
    # Ideas / variants
    # ------------------------------------------------------------------

    def create_idea(self, idea: Idea) -> None:
        self._request(
            "POST",
            f"{self._base}/ideas",
            json=idea.model_dump(mode="json", exclude_none=True),
        )

    def mark_idea_ready(self, idea_id: str) -> None:
        self._request("POST", f"{self._base}/ideas/{idea_id}/mark-ready")

    def create_variant(self, variant: Variant) -> None:
        self._request(
            "POST",
            f"{self._base}/variants",
            json=variant.model_dump(mode="json", exclude_none=True),
        )

    def declare_variant_evaluation_error(self, variant_id: str) -> None:
        self._request("POST", f"{self._base}/variants/{variant_id}/declare-evaluation-error")

    def integrate_variant(self, variant_id: str, variant_commit_sha: str) -> None:
        """Integrator integration with transport-indeterminate reconciliation.

        Implements the §5 three-outcome rule:

        - observed SHA == expected → success.
        - observed SHA != expected (and not None) → ``InvalidPrecondition``
          (same exception the server-side same-value idempotency
          divergence branch raises directly, so ``Integrator`` can
          distinguish it from other failures).
        - observed SHA absent, or read-back fails →
          ``IndeterminateIntegration``.
        """
        path = f"{self._base}/variants/{variant_id}/integrate"
        try:
            self._request("POST", path, json={"variant_commit_sha": variant_commit_sha})
            return
        except httpx.TransportError as exc:
            # httpx.ReadTimeout is a subclass of TransportError, so
            # this single clause catches all indeterminate transports.
            original = exc

        variant = self._try_read_variant(variant_id)
        if variant is None:
            raise IndeterminateIntegration(
                f"integrate_variant({variant_id!r}) transport failed "
                f"({type(original).__name__}) and read-back could not be "
                f"completed; server-side outcome unknown"
            ) from original
        observed = variant.variant_commit_sha
        if observed == variant_commit_sha:
            return  # confirmed success
        if observed is not None:
            raise InvalidPrecondition(
                f"variant {variant_id!r} is already integrated with a different "
                f"variant_commit_sha ({observed!r} != {variant_commit_sha!r})"
            ) from original
        raise IndeterminateIntegration(
            f"integrate_variant({variant_id!r}) transport failed "
            f"({type(original).__name__}); read-back shows no "
            f"variant_commit_sha, but the original request may still be "
            f"in flight — compensation is unsafe"
        ) from original

    def _try_read_variant(self, variant_id: str) -> Variant | None:
        for _ in range(self._read_back_attempts):
            try:
                return self.read_variant(variant_id)
            except Exception:
                continue
        return None

    # ------------------------------------------------------------------
    # Shared validators
    # ------------------------------------------------------------------

    def validate_evaluation(self, evaluation: dict[str, Any]) -> None:
        self._request(
            "POST",
            f"{self._ref_base}/validate/evaluation",
            json={"evaluation": evaluation},
        )

    # ------------------------------------------------------------------
    # Worker registry (12a-1) — chapter 7 §6 + §13
    # ------------------------------------------------------------------

    def register_worker(
        self,
        worker_id: str,
        *,
        labels: dict[str, str] | None = None,
        registered_by: str | None = None,  # noqa: ARG002 — set by server-side principal
    ) -> tuple[Worker, str | None]:
        body: dict[str, Any] = {"worker_id": worker_id}
        if labels:
            body["labels"] = dict(labels)
        resp = self._request("POST", f"{self._base}/workers", json=body)
        data = resp.json()
        token = data.pop("registration_token", None)
        worker = Worker.model_validate(data)
        return (worker, token)

    def reissue_credential(self, worker_id: str) -> str:
        resp = self._request(
            "POST", f"{self._base}/workers/{worker_id}/reissue-credential"
        )
        data = resp.json()
        token = data.get("registration_token")
        if not isinstance(token, str):
            msg = "reissue_credential response missing registration_token"
            raise RuntimeError(msg)
        return token

    def verify_worker_credential(
        self, worker_id: str, registration_token: str
    ) -> bool:
        """Return ``True`` iff the bearer ``<worker_id>:<registration_token>`` authenticates.

        Implements the chapter 07 §6.4 / chapter 08 §9 Store-side
        contract over the wire. The §13 verifier is exposed as
        ``GET /whoami``: an authenticated probe that returns the
        bearer's principal worker_id on success and 401
        ``eden://error/unauthorized`` on a bad credential. The probe
        is issued directly through this client's configured transport
        (so an injected ``httpx.Client`` — mock transport, TLS
        config, proxy, etc. — applies to the verify call too); only
        the Authorization header is swapped for the candidate bearer
        on this single request.

        Returns ``True`` only when the returned worker_id equals
        ``worker_id`` (the chapter 02 §6.7 "wrong worker_id
        returned" recovery branch). Returns ``False`` ONLY for a
        confirmed-bad-credential outcome (HTTP 401). Every other
        unexpected outcome — HTTP failure, malformed response body,
        ``/whoami`` returning a different ``worker_id`` than the
        candidate, transport blip — propagates as an exception. The
        last case (``200`` but mismatched ``worker_id``) is a
        deployment misconfiguration (proxy mix-up, server bug, or
        the §6.7 recovery branch where the registry was rebuilt
        with the same id but a different identity behind it); the
        caller MUST treat it as "we don't know which worker this
        bearer actually authenticates as" rather than silently
        reissuing.
        """
        candidate_bearer = f"{worker_id}:{registration_token}"
        headers = {
            **self._headers,
            "Authorization": f"Bearer {candidate_bearer}",
        }
        resp = self._client.request(
            "GET", f"{self._base}/whoami", headers=headers
        )
        if resp.status_code == 401:
            return False
        resp.raise_for_status()
        returned = resp.json().get("worker_id")
        if returned == worker_id:
            return True
        # 200 OK but the bearer authenticates as somebody else — or
        # the body is malformed. Don't silently misclassify as
        # "credential is bad"; the caller must surface this.
        msg = (
            f"verify_worker_credential: /whoami returned worker_id="
            f"{returned!r} for bearer authenticating as {worker_id!r}; "
            f"this is the §6.7 recovery branch (registry rebuilt with "
            f"same id, different identity) or a proxy / server bug. "
            f"Surface to the operator rather than treating as a bad "
            f"credential."
        )
        raise RuntimeError(msg)

    def whoami(self) -> str:
        """Return the ``worker_id`` the bearer authenticates as (§6.4)."""
        resp = self._request("GET", f"{self._base}/whoami")
        return str(resp.json()["worker_id"])

    def read_worker(self, worker_id: str) -> Worker:
        resp = self._request("GET", f"{self._base}/workers/{worker_id}")
        return Worker.model_validate(resp.json())

    def list_workers(self) -> list[Worker]:
        resp = self._request("GET", f"{self._base}/workers")
        return [Worker.model_validate(w) for w in resp.json()["workers"]]

    def register_group(
        self,
        group_id: str,
        *,
        members: Iterable[str] | None = None,
        created_by: str | None = None,  # noqa: ARG002 — set by server-side principal
    ) -> Group:
        body: dict[str, Any] = {"group_id": group_id}
        if members is not None:
            body["members"] = list(members)
        resp = self._request("POST", f"{self._base}/groups", json=body)
        return Group.model_validate(resp.json())

    def add_to_group(self, group_id: str, member_id: str) -> Group:
        resp = self._request(
            "POST",
            f"{self._base}/groups/{group_id}/members",
            json={"member_id": member_id},
        )
        return Group.model_validate(resp.json())

    def remove_from_group(self, group_id: str, member_id: str) -> Group:
        resp = self._request(
            "DELETE",
            f"{self._base}/groups/{group_id}/members/{member_id}",
        )
        return Group.model_validate(resp.json())

    def delete_group(self, group_id: str) -> None:
        self._request("DELETE", f"{self._base}/groups/{group_id}")

    def read_group(self, group_id: str) -> Group:
        resp = self._request("GET", f"{self._base}/groups/{group_id}")
        return Group.model_validate(resp.json())

    def list_groups(self) -> list[Group]:
        resp = self._request("GET", f"{self._base}/groups")
        return [Group.model_validate(g) for g in resp.json()["groups"]]

    def resolve_worker_in_group(self, worker_id: str, group_id: str) -> bool:
        """Return ``True`` iff ``worker_id`` is a transitive member of ``group_id``.

        Walks the chapter 02 §7.2 transitive closure over the wire by
        repeated ``read_group`` calls. Cycle-safe by construction:
        the §7.3 cycle-detection at write-time guarantees the group
        DAG terminates, and this walk tracks visited groups
        defensively so a server that violated §7.3 cannot wedge the
        client.

        Per §7.1 "a reference to a non-existent worker / group
        resolves to membership=false": short-circuit if the candidate
        ``worker_id`` is not itself a registered worker. A
        non-existent group along the walk is caught explicitly so
        the walk continues (legitimate dangling references must not
        abort the search). Auth failures, transport errors, and
        other unexpected exceptions propagate so callers can
        distinguish "we don't know" from "confirmed not a member".
        """
        # §7.1: unregistered candidate cannot be a member.
        try:
            self.read_worker(worker_id)
        except NotFound:
            return False
        visited: set[str] = set()
        stack: list[str] = [group_id]
        while stack:
            current = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            try:
                group = self.read_group(current)
            except NotFound:
                # Dangling reference: §7.1 says a member name need not
                # exist at write time. Skip this node and keep walking.
                continue
            for member in group.members:
                if member == worker_id:
                    return True
                if member not in visited:
                    stack.append(member)
        return False

    # ------------------------------------------------------------------
    # Reassign / dispatch_mode (12a-2 wave 2 — wire endpoints land in wave 3)
    # ------------------------------------------------------------------

    def reassign_task(
        self,
        task_id: str,  # noqa: ARG002 — wave-3 plumbs through; signature is the Protocol contract
        new_target: TaskTarget | None,  # noqa: ARG002
        *,
        reason: str,  # noqa: ARG002
        reassigned_by: str,  # noqa: ARG002
    ) -> Task:
        """Wave-2 stub for protocol conformance — wire endpoint lands in wave 3.

        Spec: [`spec/v0/07-wire-protocol.md`](../../../../spec/v0/07-wire-protocol.md)
        §2.7. The HTTP binding (``POST .../tasks/{T}/reassign``) plus
        the read-back ladder for transport-indeterminate failures
        ship in wave 3; this stub keeps ``StoreClient`` structurally
        conformant with the Store Protocol so type-checking passes.
        """
        raise NotImplementedError(
            "reassign_task is wave-3 (eden_wire wire endpoint binding); "
            "wave-2 only adds the Store-side semantics. Use the in-process "
            "Store directly until wave 3 lands."
        )

    def read_dispatch_mode(self) -> DispatchMode:
        """Wave-2 stub; wire binding lands in wave 3 (chapter 07 §2.8)."""
        raise NotImplementedError(
            "read_dispatch_mode is wave-3; wave-2 only adds Store-side semantics."
        )

    def update_dispatch_mode(
        self,
        updates: DispatchMode | dict[str, str],  # noqa: ARG002
        *,
        updated_by: str,  # noqa: ARG002
    ) -> DispatchMode:
        """Wave-2 stub; wire binding lands in wave 3 (chapter 07 §2.8)."""
        raise NotImplementedError(
            "update_dispatch_mode is wave-3; wave-2 only adds Store-side semantics."
        )


def _submission_to_wire(submission: Submission) -> dict[str, Any]:
    if isinstance(submission, IdeaSubmission):
        return {
            "kind": "ideation",
            "status": submission.status,
            "idea_ids": list(submission.idea_ids),
        }
    if isinstance(submission, VariantSubmission):
        body: dict[str, Any] = {
            "kind": "execution",
            "status": submission.status,
            "variant_id": submission.variant_id,
        }
        if submission.commit_sha is not None:
            body["commit_sha"] = submission.commit_sha
        return body
    if isinstance(submission, EvaluationSubmission):
        body = {
            "kind": "evaluation",
            "status": submission.status,
            "variant_id": submission.variant_id,
        }
        if submission.evaluation is not None:
            body["evaluation"] = submission.evaluation
        if submission.artifacts_uri is not None:
            body["artifacts_uri"] = submission.artifacts_uri
        return body
    raise ValueError(f"unknown submission type: {type(submission).__name__}")


def _submission_from_wire(kind: str, payload: dict[str, Any]) -> Submission:
    if kind == "ideation":
        return IdeaSubmission(
            status=payload["status"],
            idea_ids=tuple(payload.get("idea_ids", ())),
        )
    if kind == "execution":
        return VariantSubmission(
            status=payload["status"],
            variant_id=payload["variant_id"],
            commit_sha=payload.get("commit_sha"),
        )
    if kind == "evaluation":
        return EvaluationSubmission(
            status=payload["status"],
            variant_id=payload["variant_id"],
            evaluation=payload.get("evaluation"),
            artifacts_uri=payload.get("artifacts_uri"),
        )
    raise ValueError(f"unknown submission kind: {kind!r}")


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _as_wire_datetime(value: datetime | str) -> str:
    if isinstance(value, datetime):
        from datetime import UTC

        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        else:
            value = value.astimezone(UTC)
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
    return value


@contextmanager
def store_client(
    base_url: str, experiment_id: str, **kwargs: Any
) -> Iterator[StoreClient]:
    """Context-manager convenience for :class:`StoreClient`."""
    client = StoreClient(base_url, experiment_id, **kwargs)
    try:
        yield client
    finally:
        client.close()
