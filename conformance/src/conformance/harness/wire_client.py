"""Thin httpx wrapper used by every conformance scenario.

Returns raw httpx.Response objects; does NOT decode problem+json into
typed exceptions, does NOT retry, does NOT paper over errors. The
suite needs to assert on status codes, content-type headers, and
raw body shapes — wrappers that hide those details would mask the
wire shape under test.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx


class WireClient:
    """Direct chapter-7 HTTP client used by conformance scenarios."""

    def __init__(
        self,
        *,
        base_url: str,
        experiment_id: str,
        extra_headers: Mapping[str, str] | None = None,
        timeout: float = 30.0,
        observed_problem_types: set[str] | None = None,
    ) -> None:
        headers = {
            "X-Eden-Experiment-Id": experiment_id,
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json, application/problem+json",
        }
        if extra_headers:
            headers.update(extra_headers)
        self.base_url = base_url.rstrip("/")
        self.experiment_id = experiment_id
        # Shared collector for vocabulary-closure assertions; pass a
        # session-scoped set to accumulate across scenarios.
        self.observed_problem_types: set[str] = (
            observed_problem_types if observed_problem_types is not None else set()
        )
        # Per-worker bearer registry. The default ``Authorization``
        # header in ``headers`` (set from ``extra_headers``) is the
        # admin bearer; ``request(..., as_worker=<wid>)`` swaps it for
        # the worker's registered credential for that single call.
        # See the chapter-7 §13 per-worker bearer scheme.
        #
        # Since the identity rename (#128) worker/group ids are opaque,
        # system-minted (``wkr_*`` / ``grp_*``); scenarios still want to
        # refer to a worker/group by a stable human handle. The bearer
        # registry is keyed by BOTH the minted opaque id and (when a
        # display name was used at registration) the display name, so
        # ``as_worker="executor-host"`` and ``as_worker="wkr_..."`` both
        # resolve. The name<->id maps below let scenarios resolve a
        # display name to the minted opaque id when building wire
        # payloads that carry an opaque reference (``target.id``,
        # ``member_id``, ``intended_executor`` / ``intended_evaluator``).
        self._worker_bearers: dict[str, str] = {}
        self._worker_id_by_name: dict[str, str] = {}
        self._group_id_by_name: dict[str, str] = {}
        self._client = httpx.Client(
            base_url=self.base_url,
            headers=headers,
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> WireClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def copy_worker_bearers_from(self, other: WireClient) -> None:
        """Mirror ``other``'s per-worker bearer + name->id registries onto this client.

        Used by scenarios that spawn a second WireClient against the
        same IUT to model two distinct client applications sharing
        a worker identity (chapter 04 §3.3 cross-application claim).
        """
        self._worker_bearers.update(other._worker_bearers)
        self._worker_id_by_name.update(other._worker_id_by_name)
        self._group_id_by_name.update(other._group_id_by_name)

    # Identity registry (name <-> minted opaque id) -----------------

    def record_worker_identity(self, name: str | None, worker_id: str) -> None:
        """Record the minted ``worker_id`` and (optional) display name.

        Lets later calls resolve a stable display name to the opaque
        ``wkr_*`` id the server minted at registration time.
        """
        if name is not None:
            self._worker_id_by_name[name] = worker_id

    def record_group_identity(self, name: str | None, group_id: str) -> None:
        """Record a minted ``grp_*`` id under its (optional) display name."""
        if name is not None:
            self._group_id_by_name[name] = group_id

    def worker_id_for(self, name: str) -> str:
        """Resolve a worker display name to its minted ``wkr_*`` id.

        If ``name`` already looks like an opaque id (or is unknown), it
        is returned unchanged so callers can pass either a handle or a
        raw id, and deliberate "unknown worker" probes still flow the
        literal through.
        """
        return self._worker_id_by_name.get(name, name)

    def group_id_for(self, name: str) -> str:
        """Resolve a group display name to its minted ``grp_*`` id (else unchanged)."""
        return self._group_id_by_name.get(name, name)

    def member_ref(self, kind: str, name: str) -> dict[str, str]:
        """Build a ``{kind, id}`` target/member ref, resolving name->opaque id.

        ``kind`` is ``"worker"`` or ``"group"``; ``name`` is the stable
        handle the scenario uses. The returned ``id`` is the minted
        opaque id (``wkr_*`` / ``grp_*``) so it satisfies the
        ``MemberId`` grammar the wire now enforces.
        """
        if kind == "group":
            return {"kind": "group", "id": self.group_id_for(name)}
        return {"kind": "worker", "id": self.worker_id_for(name)}

    def register_worker_bearer(self, worker_id: str, bearer: str) -> None:
        """Associate ``worker_id`` with the per-worker bearer.

        Subsequent ``request(..., as_worker=worker_id)`` calls swap the
        default ``Authorization`` header for ``Bearer <bearer>`` on
        that single request. ``bearer`` is the §13.1 form
        ``<principal>:<secret>``.
        """
        self._worker_bearers[worker_id] = bearer

    def bearer_for(self, worker_id: str) -> str:
        """Return the bearer registered for ``worker_id``.

        Raises ``KeyError`` if ``worker_id`` has not been registered
        through :meth:`register_worker_bearer` — useful for catching
        scenarios that forgot to seed a worker's credential.
        """
        return self._worker_bearers[worker_id]

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: Mapping[str, str | int] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        as_worker: str | None = None,
    ) -> httpx.Response:
        kwargs: dict[str, Any] = {}
        if json is not None:
            kwargs["json"] = json
        if params is not None:
            kwargs["params"] = params
        request_headers: dict[str, str] = {}
        if headers is not None:
            request_headers.update(headers)
        if as_worker is not None:
            # Per-call bearer swap. ``as_worker`` may be a display-name
            # handle or an already-minted ``wkr_*`` id; resolve to the
            # opaque id first, then look up the credential and override
            # the Authorization header for this single request. The
            # client's default header (typically the admin bearer) stays
            # in place for other calls.
            principal = self.worker_id_for(as_worker)
            bearer = self._worker_bearers.get(principal)
            if bearer is None:
                bearer = self._worker_bearers.get(as_worker)
            if bearer is not None:
                request_headers["Authorization"] = f"Bearer {bearer}"
            else:
                # Unknown worker — send the bearer in the form the
                # server would reject (worker-id principal but no
                # secret). Scenarios deliberately probing the
                # ``unauthorized`` / ``worker-not-registered`` paths
                # use this fallback rather than seeding a credential.
                request_headers["Authorization"] = f"Bearer {principal}:not-a-real-token"
        if request_headers:
            kwargs["headers"] = request_headers
        if timeout is not None:
            kwargs["timeout"] = timeout
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

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("PATCH", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("DELETE", path, **kwargs)

    # Path helpers --------------------------------------------------

    @property
    def base_path(self) -> str:
        return f"/v0/experiments/{self.experiment_id}"

    def tasks_path(self, task_id: str | None = None, suffix: str = "") -> str:
        base = f"{self.base_path}/tasks"
        if task_id is None:
            return base
        return f"{base}/{task_id}{suffix}"

    def dispatch_mode_path(self) -> str:
        return f"{self.base_path}/dispatch_mode"

    def ideas_path(self, idea_id: str | None = None, suffix: str = "") -> str:
        base = f"{self.base_path}/ideas"
        if idea_id is None:
            return base
        return f"{base}/{idea_id}{suffix}"

    def variants_path(self, variant_id: str | None = None, suffix: str = "") -> str:
        base = f"{self.base_path}/variants"
        if variant_id is None:
            return base
        return f"{base}/{variant_id}{suffix}"

    def events_path(self, suffix: str = "") -> str:
        return f"{self.base_path}/events{suffix}"

    def terminate_path(self) -> str:
        """``POST /v0/experiments/{E}/terminate`` (12a-3 §2.9)."""
        return f"{self.base_path}/terminate"

    def state_path(self) -> str:
        """``GET /v0/experiments/{E}/state`` (12a-3 §2.9 companion read)."""
        return f"{self.base_path}/state"

    def experiment_path(self) -> str:
        """``GET /v0/experiments/{E}`` (12b chapter 07 §14.3 full read)."""
        return self.base_path

    def export_checkpoint_path(self) -> str:
        """``POST /v0/experiments/{E}/checkpoint`` (12b chapter 07 §14.1)."""
        return f"{self.base_path}/checkpoint"

    def import_checkpoint_path(self) -> str:
        """``POST /v0/checkpoints/import`` (12b chapter 07 §14.2).

        Global path outside ``experiments/{E}/`` per the §1.3 carve-out;
        callers MAY omit the ``X-Eden-Experiment-Id`` header on this
        endpoint.
        """
        return "/v0/checkpoints/import"

    def request_bytes(
        self,
        method: str,
        path: str,
        *,
        content: bytes,
        content_type: str,
        params: Mapping[str, str | int] | None = None,
        headers: Mapping[str, str] | None = None,
        omit_experiment_header: bool = False,
        timeout: float | None = None,
    ) -> httpx.Response:
        """Send a raw-bytes request body (no JSON encoding).

        Used by the checkpoint endpoints, which carry tar archive bytes
        as the body. ``omit_experiment_header`` flips the §1.3
        carve-out: when True, the default ``X-Eden-Experiment-Id``
        header is stripped before dispatch.
        """
        merged: dict[str, str] = {"Content-Type": content_type}
        if headers is not None:
            merged.update(headers)
        if omit_experiment_header:
            request_headers = {
                k: v
                for k, v in self._client.headers.items()
                if k.lower() != "x-eden-experiment-id"
            }
            request_headers.update(merged)
            kwargs: dict[str, Any] = {"headers": request_headers, "content": content}
        else:
            kwargs = {"headers": merged, "content": content}
        if params is not None:
            kwargs["params"] = params
        if timeout is not None:
            kwargs["timeout"] = timeout
        resp = self._client.request(method, path, **kwargs)
        self._record_problem_type(resp)
        return resp
