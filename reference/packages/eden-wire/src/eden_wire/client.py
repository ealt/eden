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

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

import httpx
from eden_contracts import (
    EvaluationTask,
    Event,
    ExecutionTask,
    FailReason,
    Idea,
    IdeationTask,
    ReclaimCause,
    Task,
    TaskAdapter,
    TaskClaim,
    Variant,
)
from eden_storage.errors import InvalidPrecondition
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
        token: str | None = None,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
        read_back_attempts: int = 3,
    ) -> None:
        self._experiment_id = experiment_id
        self._base = f"{base_url.rstrip('/')}/v0/experiments/{experiment_id}"
        self._ref_base = f"{base_url.rstrip('/')}/_reference/experiments/{experiment_id}"
        self._headers: dict[str, str] = {"X-Eden-Experiment-Id": experiment_id}
        if token is not None:
            self._headers["Authorization"] = f"Bearer {token}"
        self._owns_client = client is None
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
    ) -> httpx.Response:
        resp = self._client.request(
            method,
            path,
            params=params,
            json=json,
            headers=self._headers,
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

    def claim(
        self,
        task_id: str,
        worker_id: str,
        *,
        expires_at: datetime | str | None = None,
    ) -> TaskClaim:
        body: dict[str, Any] = {"worker_id": worker_id}
        if expires_at is not None:
            body["expires_at"] = _as_wire_datetime(expires_at)
        resp = self._request("POST", f"{self._base}/tasks/{task_id}/claim", json=body)
        return TaskClaim.model_validate(resp.json())

    def submit(self, task_id: str, token: str, submission: Submission) -> None:
        payload = _submission_to_wire(submission)
        self._request(
            "POST",
            f"{self._base}/tasks/{task_id}/submit",
            json={"token": token, "payload": payload},
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
