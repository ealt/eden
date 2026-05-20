"""Thin httpx wrapper for the chapter-07 §15 control-plane endpoints.

Parallel to :class:`WireClient` but rooted at `/v0/control/`. Returns
raw `httpx.Response` so scenarios assert on status codes + problem+json
`type` strings — no exception-class shortcuts that would couple the
suite to a reference Python package (chapter 9 §6 makes the chapter-7
binding the only IUT contract; the suite MUST stay IUT-agnostic).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx


class ControlPlaneWireClient:
    """Direct chapter-07 §15 HTTP client used by control-plane scenarios."""

    def __init__(
        self,
        *,
        base_url: str,
        bearer: str | None = None,
        extra_headers: Mapping[str, str] | None = None,
        timeout: float = 30.0,
        observed_problem_types: set[str] | None = None,
    ) -> None:
        headers: dict[str, str] = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json, application/problem+json",
        }
        if bearer is not None:
            headers["Authorization"] = f"Bearer {bearer}"
        if extra_headers:
            # `extra_headers` comes from the IUT adapter's
            # `IutHandle.extra_headers` — auth-enabled IUTs use it
            # to pass session bearers / custom auth headers. Merge
            # AFTER the bearer so a header-based Authorization in
            # `extra_headers` takes precedence (matches WireClient's
            # auth-propagation posture).
            headers.update(extra_headers)
        self.base_url = base_url.rstrip("/")
        self._base = f"{self.base_url}/v0/control"
        self.observed_problem_types: set[str] = (
            observed_problem_types if observed_problem_types is not None else set()
        )
        self._client = httpx.Client(
            base_url=self.base_url,
            headers=headers,
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ControlPlaneWireClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Generic request plumbing
    # ------------------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: Mapping[str, str | int] | None = None,
    ) -> httpx.Response:
        kwargs: dict[str, Any] = {}
        if json is not None:
            kwargs["json"] = json
        if params is not None:
            kwargs["params"] = params
        resp = self._client.request(method, path, **kwargs)
        self._record_problem_type(resp)
        return resp

    def _record_problem_type(self, resp: httpx.Response) -> None:
        if resp.status_code >= 400 and "application/problem+json" in resp.headers.get(
            "content-type", ""
        ):
            try:
                body = resp.json()
            except ValueError:
                return
            type_uri = body.get("type")
            if isinstance(type_uri, str):
                self.observed_problem_types.add(type_uri)

    # ------------------------------------------------------------------
    # §15.1 experiment registry
    # ------------------------------------------------------------------

    def register_experiment(
        self, experiment_id: str, config_uri: str
    ) -> httpx.Response:
        return self.request(
            "POST",
            f"{self._base}/experiments",
            json={"experiment_id": experiment_id, "config_uri": config_uri},
        )

    def unregister_experiment(self, experiment_id: str) -> httpx.Response:
        return self.request(
            "DELETE", f"{self._base}/experiments/{experiment_id}"
        )

    def list_experiments(self) -> httpx.Response:
        return self.request("GET", f"{self._base}/experiments")

    def read_experiment_metadata(self, experiment_id: str) -> httpx.Response:
        return self.request("GET", f"{self._base}/experiments/{experiment_id}")

    # ------------------------------------------------------------------
    # §15.2 lease operations
    # ------------------------------------------------------------------

    def acquire_lease(
        self, experiment_id: str, holder: str, holder_instance: str
    ) -> httpx.Response:
        return self.request(
            "POST",
            f"{self._base}/experiments/{experiment_id}/leases",
            json={"holder": holder, "holder_instance": holder_instance},
        )

    def renew_lease(self, lease_id: str, holder_instance: str) -> httpx.Response:
        return self.request(
            "POST",
            f"{self._base}/leases/{lease_id}/renew",
            json={"holder_instance": holder_instance},
        )

    def release_lease(self, lease_id: str, holder_instance: str) -> httpx.Response:
        return self.request(
            "POST",
            f"{self._base}/leases/{lease_id}/release",
            json={"holder_instance": holder_instance},
        )

    def list_active_leases(self, holder: str) -> httpx.Response:
        return self.request(
            "GET", f"{self._base}/leases", params={"holder": holder}
        )

    # ------------------------------------------------------------------
    # §15.3 deployment-scoped worker / group registry
    # ------------------------------------------------------------------

    def register_worker(
        self,
        worker_id: str,
        *,
        labels: dict[str, str] | None = None,
    ) -> httpx.Response:
        body: dict[str, Any] = {"worker_id": worker_id}
        if labels is not None:
            body["labels"] = labels
        return self.request("POST", f"{self._base}/workers", json=body)

    def register_group(
        self,
        group_id: str,
        *,
        members: list[str] | None = None,
    ) -> httpx.Response:
        body: dict[str, Any] = {"group_id": group_id}
        if members is not None:
            body["members"] = members
        return self.request("POST", f"{self._base}/groups", json=body)

    def add_to_group(self, group_id: str, worker_id: str) -> httpx.Response:
        return self.request(
            "POST",
            f"{self._base}/groups/{group_id}/members",
            json={"worker_id": worker_id},
        )

    def remove_from_group(self, group_id: str, worker_id: str) -> httpx.Response:
        return self.request(
            "DELETE",
            f"{self._base}/groups/{group_id}/members/{worker_id}",
        )
