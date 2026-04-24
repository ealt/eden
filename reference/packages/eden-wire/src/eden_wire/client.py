"""``StoreClient`` — a ``Store``-Protocol-compatible HTTP client.

``StoreClient`` makes the EDEN wire binding look exactly like a
direct ``Store`` to callers. The dispatch driver, integrator, and
conformance scenarios all work against it unchanged: structural
Protocol conformance means "talks to a store" doesn't commit to a
transport.

Transport-indeterminate reconciliation on ``integrate_trial``
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
    EvaluateTask,
    Event,
    FailReason,
    ImplementTask,
    PlanTask,
    Proposal,
    ReclaimCause,
    Task,
    TaskAdapter,
    TaskClaim,
    Trial,
)
from eden_storage.errors import InvalidPrecondition
from eden_storage.submissions import (
    EvaluateSubmission,
    ImplementSubmission,
    PlanSubmission,
    Submission,
)

from .errors import raise_for_envelope

__all__ = ["IndeterminateIntegration", "StoreClient"]


class IndeterminateIntegration(RuntimeError):
    """An ``integrate_trial`` call's outcome cannot be determined.

    Raised by :meth:`StoreClient.integrate_trial` when a transport-
    indeterminate failure cannot be resolved by a read-back of the
    trial (read-back itself fails, or shows no
    ``trial_commit_sha``). The caller (typically
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

    def read_proposal(self, proposal_id: str) -> Proposal:
        resp = self._request("GET", f"{self._base}/proposals/{proposal_id}")
        return Proposal.model_validate(resp.json())

    def read_trial(self, trial_id: str) -> Trial:
        resp = self._request("GET", f"{self._base}/trials/{trial_id}")
        return Trial.model_validate(resp.json())

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

    def list_proposals(self, *, state: str | None = None) -> list[Proposal]:
        params = {"state": state} if state is not None else None
        resp = self._request("GET", f"{self._base}/proposals", params=params)
        return [Proposal.model_validate(item) for item in resp.json()]

    def list_trials(self, *, status: str | None = None) -> list[Trial]:
        params = {"status": status} if status is not None else None
        resp = self._request("GET", f"{self._base}/trials", params=params)
        return [Trial.model_validate(item) for item in resp.json()]

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

    def create_plan_task(self, task_id: str) -> PlanTask:
        task = PlanTask.model_validate(
            {
                "task_id": task_id,
                "kind": "plan",
                "state": "pending",
                "created_at": _now(),
                "updated_at": _now(),
                "payload": {"experiment_id": self._experiment_id},
            }
        )
        created = self.create_task(task)
        assert isinstance(created, PlanTask)
        return created

    def create_implement_task(self, task_id: str, proposal_id: str) -> ImplementTask:
        task = ImplementTask.model_validate(
            {
                "task_id": task_id,
                "kind": "implement",
                "state": "pending",
                "created_at": _now(),
                "updated_at": _now(),
                "payload": {"proposal_id": proposal_id},
            }
        )
        created = self.create_task(task)
        assert isinstance(created, ImplementTask)
        return created

    def create_evaluate_task(self, task_id: str, trial_id: str) -> EvaluateTask:
        task = EvaluateTask.model_validate(
            {
                "task_id": task_id,
                "kind": "evaluate",
                "state": "pending",
                "created_at": _now(),
                "updated_at": _now(),
                "payload": {"trial_id": trial_id},
            }
        )
        created = self.create_task(task)
        assert isinstance(created, EvaluateTask)
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
    # Proposals / trials
    # ------------------------------------------------------------------

    def create_proposal(self, proposal: Proposal) -> None:
        self._request(
            "POST",
            f"{self._base}/proposals",
            json=proposal.model_dump(mode="json", exclude_none=True),
        )

    def mark_proposal_ready(self, proposal_id: str) -> None:
        self._request("POST", f"{self._base}/proposals/{proposal_id}/mark-ready")

    def create_trial(self, trial: Trial) -> None:
        self._request(
            "POST",
            f"{self._base}/trials",
            json=trial.model_dump(mode="json", exclude_none=True),
        )

    def declare_trial_eval_error(self, trial_id: str) -> None:
        self._request("POST", f"{self._base}/trials/{trial_id}/declare-eval-error")

    def integrate_trial(self, trial_id: str, trial_commit_sha: str) -> None:
        """Integrator promotion with transport-indeterminate reconciliation.

        Implements the §5 three-outcome rule:

        - observed SHA == expected → success.
        - observed SHA != expected (and not None) → ``InvalidPrecondition``
          (same exception the server-side same-value idempotency
          divergence branch raises directly, so ``Integrator`` can
          distinguish it from other failures).
        - observed SHA absent, or read-back fails →
          ``IndeterminateIntegration``.
        """
        path = f"{self._base}/trials/{trial_id}/integrate"
        try:
            self._request("POST", path, json={"trial_commit_sha": trial_commit_sha})
            return
        except httpx.TransportError as exc:
            # httpx.ReadTimeout is a subclass of TransportError, so
            # this single clause catches all indeterminate transports.
            original = exc

        trial = self._try_read_trial(trial_id)
        if trial is None:
            raise IndeterminateIntegration(
                f"integrate_trial({trial_id!r}) transport failed "
                f"({type(original).__name__}) and read-back could not be "
                f"completed; server-side outcome unknown"
            ) from original
        observed = trial.trial_commit_sha
        if observed == trial_commit_sha:
            return  # confirmed success
        if observed is not None:
            raise InvalidPrecondition(
                f"trial {trial_id!r} is already integrated with a different "
                f"trial_commit_sha ({observed!r} != {trial_commit_sha!r})"
            ) from original
        raise IndeterminateIntegration(
            f"integrate_trial({trial_id!r}) transport failed "
            f"({type(original).__name__}); read-back shows no "
            f"trial_commit_sha, but the original request may still be "
            f"in flight — compensation is unsafe"
        ) from original

    def _try_read_trial(self, trial_id: str) -> Trial | None:
        for _ in range(self._read_back_attempts):
            try:
                return self.read_trial(trial_id)
            except Exception:
                continue
        return None

    # ------------------------------------------------------------------
    # Shared validators
    # ------------------------------------------------------------------

    def validate_metrics(self, metrics: dict[str, Any]) -> None:
        self._request(
            "POST",
            f"{self._ref_base}/validate/metrics",
            json={"metrics": metrics},
        )


def _submission_to_wire(submission: Submission) -> dict[str, Any]:
    if isinstance(submission, PlanSubmission):
        return {
            "kind": "plan",
            "status": submission.status,
            "proposal_ids": list(submission.proposal_ids),
        }
    if isinstance(submission, ImplementSubmission):
        body: dict[str, Any] = {
            "kind": "implement",
            "status": submission.status,
            "trial_id": submission.trial_id,
        }
        if submission.commit_sha is not None:
            body["commit_sha"] = submission.commit_sha
        return body
    if isinstance(submission, EvaluateSubmission):
        body = {
            "kind": "evaluate",
            "status": submission.status,
            "trial_id": submission.trial_id,
        }
        if submission.metrics is not None:
            body["metrics"] = submission.metrics
        if submission.artifacts_uri is not None:
            body["artifacts_uri"] = submission.artifacts_uri
        return body
    raise ValueError(f"unknown submission type: {type(submission).__name__}")


def _submission_from_wire(kind: str, payload: dict[str, Any]) -> Submission:
    if kind == "plan":
        return PlanSubmission(
            status=payload["status"],
            proposal_ids=tuple(payload.get("proposal_ids", ())),
        )
    if kind == "implement":
        return ImplementSubmission(
            status=payload["status"],
            trial_id=payload["trial_id"],
            commit_sha=payload.get("commit_sha"),
        )
    if kind == "evaluate":
        return EvaluateSubmission(
            status=payload["status"],
            trial_id=payload["trial_id"],
            metrics=payload.get("metrics"),
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
