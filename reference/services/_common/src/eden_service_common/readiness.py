"""Readiness probe for a task-store-server.

Retries ``GET /v0/experiments/{E}/events`` with capped exponential
backoff until it returns 200 or a deadline expires. Used by the
orchestrator and worker hosts at startup so they don't issue a burst
of failing requests against a task-store that is still binding.
"""

from __future__ import annotations

import time

import httpx


class TaskStoreUnreachable(RuntimeError):
    """Readiness probe failed to reach the task-store within the deadline."""


def wait_for_task_store(
    *,
    base_url: str,
    experiment_id: str,
    token: str | None = None,
    deadline_seconds: float = 30.0,
    initial_backoff_seconds: float = 0.05,
    max_backoff_seconds: float = 1.0,
) -> None:
    """Block until the task-store is live or the deadline expires.

    Raises :class:`TaskStoreUnreachable` on timeout. The probe targets
    ``/v0/experiments/{E}/events`` — a normative endpoint that any
    conforming task-store implements — so the check is transport-
    agnostic beyond "HTTP".
    """
    url = f"{base_url.rstrip('/')}/v0/experiments/{experiment_id}/events"
    headers: dict[str, str] = {"X-Eden-Experiment-Id": experiment_id}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"

    deadline = time.monotonic() + deadline_seconds
    backoff = initial_backoff_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with httpx.Client(timeout=2.0) as client:
                resp = client.get(url, headers=headers)
            if resp.status_code == 200:
                return
            last_error = RuntimeError(
                f"task-store returned HTTP {resp.status_code}: {resp.text!r}"
            )
        except httpx.HTTPError as exc:
            last_error = exc
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(backoff, remaining))
        backoff = min(backoff * 2, max_backoff_seconds)
    raise TaskStoreUnreachable(
        f"task-store at {base_url!r} not ready within {deadline_seconds}s"
        + (f" (last error: {last_error!r})" if last_error else "")
    )
