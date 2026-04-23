"""Event — envelope appended to an EDEN event log.

Mirrors ``spec/v0/schemas/event.schema.json``. Type-specific payload shapes
live in spec/v0/05-event-protocol.md (Phase 4).
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from ._common import DateTimeStr

EVENT_TYPE_PATTERN = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$"


class Event(BaseModel):
    """Event envelope. The ``data`` payload shape is fixed by ``type``."""

    model_config = ConfigDict(strict=True, extra="allow")

    event_id: Annotated[str, Field(min_length=1)]
    type: Annotated[str, StringConstraints(pattern=EVENT_TYPE_PATTERN)]
    occurred_at: DateTimeStr
    experiment_id: Annotated[str, Field(min_length=1)]
    data: dict[str, Any]
