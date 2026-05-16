"""Experiment — observed runtime state of an EDEN experiment.

Mirrors ``spec/v0/schemas/experiment.schema.json``. Distinct from
``ExperimentConfig`` (declarative input written by the operator at
experiment creation): this model carries the lifecycle ``state`` field
introduced in 12a-3 (``02-data-model.md`` §2.5) and any other observed
runtime metadata. The split mirrors task vs task-payload — declarative
input lives in the config, observed runtime in the experiment object.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from ._common import DateTimeStr

ExperimentState = Literal["running", "terminated"]
"""Lifecycle state per ``02-data-model.md`` §2.5.

``"running"`` is the default at experiment creation. ``"terminated"``
is a one-way transition committed by the operator wire op
(``04-task-protocol.md`` §8.1) or the orchestrator's policy-driven
termination path (``03-roles.md`` §6.2 decision-type 0).
"""


class Experiment(BaseModel):
    """Runtime object backing the ``experiment.state`` lifecycle field.

    Constructed in-memory by the Store; mutated only via the lifecycle
    ops (``terminate_experiment`` / ``update_experiment_state``).
    """

    model_config = ConfigDict(strict=True, extra="allow")

    experiment_id: Annotated[str, Field(min_length=1)]
    state: ExperimentState
    created_at: DateTimeStr
