"""ExperimentConfig — declarative input to an EDEN experiment.

Mirrors ``spec/v0/schemas/experiment-config.schema.json``.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Discriminator, Field

from ._common import NotNone
from .evaluation import EvaluationSchema

Direction = Literal["maximize", "minimize"]
"""Objective direction — whether the scalar expression is to be maximized or minimized."""

DispatchModeValue = Literal["auto", "manual"]
"""Per-decision-type dispatch-mode value (``02-data-model.md`` §2.4)."""


class DispatchMode(BaseModel):
    """Per-experiment, per-decision-type gate on orchestrator automation.

    Each key gates one orchestrator decision (``03-roles.md`` §6.2):
    ``termination`` toggles whether the orchestrator consults the
    deployment-supplied termination policy, ``ideation_creation``
    toggles auto-creation of ideation tasks, ``execution_dispatch``
    toggles per-ready-idea execution-task creation,
    ``evaluation_dispatch`` toggles per-starting-variant evaluation-task
    creation, and ``integration`` toggles auto-invocation of the
    integrator on success variants. The four operational keys default
    to ``"auto"``; ``termination`` defaults to ``"manual"`` for
    backward compatibility with pre-12a-3 deployments that had no
    termination policy. Unknown keys are tolerated and ignored per
    ``02-data-model.md`` §2.4.
    """

    model_config = ConfigDict(strict=True, extra="allow")

    termination: DispatchModeValue = "manual"
    ideation_creation: DispatchModeValue = "auto"
    execution_dispatch: DispatchModeValue = "auto"
    evaluation_dispatch: DispatchModeValue = "auto"
    integration: DispatchModeValue = "auto"


IdeationPolicyKind = Literal["maintain_pending", "fixed_total"]
"""Named ideation-policy kinds shipped by the reference impl."""


class MaintainPendingPolicyConfig(BaseModel):
    """Refill the pending-ideation queue to ``target`` depth each iteration.

    Optionally clamp lifetime ideation creation to ``max_total`` tasks
    (``None`` disables the cap). Schema: see
    ``ideation_policy`` (kind="maintain_pending") in
    ``schemas/experiment-config.schema.json``.
    """

    model_config = ConfigDict(strict=True, extra="allow")

    kind: Literal["maintain_pending"]
    target: Annotated[int, Field(ge=1)] = 3
    max_total: Annotated[int | None, Field(ge=0)] = None


class FixedTotalPolicyConfig(BaseModel):
    """Create exactly ``total`` ideation tasks across the experiment's lifetime.

    Schema: see ``ideation_policy`` (kind="fixed_total") in
    ``schemas/experiment-config.schema.json``.
    """

    model_config = ConfigDict(strict=True, extra="allow")

    kind: Literal["fixed_total"]
    total: Annotated[int, Field(ge=1)]


IdeationPolicyConfig = Annotated[
    MaintainPendingPolicyConfig | FixedTotalPolicyConfig,
    Discriminator("kind"),
]
"""Discriminated union over named ideation-policy kinds (see §2.4)."""


class ObjectiveSpec(BaseModel):
    """Scalar optimization target: expression over metrics + direction."""

    model_config = ConfigDict(strict=True, extra="allow")

    expr: Annotated[str, Field(min_length=1)]
    direction: Direction


class ExperimentConfig(BaseModel):
    """Declarative experiment input.

    Pre-12a-3 the model carried ``max_variants``, ``max_wall_time``,
    ``convergence_window``, and ``target_condition`` as normative
    termination bounds; 12a-3 moves termination to a deployment-supplied
    policy callable (``03-roles.md`` §6.2 decision-type 0) and the four
    fields are removed from the normative shape. Deployments MAY still
    carry them as additional top-level fields under the §2.3
    forward-compatibility rule; the ``extra="allow"`` config makes them
    round-trip without rejection.

    Role bindings (how the ideator, executor, and evaluator are hosted)
    are implementation-defined and flow through the same ``extra="allow"``
    channel until the spec pins them.
    """

    model_config = ConfigDict(strict=True, extra="allow")

    parallel_variants: Annotated[int, Field(ge=1)]
    evaluation_schema: EvaluationSchema
    objective: ObjectiveSpec
    dispatch_mode: Annotated[DispatchMode | None, NotNone] = None
    ideation_policy: Annotated[IdeationPolicyConfig | None, NotNone] = None
