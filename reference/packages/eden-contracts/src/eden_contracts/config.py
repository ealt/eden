"""ExperimentConfig — declarative input to an EDEN experiment.

Mirrors ``spec/v0/schemas/experiment-config.schema.json``.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    ValidationInfo,
    model_validator,
)

from ._common import DurationStr, NotNone
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


TerminationPolicyKind = Literal[
    "never_terminate",
    "max_variants",
    "max_wall_time",
    "convergence_window",
    "target_condition",
]
"""Named termination-policy kinds shipped by the reference impl."""


class NeverTerminateConfig(BaseModel):
    """Never terminate — the reference default; explicit no-op.

    Schema: see ``termination_policy`` (kind="never_terminate") in
    ``schemas/experiment-config.schema.json``.
    """

    model_config = ConfigDict(strict=True, extra="allow")

    kind: Literal["never_terminate"]


class MaxVariantsTerminationConfig(BaseModel):
    """Terminate when ``target`` variants have been attempted.

    Schema: see ``termination_policy`` (kind="max_variants").
    """

    model_config = ConfigDict(strict=True, extra="allow")

    kind: Literal["max_variants"]
    target: Annotated[int, Field(ge=1)]


class MaxWallTimeTerminationConfig(BaseModel):
    """Terminate when wall-time since experiment start exceeds ``duration``.

    ``duration`` is a validated ISO 8601 duration string (e.g. ``"PT2H"``)
    that the orchestrator's ``build_termination_policy`` converts to a
    ``timedelta`` at the factory boundary. Schema: see ``termination_policy``
    (kind="max_wall_time").
    """

    model_config = ConfigDict(strict=True, extra="allow")

    kind: Literal["max_wall_time"]
    duration: DurationStr


class ConvergenceWindowTerminationConfig(BaseModel):
    """Terminate when ``metric`` has not improved over the trailing ``window``.

    Schema: see ``termination_policy`` (kind="convergence_window").
    """

    model_config = ConfigDict(strict=True, extra="allow")

    kind: Literal["convergence_window"]
    metric: Annotated[str, Field(min_length=1)]
    window: Annotated[int, Field(ge=1)]
    direction: Direction = "maximize"


class TargetConditionTerminationConfig(BaseModel):
    """Terminate when the latest integrated variant's ``metric`` crosses ``threshold``.

    Schema: see ``termination_policy`` (kind="target_condition").
    """

    model_config = ConfigDict(strict=True, extra="allow")

    kind: Literal["target_condition"]
    metric: Annotated[str, Field(min_length=1)]
    threshold: float
    direction: Direction = "maximize"


TerminationPolicyConfig = Annotated[
    NeverTerminateConfig
    | MaxVariantsTerminationConfig
    | MaxWallTimeTerminationConfig
    | ConvergenceWindowTerminationConfig
    | TargetConditionTerminationConfig,
    Discriminator("kind"),
]
"""Discriminated union over named termination-policy kinds (see §2.4)."""


class ObjectiveSpec(BaseModel):
    """Scalar optimization target: expression over metrics + direction."""

    model_config = ConfigDict(strict=True, extra="allow")

    expr: Annotated[str, Field(min_length=1)]
    direction: Direction


class BaselineConfig(BaseModel):
    """Baseline-variant block (``02-data-model.md`` §2.7).

    Controls whether the experiment seed is elevated to a first-class
    ``kind == "baseline"`` variant (``02-data-model.md`` §9.4). An absent
    block is equivalent to ``{enabled: true}`` (default-on, real
    evaluation); the default-on interpretation lives in the orchestrator's
    ``ensure_baseline_variant``, not in this model's defaults (so
    ``model_dump(exclude_none=True)`` round-trips an absent ``enabled`` as
    absent for schema parity). When ``metrics`` is present the orchestrator
    creates the baseline directly in ``success`` carrying those metrics and
    skips evaluation dispatch; the metrics MUST validate against
    ``evaluation_schema`` at runtime (the same per-experiment limitation as
    ``variant.evaluation`` — §9.2).
    """

    model_config = ConfigDict(strict=True, extra="allow")

    enabled: Annotated[bool | None, NotNone] = None
    metrics: Annotated[dict[str, Any] | None, NotNone] = None

    @model_validator(mode="after")
    def _no_metrics_when_disabled(self) -> BaselineConfig:
        # Mirrors the experiment-config.schema.json baseline.allOf-if-then:
        # supplying metrics while enabled is false is a config error
        # (suppressing a baseline while supplying its metrics).
        if self.enabled is False and self.metrics is not None:
            raise ValueError(
                "baseline.metrics MUST NOT be supplied when baseline.enabled is false"
            )
        return self


class ExperimentConfig(BaseModel):
    """Declarative experiment input.

    Pre-12a-3 the model carried ``max_variants``, ``max_wall_time``,
    ``convergence_window``, and ``target_condition`` as normative
    top-level termination bounds; 12a-3 removed those scalar fields from
    the normative shape. Their semantics now round-trip as
    ``termination_policy.kind`` values: the orchestrator selects a
    termination policy declaratively via the ``termination_policy`` block
    (``03-roles.md`` §6.2 decision-type 0), and the ``build_termination_policy``
    factory in ``eden-dispatch`` maps it to a callable. Deployments MAY
    still carry the old top-level fields under the §2.3
    forward-compatibility rule; the ``extra="allow"`` config makes them
    round-trip without rejection.

    ``max_quiescent_iterations`` is the orchestrator's quiescent-exit
    budget (``03-roles.md`` §3.1); the three ``*_task_deadline`` fields
    bound each worker host's per-task SLA. All four are optional with
    reference defaults; ``termination_policy`` is required when
    ``dispatch_mode.termination == "auto"`` (enforced below and mirrored
    by the JSON Schema's top-level ``allOf-if-then``).

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
    termination_policy: Annotated[TerminationPolicyConfig | None, NotNone] = None
    baseline: Annotated[BaselineConfig | None, NotNone] = None
    max_quiescent_iterations: Annotated[int | None, NotNone, Field(ge=2)] = None
    ideation_task_deadline: Annotated[float | None, NotNone, Field(gt=0)] = None
    execution_task_deadline: Annotated[float | None, NotNone, Field(gt=0)] = None
    evaluation_task_deadline: Annotated[float | None, NotNone, Field(gt=0)] = None

    @model_validator(mode="after")
    def _termination_required_when_auto(
        self, info: ValidationInfo
    ) -> ExperimentConfig:
        # Mirrors the JSON Schema's top-level allOf-if-then over
        # dispatch_mode.termination. Both sides reject the same fixtures;
        # the schema-parity test (which validates with no context) is the
        # gate that keeps them in lockstep.
        #
        # This is a SINGLE-EXPERIMENT-mode contract: that mode reads the
        # termination policy from this config's termination_policy block.
        # The orchestrator's multi-experiment mode drives termination from
        # the --termination-policy CLI flag instead (per-experiment config
        # resolution is deferred to issue #214), so it loads the bootstrap
        # config with context={"require_termination_policy": False} to skip
        # this check — keeping a termination=auto bootstrap config that was
        # valid pre-#157 startable. Absent that explicit opt-out the rule is
        # enforced.
        if info.context and info.context.get("require_termination_policy") is False:
            return self
        if (
            self.dispatch_mode is not None
            and self.dispatch_mode.termination == "auto"
            and self.termination_policy is None
        ):
            raise ValueError(
                "termination_policy is required when "
                "dispatch_mode.termination == 'auto'"
            )
        return self
