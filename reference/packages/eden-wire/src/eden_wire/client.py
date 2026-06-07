# slop-allow-file: F-4 eden-wire/client.py per-resource split deferred to issue #116

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

import io
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from eden_checkpoint import ExperimentIdConflict
from eden_contracts import (
    DispatchMode,
    EvaluationTask,
    Event,
    ExecutionTask,
    Experiment,
    ExperimentState,
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
    Submission,
    submission_from_payload,
    submission_to_payload,
)

from .errors import raise_for_envelope
from .models import DepositArtifactResponse

__all__ = [
    "IndeterminateDispatchModeUpdate",
    "IndeterminateImport",
    "IndeterminateIntegration",
    "IndeterminateReassign",
    "IndeterminateTermination",
    "StoreClient",
    "WhoamiResult",
]


@dataclass(frozen=True)
class WhoamiResult:
    """The identity returned by ``GET /v0/experiments/{E}/whoami`` (§6.4).

    Carries the opaque ``worker_id`` the bearer authenticates as plus the
    OPTIONAL operator-supplied display ``name`` the server echoes back
    (``None`` when the worker was registered without a name).
    """

    worker_id: str
    name: str | None = None


class IndeterminateImport(RuntimeError):
    """An ``import_checkpoint`` call's outcome cannot be determined.

    Raised by :meth:`StoreClient.import_checkpoint` when a transport-
    indeterminate failure cannot be resolved by the
    chapter-10 §10 ``read_experiment`` recovery probe (read-back
    itself fails, or the experiment does not exist). The caller MUST
    NOT assume the server has not committed; operator intervention is
    required.
    """


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


class IndeterminateReassign(RuntimeError):
    """A ``reassign_task`` call's outcome cannot be determined.

    Raised by :meth:`StoreClient.reassign_task` when a transport-
    indeterminate failure cannot be resolved by a read-back of the
    task (read-back itself fails, or shows a target that matches
    neither the prior nor the requested state). Operator intervention
    is required.
    """


class IndeterminateDispatchModeUpdate(RuntimeError):
    """An ``update_dispatch_mode`` call's outcome cannot be determined.

    Raised by :meth:`StoreClient.update_dispatch_mode` when a
    transport-indeterminate failure cannot be resolved by a read-back
    of the experiment's current ``dispatch_mode`` (read-back itself
    fails, or the observed state matches neither "before" nor "after"
    semantics). Operator intervention is required.
    """


class IndeterminateTermination(RuntimeError):
    """A ``terminate_experiment`` call's outcome cannot be determined.

    Raised by :meth:`StoreClient.terminate_experiment` when a
    transport-indeterminate failure cannot be resolved by a read-back
    of the experiment's lifecycle ``state`` (read-back itself fails,
    or the observed state remains ``"running"``). Note that the
    operation is idempotent on the terminated state per
    [`04-task-protocol.md`](../../../../spec/v0/04-task-protocol.md)
    §8.1, so an observed ``"terminated"`` state after a transport
    error is treated as confirmed success regardless of which call
    actually won the race.
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
        content: bytes | None = None,
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
            content=content,
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

    def create_execution_task(
        self,
        task_id: str,
        idea_id: str,
        *,
        target: TaskTarget | None = None,
    ) -> ExecutionTask:
        body: dict[str, Any] = {
            "task_id": task_id,
            "kind": "execution",
            "state": "pending",
            "created_at": _now(),
            "updated_at": _now(),
            "payload": {"idea_id": idea_id},
        }
        if target is not None:
            body["target"] = target.model_dump(mode="json", exclude_none=True)
        task = ExecutionTask.model_validate(body)
        created = self.create_task(task)
        assert isinstance(created, ExecutionTask)
        return created

    def create_evaluation_task(
        self,
        task_id: str,
        variant_id: str,
        *,
        target: TaskTarget | None = None,
    ) -> EvaluationTask:
        body: dict[str, Any] = {
            "task_id": task_id,
            "kind": "evaluation",
            "state": "pending",
            "created_at": _now(),
            "updated_at": _now(),
            "payload": {"variant_id": variant_id},
        }
        if target is not None:
            body["target"] = target.model_dump(mode="json", exclude_none=True)
        task = EvaluationTask.model_validate(body)
        created = self.create_task(task)
        assert isinstance(created, EvaluationTask)
        return created

    def _assert_bearer_matches_worker_id(self, worker_id: str) -> None:
        """Preflight that the call-supplied worker_id matches the bearer's principal.

        Per chapter 04 §3.3, authentication is a binding-layer concern
        and the §4.1 / §3.5 enforcement runs against the authenticated
        ``worker_id``. Without this client-side check, a caller
        passing a mismatched ``worker_id`` would be silently re-bound
        to the bearer's identity at the server. The check is a no-op
        when no bearer is set (auth-disabled / TestClient posture) or
        when the bearer principal is ``admin``: admin bearers cannot
        reach worker-gated routes — the §13.3 dispatcher 403s them —
        so any disagreement is rejected at the wire instead.
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
        # symmetry with the in-process Store contract; the wire layer
        # ignores it and uses the bearer's principal.
        self._assert_bearer_matches_worker_id(worker_id)
        body: dict[str, Any] = {}
        if expires_at is not None:
            body["expires_at"] = _as_wire_datetime(expires_at)
        resp = self._request(
            "POST",
            f"{self._base}/tasks/{task_id}/claim",
            json=body,
        )
        return TaskClaim.model_validate(resp.json())

    def submit(
        self, task_id: str, worker_id: str, submission: Submission
    ) -> None:
        # Per §2.4 + §13: server forwards the authenticated worker_id
        # to Store.submit; client passes the same identity for
        # symmetry with the in-process Store contract. The wire layer
        # derives the actor from the bearer's principal.
        self._assert_bearer_matches_worker_id(worker_id)
        payload = _submission_to_wire(submission)
        self._request(
            "POST",
            f"{self._base}/tasks/{task_id}/submit",
            json={"payload": payload},
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
    # Artifacts (chapter 7 §16, issue #166)
    # ------------------------------------------------------------------

    def deposit_artifact(
        self,
        data: bytes,
        *,
        filename: str = "artifact",
        content_type: str = "application/octet-stream",
    ) -> DepositArtifactResponse:
        """Deposit ``data`` and return the opaque ``artifacts_uri`` (§16.1).

        The bytes ride a single multipart ``file`` part; the server mints
        the opaque id, persists the bytes, records ``created_by`` from
        this client's bearer, and returns the resolvable URI. Unlike
        ``integrate_variant`` there is NO read-back ladder — a deposit
        carries no client-asserted identity to reconcile; a lost response
        just means re-deposit for a fresh id (§16.1).
        """
        # Encode the multipart body via a standalone httpx.Request (no
        # client defaults applied) so we capture the generated boundary
        # Content-Type and send it explicitly. A caller-injected
        # ``client=httpx.Client(headers={"Content-Type": "application/json"})``
        # would otherwise leave httpx unable to override that default for a
        # ``files=`` request, and the server would see JSON + no file part.
        encoded = httpx.Request(
            "POST",
            self._client.base_url.join(f"{self._base}/artifacts"),
            files={"file": (filename, io.BytesIO(data), content_type)},
        )
        resp = self._request(
            "POST",
            f"{self._base}/artifacts",
            content=encoded.read(),
            extra_headers={"Content-Type": encoded.headers["content-type"]},
        )
        return DepositArtifactResponse.model_validate(resp.json())

    def fetch_artifact(self, artifacts_uri: str) -> bytes:
        """Return the exact bytes for an artifact (§16.2).

        Takes the opaque ``artifacts_uri`` a deposit / idea / variant
        carries and presents it **verbatim** as the ``uri`` query parameter
        — the client never parses the opaque URI (§1.5); the issuing server
        maps it back to bytes. Raises the §9 wire error the server returned
        (``NotFound`` for an unknown uri, ``Forbidden`` for an ACL miss) via
        the shared problem+json reconstruction.
        """
        resp = self._request(
            "GET", f"{self._base}/artifacts", params={"uri": artifacts_uri}
        )
        return resp.content

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
        name: str | None = None,
        *,
        labels: dict[str, str] | None = None,
        registered_by: str | None = None,  # noqa: ARG002 — set by server-side principal
    ) -> tuple[Worker, str | None]:
        """Register a worker; the server mints the opaque ``worker_id``.

        The caller supplies only an optional display ``name`` + deployment
        ``labels``. The minted ``worker_id`` (and one-time
        ``registration_token``) come back in the response. ``registered_by``
        is stamped server-side from the authenticated principal; the
        parameter exists for ``Store``-Protocol signature parity.
        """
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
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

    def whoami(self) -> WhoamiResult:
        """Return the identity the bearer authenticates as (§6.4).

        Returns a :class:`WhoamiResult` carrying both the opaque
        ``worker_id`` and the OPTIONAL display ``name`` echoed by the
        server. Callers that only need the id read ``.worker_id``.
        """
        resp = self._request("GET", f"{self._base}/whoami")
        body = resp.json()
        return WhoamiResult(
            worker_id=str(body["worker_id"]),
            name=body.get("name"),
        )

    def read_worker(self, worker_id: str) -> Worker:
        resp = self._request("GET", f"{self._base}/workers/{worker_id}")
        return Worker.model_validate(resp.json())

    def list_workers(self, name: str | None = None) -> list[Worker]:
        """List workers, optionally filtered by exact display ``name`` (§6.2)."""
        params = {"name": name} if name is not None else None
        resp = self._request("GET", f"{self._base}/workers", params=params)
        return [Worker.model_validate(w) for w in resp.json()["workers"]]

    def register_group(
        self,
        name: str | None = None,
        *,
        members: Iterable[str] | None = None,
        created_by: str | None = None,  # noqa: ARG002 — set by server-side principal
        allow_reserved: bool = False,  # noqa: ARG002 — server derives this from the authenticated principal
    ) -> Group:
        """Register a group; the server mints the opaque ``group_id``.

        The caller supplies only an optional display ``name`` + initial
        ``members`` list (each an opaque ``wkr_*`` / ``grp_*`` id). The
        minted ``group_id`` comes back in the response. ``created_by`` is
        stamped server-side from the authenticated principal.

        ``allow_reserved`` exists for ``Store``-Protocol signature parity;
        over the wire the server derives the reserved-name allowance from
        the authenticated principal (admin), so the client cannot grant
        it by passing this flag.
        """
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
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

    def list_groups(self, name: str | None = None) -> list[Group]:
        """List groups, optionally filtered by exact display ``name`` (§7.2)."""
        params = {"name": name} if name is not None else None
        resp = self._request("GET", f"{self._base}/groups", params=params)
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
    # Reassign / dispatch_mode (12a-2 wave 3) — chapter 7 §§2.7-2.8
    # ------------------------------------------------------------------

    def reassign_task(
        self,
        task_id: str,
        new_target: TaskTarget | None,
        *,
        reason: str,
        reassigned_by: str,  # noqa: ARG002 — set server-side from principal
    ) -> Task:
        """Reassign a task's `target` over the wire (§2.7).

        Authority is enforced server-side: the bearer's worker_id MUST
        be a transitive member of the ``admins`` group; the server
        stamps ``reassigned_by`` on the emitted event from that
        authenticated identity, NOT from the ``reassigned_by``
        parameter here. The parameter exists to satisfy the
        ``Store`` Protocol signature; the in-process Store consumes
        it and the wire client lets the bearer be authoritative.

        On transport-indeterminate failure, runs a §2.7 / §6 read-back
        ladder parallel to ``integrate_variant`` (§5):

        - read-back shows ``task.target == new_target`` → success.
        - read-back shows a definitively-different target →
          :class:`IndeterminateReassign` (the operator must
          investigate; we can't tell whether OUR request committed
          and was subsequently overwritten, or never landed).
        - read-back itself fails →
          :class:`IndeterminateReassign`.
        """
        path = f"{self._base}/tasks/{task_id}/reassign"
        body: dict[str, Any] = {
            "new_target": (
                None
                if new_target is None
                else new_target.model_dump(mode="json", exclude_none=True)
            ),
            "reason": reason,
        }
        try:
            resp = self._request("POST", path, json=body)
            return TaskAdapter.validate_python(resp.json())
        except httpx.TransportError as exc:
            original = exc

        observed = self._try_read_task(task_id)
        if observed is None:
            raise IndeterminateReassign(
                f"reassign_task({task_id!r}) transport failed "
                f"({type(original).__name__}) and read-back could not be "
                f"completed; server-side outcome unknown"
            ) from original
        if _task_targets_equal(observed.target, new_target):
            return observed
        raise IndeterminateReassign(
            f"reassign_task({task_id!r}) transport failed "
            f"({type(original).__name__}); read-back shows target "
            f"{observed.target!r} which matches neither the requested "
            f"{new_target!r} nor reflects our intended write — operator "
            f"must investigate"
        ) from original

    def read_dispatch_mode(self) -> DispatchMode:
        """Fetch the experiment's current dispatch_mode (§2.8 read).

        Returns the full state — every normative key populated per
        ``02-data-model.md`` §2.5 defaults. Unknown keys persisted by
        older writes round-trip via ``DispatchMode``'s
        ``extra="allow"``.
        """
        resp = self._request("GET", f"{self._base}/dispatch_mode")
        return DispatchMode.model_validate(resp.json())

    def update_dispatch_mode(
        self,
        updates: DispatchMode | dict[str, str],
        *,
        updated_by: str,  # noqa: ARG002 — set server-side from principal
    ) -> DispatchMode:
        """Partial-merge update over the wire (§2.8).

        Authority enforced server-side (caller MUST be in
        ``admins``). The server stamps ``updated_by`` from the
        authenticated principal.

        On transport-indeterminate failure, runs a read-back ladder:
        re-fetch the dispatch_mode and compare to the requested
        ``updates``. If every requested key already holds the
        requested value, treat as confirmed success. Otherwise raise
        :class:`IndeterminateDispatchModeUpdate` — the server's
        outcome can't be determined (it may have applied the update
        and then someone else partially reverted, or our update never
        landed).
        """
        if isinstance(updates, DispatchMode):
            payload = updates.model_dump(mode="json", exclude_none=True)
        else:
            payload = dict(updates)
        path = f"{self._base}/dispatch_mode"
        try:
            resp = self._request("PATCH", path, json=payload)
            return DispatchMode.model_validate(resp.json())
        except httpx.TransportError as exc:
            original = exc

        observed = self._try_read_dispatch_mode()
        if observed is None:
            raise IndeterminateDispatchModeUpdate(
                f"update_dispatch_mode transport failed "
                f"({type(original).__name__}) and read-back could not be "
                f"completed; server-side outcome unknown"
            ) from original
        observed_dump = observed.model_dump(mode="json", exclude_none=True)
        if all(observed_dump.get(k) == v for k, v in payload.items()):
            return observed
        raise IndeterminateDispatchModeUpdate(
            f"update_dispatch_mode transport failed "
            f"({type(original).__name__}); read-back disagrees with the "
            f"requested update (observed={observed_dump!r}, requested="
            f"{payload!r}) — operator must investigate"
        ) from original

    # ------------------------------------------------------------------
    # Experiment lifecycle (12a-3) — chapter 7 §2.9
    # ------------------------------------------------------------------

    def read_experiment(self) -> Experiment:
        """Read the experiment runtime object via ``GET /v0/experiments/{E}``.

        Admin-gated server-side per chapter 7 §14.3. Returns the full
        :class:`Experiment` shape including ``imported_from``, which is
        the recovery-probe anchor for the
        chapter-10 §10 lost-import-response case. Worker-bearer callers
        who only need the lifecycle ``state`` projection should use
        :meth:`read_experiment_state` (either-auth ``GET /state``).
        """
        resp = self._request("GET", self._base)
        return Experiment.model_validate(resp.json())

    def read_experiment_state(self) -> ExperimentState:
        """Fetch the experiment's current lifecycle state (§2.9 read).

        Either-auth on the server (any registered worker MAY read).
        The endpoint returns ``{"state": "running"|"terminated"}``.
        """
        resp = self._request("GET", f"{self._base}/state")
        body = resp.json()
        state = body.get("state")
        if state not in ("running", "terminated"):
            raise RuntimeError(
                f"unexpected experiment state {state!r} in response body"
            )
        return state

    def update_experiment_state(self, new_state: ExperimentState) -> Experiment:
        """Not exposed on the wire — internal Store primitive only.

        Per [`04-task-protocol.md`](../../../../spec/v0/04-task-protocol.md)
        §8.3, ``update_experiment_state`` is the storage-layer primitive
        used by ``terminate_experiment`` and the orchestrator's
        policy-driven branch; it is NOT a wire endpoint in v0. Use
        :meth:`terminate_experiment` for the public lifecycle op.
        """
        raise NotImplementedError(
            "update_experiment_state is an internal Store primitive; "
            "not exposed as a wire endpoint per 04-task-protocol.md §8.3"
        )

    def emit_policy_error(
        self,
        *,
        policy_kind: str,
        error_type: str,
        error_message: str,
    ) -> None:
        """Append an ``experiment.policy_error`` event over the wire.

        Posts to ``POST /v0/experiments/{E}/policy-errors`` (the
        12a-3 wave-7 follow-up endpoint added to satisfy the
        ``03-roles.md`` §6.2 decision-type 0 fault-tolerance MUST
        through the wire-bound orchestrator service). Authority:
        ``orchestrators`` — caller's bearer MUST be a member of that
        group per chapter 07 §13.3. The route returns 204 on success.

        No read-back ladder is needed: the event is exempt from the
        ``05-event-protocol.md`` §2 transactional invariant (no state
        mutation pairs with it), so a transport-indeterminate failure
        is an at-most-once observability gap, not a state-correctness
        risk. The driver layer catches generic ``Exception`` and
        degrades to a structured log so a single failed emit cannot
        cascade into a stuck orchestrator iteration.
        """
        path = f"{self._base}/policy-errors"
        body = {
            "policy_kind": policy_kind,
            "error_type": error_type,
            "error_message": error_message,
        }
        resp = self._request("POST", path, json=body)
        resp.raise_for_status()

    def terminate_experiment(
        self, *, reason: str, terminated_by: str  # noqa: ARG002 — server stamps it
    ) -> Experiment:
        """Commit the ``running → terminated`` transition over the wire (§2.9).

        Group-gated server-side on ``admins`` OR ``orchestrators``
        (issue #256); the bearer's principal is the authoritative
        ``terminated_by``. The ``terminated_by`` parameter
        here exists to satisfy the ``Store`` Protocol signature; the
        wire body MUST NOT carry the field (the
        :class:`TerminateRequest` model rejects unknown keys), and the
        server stamps the recorded event from the authenticated
        principal.

        Idempotent on the terminated state per
        [`04-task-protocol.md`](../../../../spec/v0/04-task-protocol.md)
        §8.1 — a second call against an already-terminated experiment
        returns the recorded ``Experiment`` without committing a second
        transition.

        On transport-indeterminate failure, runs a read-back ladder
        parallel to ``integrate_variant`` (§5):

        - read-back shows ``state == "terminated"`` → confirmed success
          (idempotency means our call OR a racing call has won; either
          way the operator's intent is satisfied).
        - read-back shows ``state == "running"`` →
          :class:`IndeterminateTermination` (the server-side outcome
          can't be determined; our request may have committed but a
          subsequent resume — not in v0 — could have reverted, or our
          request never landed).
        - read-back itself fails →
          :class:`IndeterminateTermination`.
        """
        path = f"{self._base}/terminate"
        body: dict[str, Any] = {"reason": reason}
        try:
            resp = self._request("POST", path, json=body)
            return Experiment.model_validate(resp.json())
        except httpx.TransportError as exc:
            original = exc

        observed = self._try_read_experiment_state()
        if observed is None:
            raise IndeterminateTermination(
                f"terminate_experiment transport failed "
                f"({type(original).__name__}) and read-back of "
                f"experiment state could not be completed; server-side "
                f"outcome unknown"
            ) from original
        if observed == "terminated":
            # Idempotency wins: the server is in the requested
            # post-condition. We can't reconstruct the recorded
            # `created_at` without an explicit read-experiment endpoint,
            # so the read-back caller surfaces a synthetic Experiment
            # whose `created_at` is best-effort — the lifecycle field
            # is what callers care about here. The chapter-7 §2.9
            # binding returns the full Experiment on the happy path;
            # this fallback exists only for the indeterminate branch.
            return Experiment(
                experiment_id=self._experiment_id,
                state="terminated",
                created_at=_now(),
            )
        raise IndeterminateTermination(
            f"terminate_experiment transport failed "
            f"({type(original).__name__}); read-back shows "
            f"state={observed!r} — server-side outcome unknown, "
            "operator must investigate"
        ) from original

    # ------------------------------------------------------------------
    # Portable checkpoints (chapter 7 §14)
    # ------------------------------------------------------------------

    def export_checkpoint(
        self,
        stream: Any,
        *,
        experiment_config: str | bytes = "",  # noqa: ARG002 — server-side composes its own
        repo_bundle: bytes = b"",  # noqa: ARG002
        exporter_info: Any | None = None,  # noqa: ARG002
    ) -> Any:
        """Download a portable-checkpoint archive to ``stream``.

        Calls ``POST /v0/experiments/{E}/checkpoint`` and copies the
        response bytes into ``stream``. Admin-gated server-side per
        chapter 7 §14.1. Returns the parsed :class:`CheckpointManifest`
        recovered from the archive (mirrors the in-process Store-level
        return signature so callers don't branch by transport).

        The substrate-external parameters (``experiment_config``,
        ``repo_bundle``, ``exporter_info``) are accepted for Store-
        Protocol signature parity but are NOT forwarded — the server
        composes those from its own substrates. Wave-4 callers that
        want to customize them should use the in-process Store
        directly.
        """
        from eden_checkpoint import (  # local import — avoid cyclical at module-load
            CheckpointReader,
            extract_checkpoint,
        )

        url = f"{self._base}/checkpoint"
        with self._client.stream(
            "POST", url, headers=self._headers, timeout=self._timeout
        ) as resp:
            if 400 <= resp.status_code < 600:
                # Consume the response so the connection releases, then
                # raise.
                body = resp.read()
                try:
                    import json as _json

                    payload = _json.loads(body)
                except Exception:
                    payload = None
                if isinstance(payload, dict) and "type" in payload:
                    raise_for_envelope(payload)
                resp.raise_for_status()
            buf = io.BytesIO()
            for chunk in resp.iter_bytes():
                buf.write(chunk)
                stream.write(chunk)
        # Recover the manifest by re-reading the buffered bytes. The
        # operation is admin-only so the extra round-trip into a temp
        # dir is acceptable; production callers that own the bytes can
        # call extract_checkpoint themselves.
        import tempfile

        buf.seek(0)
        with tempfile.TemporaryDirectory(prefix="eden-checkpoint-export-") as td:
            reader: CheckpointReader = extract_checkpoint(buf, Path(td))
            return reader.manifest

    def import_checkpoint(
        self,
        stream: Any,
        *,
        as_experiment_id: str | None = None,
        extract_dir: Any | None = None,  # noqa: ARG002 — server owns extraction
    ) -> Any:
        """Upload a portable-checkpoint archive to the receiving server.

        Calls ``POST /v0/checkpoints/import`` with the archive bytes in
        the request body. Admin-gated per chapter 7 §14.2. The
        ``X-Eden-Experiment-Id`` header is OPTIONAL on this endpoint
        per the §1.3 carve-out; the client sends it so server-side
        defense-in-depth still applies, but the server will accept the
        request equally without it.

        Returns a dict with ``experiment_id`` and ``warnings`` (the
        wave-4 surface; full :class:`ImportResult` parity will land
        when the format library's substrate-external pieces flow
        through the wire — wave 5).

        Read-back ladder on transport-indeterminate failure: the
        server-side import is a single composite commit, so the client
        probes ``GET /v0/experiments/{target_id}`` and matches
        ``imported_from`` against a synthesized expectation. The probe
        path returns the experiment object including ``imported_from``
        per chapter 10 §10. Unlike ``terminate_experiment``, the import
        is NOT idempotent on the post-success state — a retry against
        an already-imported experiment raises ``ExperimentIdConflict``,
        which the client surfaces unchanged.
        """
        # Read all bytes — the FastAPI request handler does the same.
        # We need the bytes twice: once for the POST + once to parse the
        # manifest's exported_at for the recovery-probe ladder.
        if hasattr(stream, "read"):
            data = stream.read()
        else:
            data = bytes(stream)
        params: dict[str, Any] = {}
        if as_experiment_id is not None:
            params["as_experiment_id"] = as_experiment_id
        url = f"{self._base_url}/v0/checkpoints/import"
        # The §1.3 carve-out makes X-Eden-Experiment-Id optional on
        # this route; we omit our default header so the server's
        # carve-out logic exercises through the wave-5 conformance
        # suite too.
        headers = {
            k: v for k, v in self._headers.items() if k != "X-Eden-Experiment-Id"
        }
        try:
            resp = self._client.request(
                "POST",
                url,
                content=data,
                params=params,
                headers={**headers, "Content-Type": "application/x-eden-checkpoint+tar"},
                timeout=self._timeout,
            )
        except httpx.TransportError as exc:
            return self._import_recovery_probe(
                data, as_experiment_id=as_experiment_id, original=exc
            )
        if 400 <= resp.status_code < 600:
            body = self._maybe_json(resp)
            if isinstance(body, dict) and "type" in body:
                raise_for_envelope(body)
            resp.raise_for_status()
        return resp.json()

    def _import_recovery_probe(
        self,
        archive_bytes: bytes,
        *,
        as_experiment_id: str | None,
        original: BaseException,
    ) -> dict[str, Any]:
        """Read-back ladder for ``import_checkpoint`` transport-indeterminacy.

        Per chapter 10 §10, a client whose import call lost its 201
        response probes ``read_experiment(target_id)`` and compares
        ``imported_from.checkpoint_exported_at`` against the manifest's
        ``exported_at``. Three outcomes:

        1. **Confirmed success.** The receiving experiment exists with
           ``imported_from.checkpoint_exported_at`` matching the local
           manifest's ``exported_at``: the import already committed;
           the missing 201 was a transport blip. Return a synthesized
           response.
        2. **Confirmed divergence.** The receiving experiment exists
           but ``imported_from`` is absent or mismatched: a different
           import won the race (or the receiver was non-empty before
           our call). Raise the ``ExperimentIdConflict`` the server
           would have returned.
        3. **Indeterminate.** The read-back itself fails (transport
           error, NotFound on the target id): we cannot determine
           whether our POST landed. Raise :class:`IndeterminateImport`;
           operator intervention is required.
        """
        target_id, source_experiment_id, local_exported_at, local_format_version = (
            self._parse_recovery_manifest(
                archive_bytes,
                as_experiment_id=as_experiment_id,
                original=original,
            )
        )
        body = self._fetch_recovery_probe(target_id, original=original)
        imported = body.get("imported_from")
        if (
            isinstance(imported, dict)
            and imported.get("source_experiment_id") == source_experiment_id
            and imported.get("checkpoint_exported_at") == local_exported_at
            and imported.get("checkpoint_format_version") == local_format_version
        ):
            # Confirmed success: our request landed; the 201 was lost.
            return {
                "experiment_id": target_id,
                "warnings": ["recovered from transport-indeterminate import"],
            }
        # The experiment exists but with no matching provenance — either
        # `imported_from is None` (native) or a different import won.
        # Surface as ExperimentIdConflict to match the server-side
        # response shape when a second client tries to claim the same id.
        raise ExperimentIdConflict(
            f"import_checkpoint transport failed "
            f"({type(original).__name__}); read-back of experiment "
            f"{target_id!r} shows it exists with non-matching "
            f"imported_from={imported!r} (expected exported_at="
            f"{local_exported_at!r}). The target experiment id is taken "
            "by a different import."
        ) from original

    def _parse_recovery_manifest(
        self,
        archive_bytes: bytes,
        *,
        as_experiment_id: str | None,
        original: BaseException,
    ) -> tuple[str, str, str, str]:
        """Stream-walk the archive's tar entries to read ``manifest.json``.

        Codex round-2 finding: don't extract the whole archive just to
        read manifest.json — for large checkpoints this doubles local
        disk/I/O on the very path that's supposed to harden a dropped-
        response recovery.

        Returns ``(probe_target_id, source_experiment_id, exported_at,
        format_version)`` where ``probe_target_id`` is the receiver's own
        experiment id (where a #128 import lands) and
        ``source_experiment_id`` is the manifest's id (matched against the
        receiver's ``imported_from.source_experiment_id``). Raises
        :class:`IndeterminateImport` from ``original`` when the archive
        is unparseable.
        """
        import io as _io
        import tarfile as _tarfile

        from eden_checkpoint import CheckpointManifest

        try:
            buf = _io.BytesIO(archive_bytes)
            manifest_bytes: bytes | None = None
            with _tarfile.open(fileobj=buf, mode="r|") as tar:
                for member in tar:
                    # Match `*/manifest.json` at archive root (the
                    # checkpoint format's single-top-level convention).
                    if member.name.endswith("/manifest.json") and member.isfile():
                        extracted = tar.extractfile(member)
                        if extracted is not None:
                            manifest_bytes = extracted.read()
                        break
            if manifest_bytes is None:
                raise ValueError("manifest.json not found in archive")
            manifest_obj = CheckpointManifest.model_validate_json(manifest_bytes)
        except Exception as parse_exc:
            # If we can't even parse the archive, we can't probe. Treat
            # as indeterminate — the caller's archive may have been
            # corrupted at the same moment the transport failed.
            raise IndeterminateImport(
                f"import_checkpoint transport failed "
                f"({type(original).__name__}) and the local archive "
                f"could not be parsed for recovery-probe: {parse_exc}"
            ) from original
        # Post-#128: an unkeyed import lands under the RECEIVER's own
        # minted experiment_id (this StoreClient's experiment), NOT the
        # source manifest id — the source id survives only as
        # ``imported_from.source_experiment_id`` for provenance
        # (``10-checkpoints.md`` §10, ``07-wire-protocol.md`` §14.2). So the
        # probe targets ``self._experiment_id`` and confirms the landing by
        # matching the source id + exported_at. ``as_experiment_id``, when
        # supplied, must equal ``self._experiment_id`` (the only experiment
        # this server serves), so it is not the probe target.
        return (
            self._experiment_id,
            manifest_obj.experiment_id,
            manifest_obj.exported_at,
            manifest_obj.checkpoint_format_version,
        )

    def _fetch_recovery_probe(
        self, target_id: str, *, original: BaseException
    ) -> dict[str, Any]:
        """GET ``/v0/experiments/{target_id}`` for the recovery probe.

        Bypasses the StoreClient's default base path (which is scoped
        to the StoreClient's own experiment_id, not the target_id) and
        hits the absolute path. Raises :class:`IndeterminateImport`
        from ``original`` on transport error, 404, or envelope-wrapped
        StorageError; otherwise returns the parsed JSON body.
        """
        from eden_storage.errors import (
            AlreadyExists,
            InvalidPrecondition,
        )
        from eden_storage.errors import (
            NotFound as _NotFound,
        )

        probe_url = f"{self._base_url}/v0/experiments/{target_id}"
        probe_headers = {
            k: v for k, v in self._headers.items() if k != "X-Eden-Experiment-Id"
        }
        probe_headers["X-Eden-Experiment-Id"] = target_id
        try:
            probe = self._client.request(
                "GET", probe_url, headers=probe_headers, timeout=self._timeout
            )
        except httpx.TransportError:
            raise IndeterminateImport(
                f"import_checkpoint transport failed "
                f"({type(original).__name__}) and the recovery-probe "
                f"GET /v0/experiments/{target_id} also failed; "
                "server-side outcome unknown"
            ) from original
        if probe.status_code == 404:
            raise IndeterminateImport(
                f"import_checkpoint transport failed "
                f"({type(original).__name__}) and the recovery-probe "
                f"shows experiment {target_id!r} does not exist on the "
                "receiver; server-side outcome unknown (the request may "
                "still be in flight)"
            ) from original
        if 400 <= probe.status_code < 600:
            body = self._maybe_json(probe)
            if isinstance(body, dict) and "type" in body:
                try:
                    raise_for_envelope(body)
                except (AlreadyExists, InvalidPrecondition, _NotFound) as e:
                    raise IndeterminateImport(
                        f"import_checkpoint transport failed "
                        f"({type(original).__name__}); read-back probe "
                        f"surfaced {type(e).__name__}: {e}"
                    ) from original
            probe.raise_for_status()
        return probe.json()

    def _try_read_task(self, task_id: str) -> Task | None:
        for _ in range(self._read_back_attempts):
            try:
                return self.read_task(task_id)
            except Exception:
                continue
        return None

    def _try_read_dispatch_mode(self) -> DispatchMode | None:
        for _ in range(self._read_back_attempts):
            try:
                return self.read_dispatch_mode()
            except Exception:
                continue
        return None

    def _try_read_experiment_state(self) -> ExperimentState | None:
        for _ in range(self._read_back_attempts):
            try:
                return self.read_experiment_state()
            except Exception:
                continue
        return None


def _task_targets_equal(a: TaskTarget | None, b: TaskTarget | None) -> bool:
    """Compare two ``Task.target`` values structurally for the reassign read-back ladder."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return a.kind == b.kind and a.id == b.id


def _submission_to_wire(submission: Submission) -> dict[str, Any]:
    kind, payload = submission_to_payload(submission)
    return {"kind": kind, **payload}


def _submission_from_wire(kind: str, payload: dict[str, Any]) -> Submission:
    return submission_from_payload(kind, payload)


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
