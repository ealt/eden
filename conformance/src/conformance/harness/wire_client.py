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

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: Mapping[str, str | int] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        kwargs: dict[str, Any] = {}
        if json is not None:
            kwargs["json"] = json
        if params is not None:
            kwargs["params"] = params
        if headers is not None:
            kwargs["headers"] = headers
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

    # Path helpers --------------------------------------------------

    @property
    def base_path(self) -> str:
        return f"/v0/experiments/{self.experiment_id}"

    def tasks_path(self, task_id: str | None = None, suffix: str = "") -> str:
        base = f"{self.base_path}/tasks"
        if task_id is None:
            return base
        return f"{base}/{task_id}{suffix}"

    def proposals_path(self, proposal_id: str | None = None, suffix: str = "") -> str:
        base = f"{self.base_path}/proposals"
        if proposal_id is None:
            return base
        return f"{base}/{proposal_id}{suffix}"

    def trials_path(self, trial_id: str | None = None, suffix: str = "") -> str:
        base = f"{self.base_path}/trials"
        if trial_id is None:
            return base
        return f"{base}/{trial_id}{suffix}"

    def events_path(self, suffix: str = "") -> str:
        return f"{self.base_path}/events{suffix}"
