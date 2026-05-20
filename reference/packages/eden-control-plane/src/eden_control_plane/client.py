"""`ControlPlaneClient` — HTTP client for the chapter 07 §15 endpoints.

The client wraps `httpx` over the 19 normative endpoints rooted at
`/v0/control/`. Mirrors `StoreClient`'s shape from `eden_wire.client`
where convenient, with the differences enforced by chapter 11:

- The endpoint root is `/v0/control/`, not `/v0/experiments/{E}/`.
- Authentication uses a deployment-scoped credential (per chapter 11
  §6) or the deployment admin token — the two principals match
  chapter 07 §13's `admin` / `<worker_id>` bearer grammar.
- The four chapter 11 §4.5 lease error codes are routed to typed
  exceptions in `eden_control_plane.errors`.

The orchestrator's startup flow (chapter 11 §5.2) will compose this
client with `LeaseManager` in wave 4.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import httpx
from eden_contracts import Group, Worker

from .errors import raise_for_control_plane_envelope
from .models import (
    ExperimentLease,
    LeaseAcquireRequest,
    LeaseReleaseRequest,
    LeaseRenewRequest,
    ListExperimentsResponse,
    ListLeasesResponse,
    RegisteredExperiment,
    RegisterExperimentRequest,
)

__all__ = ["ControlPlaneClient"]


class ControlPlaneClient:
    """HTTP client for the chapter 07 §15 control-plane endpoints."""

    def __init__(
        self,
        base_url: str,
        *,
        bearer: str | None = None,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        """Construct a control-plane client.

        Authentication: `bearer` is the §13 bearer in
        ``"<principal>:<secret>"`` form. For control-plane operations
        the principal is either the deployment admin
        (``"admin:<admin_token>"``) or a deployment-scoped worker
        (``"<worker_id>:<registration_token>"``) per chapter 11 §6.
        Per-op authority gates live on the server; the client does
        not pre-classify calls by principal class.
        """
        self._base_url = base_url.rstrip("/")
        self._base = f"{self._base_url}/v0/control"
        self._headers: dict[str, str] = {}
        self._bearer = bearer
        if bearer is not None:
            self._headers["Authorization"] = f"Bearer {bearer}"
        self._owns_client = client is None
        self._timeout = timeout
        self._client = client if client is not None else httpx.Client(timeout=timeout)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying httpx.Client if owned by this instance."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> ControlPlaneClient:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    @property
    def base_url(self) -> str:
        """The deployment-level base URL the client targets (no `/v0/control` suffix)."""
        return self._base_url

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
                raise_for_control_plane_envelope(body)
            resp.raise_for_status()
        return resp

    @staticmethod
    def _maybe_json(resp: httpx.Response) -> Any:
        try:
            return resp.json()
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Experiment registry (chapter 07 §15.1, chapter 11 §2.2)
    # ------------------------------------------------------------------

    def register_experiment(
        self,
        experiment_id: str,
        config_uri: str,
    ) -> RegisteredExperiment:
        """`POST /v0/control/experiments`.

        Admin-gated. Idempotent on (experiment_id, config_uri); a
        differing config_uri raises `AlreadyExists`.
        """
        body = RegisterExperimentRequest(
            experiment_id=experiment_id, config_uri=config_uri
        )
        resp = self._request(
            "POST",
            f"{self._base}/experiments",
            json=body.model_dump(mode="json"),
        )
        return RegisteredExperiment.model_validate(resp.json())

    def unregister_experiment(self, experiment_id: str) -> None:
        """`DELETE /v0/control/experiments/{E}`.

        Admin-gated. Raises `InvalidPrecondition` when the experiment
        is still `running` OR an active lease exists.
        """
        self._request("DELETE", f"{self._base}/experiments/{experiment_id}")

    def list_experiments(self) -> list[RegisteredExperiment]:
        """`GET /v0/control/experiments`."""
        resp = self._request("GET", f"{self._base}/experiments")
        return ListExperimentsResponse.model_validate(resp.json()).experiments

    def read_experiment_metadata(self, experiment_id: str) -> RegisteredExperiment:
        """`GET /v0/control/experiments/{E}`."""
        resp = self._request("GET", f"{self._base}/experiments/{experiment_id}")
        return RegisteredExperiment.model_validate(resp.json())

    # ------------------------------------------------------------------
    # Lease operations (chapter 07 §15.2, chapter 11 §4.5)
    # ------------------------------------------------------------------

    def acquire_lease(
        self,
        experiment_id: str,
        holder: str,
        holder_instance: str,
    ) -> ExperimentLease:
        """`POST /v0/control/experiments/{E}/leases`.

        Raises `LeaseHeldByOther` on 409. The caller MUST already be
        authenticated as `holder` (the server rejects impersonation
        with 403 `forbidden`).
        """
        body = LeaseAcquireRequest(holder=holder, holder_instance=holder_instance)
        resp = self._request(
            "POST",
            f"{self._base}/experiments/{experiment_id}/leases",
            json=body.model_dump(mode="json"),
        )
        return ExperimentLease.model_validate(resp.json())

    def renew_lease(self, lease_id: str, holder_instance: str) -> ExperimentLease:
        """`POST /v0/control/leases/{L}/renew`.

        Raises `LeaseNotHeld` (410, replacement happened),
        `LeaseExpired` (410, lapsed but not yet replaced), or
        `LeaseInstanceMismatch` (409) per chapter 11 §4.5.
        """
        body = LeaseRenewRequest(holder_instance=holder_instance)
        resp = self._request(
            "POST",
            f"{self._base}/leases/{lease_id}/renew",
            json=body.model_dump(mode="json"),
        )
        return ExperimentLease.model_validate(resp.json())

    def release_lease(self, lease_id: str, holder_instance: str) -> None:
        """`POST /v0/control/leases/{L}/release`.

        Idempotent on already-released lease (returns 200 with no
        state change). Raises `LeaseInstanceMismatch` (409) on
        `holder_instance` mismatch.
        """
        body = LeaseReleaseRequest(holder_instance=holder_instance)
        self._request(
            "POST",
            f"{self._base}/leases/{lease_id}/release",
            json=body.model_dump(mode="json"),
        )

    def list_active_leases(self, holder: str) -> list[ExperimentLease]:
        """`GET /v0/control/leases?holder=<id>`.

        Used by the orchestrator's chapter 11 §5.2 startup-fence
        probe. Returns every ACTIVE lease (`expires_at >= now`) whose
        `holder` matches the argument; the caller MUST authenticate
        as `holder` OR as the admin principal.
        """
        resp = self._request(
            "GET",
            f"{self._base}/leases",
            params={"holder": holder},
        )
        return ListLeasesResponse.model_validate(resp.json()).leases

    # ------------------------------------------------------------------
    # Deployment-scoped worker registry (chapter 07 §15.3, chapter 11 §6)
    # ------------------------------------------------------------------

    def register_worker(
        self,
        worker_id: str,
        *,
        labels: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """`POST /v0/control/workers`.

        Mints a registration token on first registration; idempotent
        on repeat (returns the existing record without a new token).
        Returns the raw JSON because the response shape includes the
        one-time-emitted `registration_token` per `02-data-model.md`
        §6 — the protocol does not surface it on subsequent reads,
        so we don't model it as a Worker.
        """
        body: dict[str, Any] = {"worker_id": worker_id}
        if labels is not None:
            body["labels"] = labels
        resp = self._request("POST", f"{self._base}/workers", json=body)
        return resp.json()

    def reissue_credential(self, worker_id: str) -> dict[str, Any]:
        """`POST /v0/control/workers/{W}/reissue-credential`.

        Returns the raw JSON to surface the new `registration_token`.
        """
        resp = self._request(
            "POST", f"{self._base}/workers/{worker_id}/reissue-credential"
        )
        return resp.json()

    def list_workers(self) -> list[Worker]:
        """`GET /v0/control/workers`. Admin-gated."""
        resp = self._request("GET", f"{self._base}/workers")
        return [Worker.model_validate(w) for w in resp.json()["workers"]]

    def read_worker(self, worker_id: str) -> Worker:
        """`GET /v0/control/workers/{W}`. Admin-gated."""
        resp = self._request("GET", f"{self._base}/workers/{worker_id}")
        return Worker.model_validate(resp.json())

    def whoami(self) -> str:
        """`GET /v0/control/whoami`. Returns the authenticated worker_id."""
        resp = self._request("GET", f"{self._base}/whoami")
        return resp.json()["worker_id"]

    # ------------------------------------------------------------------
    # Deployment-scoped group registry (chapter 07 §15.3, chapter 11 §6)
    # ------------------------------------------------------------------

    def register_group(
        self,
        group_id: str,
        *,
        members: Iterable[str] | None = None,
    ) -> Group:
        """`POST /v0/control/groups`. Admin-gated."""
        body: dict[str, Any] = {"group_id": group_id}
        if members is not None:
            body["members"] = list(members)
        resp = self._request("POST", f"{self._base}/groups", json=body)
        return Group.model_validate(resp.json())

    def add_to_group(self, group_id: str, worker_id: str) -> Group:
        """`POST /v0/control/groups/{G}/members`. Admin-gated."""
        resp = self._request(
            "POST",
            f"{self._base}/groups/{group_id}/members",
            json={"worker_id": worker_id},
        )
        return Group.model_validate(resp.json())

    def remove_from_group(self, group_id: str, worker_id: str) -> Group:
        """`DELETE /v0/control/groups/{G}/members/{W}`. Admin-gated."""
        resp = self._request(
            "DELETE",
            f"{self._base}/groups/{group_id}/members/{worker_id}",
        )
        return Group.model_validate(resp.json())

    def delete_group(self, group_id: str) -> None:
        """`DELETE /v0/control/groups/{G}`. Admin-gated."""
        self._request("DELETE", f"{self._base}/groups/{group_id}")

    def list_groups(self) -> list[Group]:
        """`GET /v0/control/groups`. Admin-gated."""
        resp = self._request("GET", f"{self._base}/groups")
        return [Group.model_validate(g) for g in resp.json()["groups"]]

    def read_group(self, group_id: str) -> Group:
        """`GET /v0/control/groups/{G}`. Admin-gated."""
        resp = self._request("GET", f"{self._base}/groups/{group_id}")
        return Group.model_validate(resp.json())
