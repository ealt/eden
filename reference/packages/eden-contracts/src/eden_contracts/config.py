"""ExperimentConfig — declarative input to an EDEN experiment.

Mirrors ``spec/v0/schemas/experiment-config.schema.json``.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from ._common import NotNone
from .metrics import MetricsSchema

Direction = Literal["maximize", "minimize"]
"""Objective direction — whether the scalar expression is to be maximized or minimized."""

WALL_TIME_PATTERN = r"^[1-9][0-9]*[smhd]$"
WallTime = Annotated[str, StringConstraints(pattern=WALL_TIME_PATTERN)]
"""Duration string — positive integer followed by one of s, m, h, d."""


class ObjectiveSpec(BaseModel):
    """Scalar optimization target: expression over metrics + direction."""

    model_config = ConfigDict(strict=True, extra="allow")

    expr: Annotated[str, Field(min_length=1)]
    direction: Direction


class ExperimentConfig(BaseModel):
    """Declarative experiment input.

    Role bindings (how the planner, implementer, and evaluator are hosted)
    are implementation-defined and flow through via ``extra="allow"`` until
    the spec pins them.
    """

    model_config = ConfigDict(strict=True, extra="allow")

    parallel_trials: Annotated[int, Field(ge=1)]
    max_trials: Annotated[int, Field(ge=1)]
    max_wall_time: WallTime
    metrics_schema: MetricsSchema
    objective: ObjectiveSpec
    convergence_window: Annotated[int | None, NotNone, Field(ge=1)] = None
    target_condition: Annotated[str | None, NotNone, Field(min_length=1)] = None
