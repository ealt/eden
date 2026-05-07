"""Worker — registered identity within a single experiment.

Mirrors ``spec/v0/schemas/worker.schema.json``. The wire-visible
shape MUST NOT carry the worker's authentication credential or its
hash; per-worker auth is specified in spec/v0/04-task-protocol.md
§3 / §4 and spec/v0/07-wire-protocol.md §13.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from ._common import DateTimeStr, NotNone, WorkerId

WorkerLabels = dict[str, str]
"""Free-form deployment metadata; the protocol does not interpret labels."""


class Worker(BaseModel):
    """A registered worker for one experiment."""

    model_config = ConfigDict(strict=True, extra="allow")

    worker_id: WorkerId
    experiment_id: Annotated[str, Field(min_length=1)]
    registered_at: DateTimeStr
    registered_by: Annotated[str | None, NotNone, Field(min_length=1)] = None
    labels: Annotated[WorkerLabels | None, NotNone] = None
