"""Thin httpx wrapper for the chapter-07 §15 control-plane endpoints.

Parallel to :class:`WireClient` but rooted at `/v0/control/`. Returns
raw `httpx.Response` so scenarios assert on status codes + problem+json
`type` strings — no exception-class shortcuts that would couple the
suite to a reference Python package (chapter 9 §6 makes the chapter-7
binding the only IUT contract; the suite MUST stay IUT-agnostic).

Identity rename (#128): after the rename the control-plane registries
mint opaque ids. `register_experiment` mints an `exp_*`,
`register_worker` mints a `wkr_*`, `register_group` mints a `grp_*`
([`spec/v0/02-data-model.md`] §1.6); the caller supplies only an
optional display `name` ([`02-data-model.md`] §1.7). Scenarios still
want to refer to an entity by a stable human handle, so this client
mirrors :class:`WireClient`'s name<->minted-id registry pattern: each
``register_*`` call records the minted id under the display name it
was created with, and the ``*_id_for`` resolvers map a handle to the
minted opaque id (returning the argument unchanged when it is unknown,
so deliberate "never-registered" probes still flow the literal
through). Lease / read ops resolve their experiment / holder / member
arguments through those registries before building the wire payload.
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
        # Identity registries (display name -> minted opaque id). Since
        # the identity rename (#128) the control plane mints `exp_*` /
        # `wkr_*` / `grp_*` ids; scenarios refer to entities by a stable
        # display handle and resolve to the minted id via the helpers
        # below when building wire payloads (lease `holder`, member
        # refs, experiment-scoped paths).
        self._experiment_id_by_name: dict[str, str] = {}
        self._worker_id_by_name: dict[str, str] = {}
        self._group_id_by_name: dict[str, str] = {}
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
    # Identity registry (name <-> minted opaque id)
    # ------------------------------------------------------------------

    def experiment_id_for(self, name: str) -> str:
        """Resolve an experiment display name to its minted ``exp_*`` id.

        Returns ``name`` unchanged when unknown, so a deliberate
        "never-registered" probe flows the literal through.
        """
        return self._experiment_id_by_name.get(name, name)

    def worker_id_for(self, name: str) -> str:
        """Resolve a worker display name to its minted ``wkr_*`` id (else unchanged)."""
        return self._worker_id_by_name.get(name, name)

    def group_id_for(self, name: str) -> str:
        """Resolve a group display name to its minted ``grp_*`` id (else unchanged)."""
        return self._group_id_by_name.get(name, name)

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
        self, name: str | None, config_uri: str
    ) -> httpx.Response:
        """Register an experiment; the control plane MINTS the ``exp_*`` id.

        ``name`` is the optional operator-supplied display label
        (scenarios pass a stable handle here). On success the minted
        ``experiment_id`` is recorded under ``name`` so later calls can
        resolve the handle via :meth:`experiment_id_for`.
        """
        body: dict[str, Any] = {"config_uri": config_uri}
        if name is not None:
            body["name"] = name
        resp = self.request("POST", f"{self._base}/experiments", json=body)
        if name is not None and 200 <= resp.status_code < 300:
            minted = resp.json().get("experiment_id")
            if isinstance(minted, str):
                self._experiment_id_by_name[name] = minted
        return resp

    def unregister_experiment(self, experiment: str) -> httpx.Response:
        eid = self.experiment_id_for(experiment)
        return self.request("DELETE", f"{self._base}/experiments/{eid}")

    def list_experiments(self, *, name: str | None = None) -> httpx.Response:
        params: dict[str, str] | None = {"name": name} if name is not None else None
        return self.request("GET", f"{self._base}/experiments", params=params)

    def read_experiment_metadata(self, experiment: str) -> httpx.Response:
        eid = self.experiment_id_for(experiment)
        return self.request("GET", f"{self._base}/experiments/{eid}")

    # ------------------------------------------------------------------
    # §15.2 lease operations
    # ------------------------------------------------------------------

    def acquire_lease(
        self, experiment: str, holder: str, holder_instance: str
    ) -> httpx.Response:
        eid = self.experiment_id_for(experiment)
        return self.request(
            "POST",
            f"{self._base}/experiments/{eid}/leases",
            json={
                "holder": self.worker_id_for(holder),
                "holder_instance": holder_instance,
            },
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
            "GET",
            f"{self._base}/leases",
            params={"holder": self.worker_id_for(holder)},
        )

    # ------------------------------------------------------------------
    # §15.3 deployment-scoped worker / group registry
    # ------------------------------------------------------------------

    def register_worker(
        self,
        name: str | None,
        *,
        labels: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Register a deployment-scoped worker; the control plane MINTS ``wkr_*``.

        ``name`` is the optional display handle; on success the minted
        ``worker_id`` is recorded under it for :meth:`worker_id_for`.
        """
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if labels is not None:
            body["labels"] = labels
        resp = self.request("POST", f"{self._base}/workers", json=body)
        if name is not None and 200 <= resp.status_code < 300:
            minted = resp.json().get("worker_id")
            if isinstance(minted, str):
                self._worker_id_by_name[name] = minted
        return resp

    def register_group(
        self,
        name: str | None,
        *,
        members: list[str] | None = None,
    ) -> httpx.Response:
        """Register a deployment-scoped group; the control plane MINTS ``grp_*``.

        ``name`` is the optional display handle; ``members`` are
        worker / group display handles resolved to their minted opaque
        ids before dispatch. On success the minted ``group_id`` is
        recorded under ``name`` for :meth:`group_id_for`.
        """
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if members is not None:
            body["members"] = [self._member_id_for(m) for m in members]
        resp = self.request("POST", f"{self._base}/groups", json=body)
        if name is not None and 200 <= resp.status_code < 300:
            minted = resp.json().get("group_id")
            if isinstance(minted, str):
                self._group_id_by_name[name] = minted
        return resp

    def add_to_group(self, group: str, member: str) -> httpx.Response:
        gid = self.group_id_for(group)
        return self.request(
            "POST",
            f"{self._base}/groups/{gid}/members",
            json={"member_id": self._member_id_for(member)},
        )

    def remove_from_group(self, group: str, member: str) -> httpx.Response:
        gid = self.group_id_for(group)
        mid = self._member_id_for(member)
        return self.request(
            "DELETE",
            f"{self._base}/groups/{gid}/members/{mid}",
        )

    def _member_id_for(self, name: str) -> str:
        """Resolve a member handle (worker OR group) to its minted opaque id.

        A member of a group may itself be a worker or a nested group
        (chapter 02 §7), so consult both registries; fall back to the
        literal when unknown.
        """
        if name in self._worker_id_by_name:
            return self._worker_id_by_name[name]
        if name in self._group_id_by_name:
            return self._group_id_by_name[name]
        return name
