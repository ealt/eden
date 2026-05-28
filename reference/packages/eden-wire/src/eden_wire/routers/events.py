"""Event-log routes (chapter 7 §6): read-range + long-poll subscribe."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, Header, Query

from .._dependencies import RouterDeps, check_experiment
from ..models import EventsResponse


def build_router(deps: RouterDeps) -> APIRouter:
    """Return the events ``APIRouter`` bound to ``deps``."""
    router = APIRouter(prefix="/v0/experiments/{experiment_id}/events")
    router.get("")(_read_range(deps))
    router.get("/subscribe")(_subscribe(deps))
    return router


def _read_range(deps: RouterDeps):
    async def read_range(
        experiment_id: str,
        cursor: int = Query(0, ge=0),
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        check_experiment(deps, experiment_id, x_eden_experiment_id)
        events = deps.store.read_range(cursor=cursor if cursor > 0 else None)
        resp = EventsResponse(events=events, cursor=cursor + len(events))
        return resp.model_dump(mode="json", exclude_none=True)

    return read_range


def _subscribe(deps: RouterDeps):
    async def subscribe(
        experiment_id: str,
        cursor: int = Query(0, ge=0),
        timeout: float | None = Query(None, ge=0),
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        # §6.2 long-poll: hold the connection open until at least one
        # event is available after `cursor` or ``timeout`` (default
        # ``subscribe_timeout``) elapses. The underlying ``Store`` is a
        # synchronous in-process object, so we poll ``read_range`` in a
        # loop with a short interval. An asyncio.sleep yields to the
        # event loop, so other requests (e.g. the write that unblocks
        # us) progress concurrently.
        check_experiment(deps, experiment_id, x_eden_experiment_id)
        effective_timeout = (
            timeout if timeout is not None else deps.subscribe_timeout
        )
        deadline = time.monotonic() + effective_timeout
        events = deps.store.read_range(cursor=cursor if cursor > 0 else None)
        while not events and time.monotonic() < deadline:
            await asyncio.sleep(deps.subscribe_poll_interval)
            events = deps.store.read_range(cursor=cursor if cursor > 0 else None)
        resp = EventsResponse(events=events, cursor=cursor + len(events))
        return resp.model_dump(mode="json", exclude_none=True)

    return subscribe
