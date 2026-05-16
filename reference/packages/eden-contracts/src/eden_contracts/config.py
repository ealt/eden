"""ExperimentConfig — declarative input to an EDEN experiment.

Mirrors ``spec/v0/schemas/experiment-config.schema.json``.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from ._common import NotNone
from .evaluation import EvaluationSchema

Direction = Literal["maximize", "minimize"]
"""Objective direction — whether the scalar expression is to be maximized or minimized."""

WALL_TIME_PATTERN = r"^[1-9][0-9]*[smhd]$"
WallTime = Annotated[str, StringConstraints(pattern=WALL_TIME_PATTERN)]
"""Duration string — positive integer followed by one of s, m, h, d."""

DispatchModeValue = Literal["auto", "manual"]
"""Per-decision-type dispatch-mode value (``02-data-model.md`` §2.5)."""


class DispatchMode(BaseModel):
    """Per-experiment, per-decision-type gate on orchestrator automation.

    Each key gates one orchestrator decision (``03-roles.md`` §6.2):
    ``ideation_creation`` toggles auto-creation of ideation tasks,
    ``execution_dispatch`` toggles per-ready-idea execution-task creation,
    ``evaluation_dispatch`` toggles per-starting-variant evaluation-task
    creation, and ``integration`` toggles auto-invocation of the
    integrator on success variants. Every key defaults to ``"auto"``;
    an omitted key is equivalent to ``"auto"``. Unknown keys are
    tolerated and ignored per ``02-data-model.md`` §2.5.
    """

    model_config = ConfigDict(strict=True, extra="allow")

    ideation_creation: DispatchModeValue = "auto"
    execution_dispatch: DispatchModeValue = "auto"
    evaluation_dispatch: DispatchModeValue = "auto"
    integration: DispatchModeValue = "auto"


class ObjectiveSpec(BaseModel):
    """Scalar optimization target: expression over metrics + direction."""

    model_config = ConfigDict(strict=True, extra="allow")

    expr: Annotated[str, Field(min_length=1)]
    direction: Direction


class ExperimentConfig(BaseModel):
    """Declarative experiment input.

    Role bindings (how the ideator, executor, and evaluator are hosted)
    are implementation-defined and flow through via ``extra="allow"`` until
    the spec pins them.
    """

    model_config = ConfigDict(strict=True, extra="allow")

    parallel_variants: Annotated[int, Field(ge=1)]
    max_variants: Annotated[int, Field(ge=1)]
    max_wall_time: WallTime
    evaluation_schema: EvaluationSchema
    objective: ObjectiveSpec
    convergence_window: Annotated[int | None, NotNone, Field(ge=1)] = None
    target_condition: Annotated[str | None, NotNone, Field(min_length=1)] = None
    dispatch_mode: Annotated[DispatchMode | None, NotNone] = None
