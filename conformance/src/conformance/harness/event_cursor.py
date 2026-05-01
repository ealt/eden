"""Event-log helpers for conformance scenarios.

Wraps GET /events and GET /events/subscribe into helpers that
scenarios can use to assert ordering, replay, and atomicity
properties.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import httpx

from .wire_client import WireClient


class EventLog:
    """Convenience read-helpers over the chapter-7 §6 event endpoints."""

    def __init__(self, client: WireClient) -> None:
        self._client = client

    def replay_all(self) -> list[dict[str, Any]]:
        resp = self._client.get(self._client.events_path(), params={"cursor": 0})
        resp.raise_for_status()
        return list(resp.json()["events"])

    def replay_from(self, cursor: int) -> tuple[list[dict[str, Any]], int]:
        resp = self._client.get(self._client.events_path(), params={"cursor": cursor})
        resp.raise_for_status()
        body = resp.json()
        return list(body["events"]), int(body["cursor"])

    def subscribe(
        self,
        cursor: int,
        *,
        timeout: float | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        kwargs: dict[str, Any] = {"params": {"cursor": cursor}}
        if timeout is not None:
            kwargs["timeout"] = timeout
        resp = self._client.get(self._client.events_path("/subscribe"), **kwargs)
        resp.raise_for_status()
        body = resp.json()
        return list(body["events"]), int(body["cursor"])

    def find_by_type(
        self,
        events: Iterable[dict[str, Any]],
        type_name: str,
    ) -> list[dict[str, Any]]:
        return [e for e in events if e.get("type") == type_name]

    def find_for_task(
        self,
        events: Iterable[dict[str, Any]],
        task_id: str,
    ) -> list[dict[str, Any]]:
        return [e for e in events if e.get("data", {}).get("task_id") == task_id]


def assert_response_ok(resp: httpx.Response, *, expected: int = 200) -> None:
    """Lightweight assertion helper for 2xx responses with diagnostic body."""
    if resp.status_code != expected:
        raise AssertionError(
            f"Expected {expected}, got {resp.status_code}: "
            f"{resp.headers.get('content-type', '<no content-type>')} "
            f"body={resp.text[:500]}"
        )
