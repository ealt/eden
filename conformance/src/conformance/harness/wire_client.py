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
        self._worker_bearers: dict[str, str] = {}
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
        """Mirror ``other``'s per-worker bearer registry onto this client.

        Used by scenarios that spawn a second WireClient against the
        same IUT to model two distinct client applications sharing
        a worker identity (chapter 04 §3.3 cross-application claim).
        """
        self._worker_bearers.update(other._worker_bearers)

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
        files: Any | None = None,
    ) -> httpx.Response:
        kwargs: dict[str, Any] = {}
        if json is not None:
            kwargs["json"] = json
        if params is not None:
            kwargs["params"] = params
        request_headers: dict[str, str] = {}
        if headers is not None:
            request_headers.update(headers)
        if files is not None:
            # Multipart deposit (chapter 07 §16.1). The client carries a
            # default ``Content-Type: application/json`` header, which
            # httpx will NOT override for a ``files=`` request — so the
            # server would see JSON and find no multipart parts. Encode
            # the multipart body via a standalone ``httpx.Request`` (no
            # client defaults applied) to capture the generated boundary
            # Content-Type, then send the raw content with that header
            # overriding the JSON default.
            encoded = httpx.Request(method, self._client.base_url.join(path), files=files)
            kwargs["content"] = encoded.read()
            request_headers["Content-Type"] = encoded.headers["content-type"]
        if as_worker is not None:
            # Per-call bearer swap. Look up the worker's credential and
            # override the Authorization header for this single
            # request; the client's default header (typically the
            # admin bearer) stays in place for other calls.
            bearer = self._worker_bearers.get(as_worker)
            if bearer is not None:
                request_headers["Authorization"] = f"Bearer {bearer}"
            else:
                # Unknown worker — send the bearer in the form the
                # server would reject (worker-id principal but no
                # secret). Scenarios deliberately probing the
                # ``unauthorized`` / ``worker-not-registered`` paths
                # use this fallback rather than seeding a credential.
                request_headers["Authorization"] = f"Bearer {as_worker}:not-a-real-token"
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
