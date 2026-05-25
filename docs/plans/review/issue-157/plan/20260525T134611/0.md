# Issue #157 — Promote deployment CLI flags to experiment-config fields

## 1. Context

[Issue #157](https://github.com/ealt/eden/issues/157) audited the orchestrator / worker-host / web-ui CLI surface and identified eight flags whose values **two operators driving two experiments in the same deployment would plausibly want different values for**, yet today live as deployment-wide CLI flags rather than per-experiment config:

| Service | Flag | Current default |
|---|---|---|
| orchestrator | `--max-quiescent-iterations` | `3` (Compose overrides to `30` via `EDEN_MAX_QUIESCENT_ITERATIONS`) |
| orchestrator | `--termination-policy` | `eden_dispatch.termination:default_termination_policy` (= `never_terminate`) |
| orchestrator | `--lease-duration-seconds` | `30` |
| ideator-host | `--ideas-per-ideation` | `1` |
| ideator-host | `--ideation-task-deadline` | `120.0` |
| executor-host | `--execution-task-deadline` | `600.0` |
| evaluator-host | `--evaluation-task-deadline` | `300.0` |
| web-ui | `--claim-ttl-seconds` | `3600` |

Issue #157 names #133 ([impl branch](https://github.com/ealt/eden/pull/215) — `impl/issue-133-ideation-policy-config`) as the template: surface a deployment-wide CLI flag as a typed block in `experiment-config.yaml`, validated by both the JSON Schema and the Pydantic `eden-contracts` binding. This chunk is the bulk audit follow-up to that template.

**Status of #133 at plan-authoring time:** PR #215 is open against `main` (`b9202a7` "Fix #133: surface ideation policy in experiment config"). The plan assumes #133 lands before impl-start; if it has not, the impl PR rebases against `main` once it does. The mechanical shape #133 establishes — a discriminated-union YAML block, a Pydantic `IdeationPolicyConfig` union, a `build_policy()` factory in `eden-dispatch`, and the orchestrator dropping `--ideation-policy` entirely — is the reference template every promotion below mirrors.

**Per-flag recommendation summary.** The issue acknowledges three of the eight are debatable. The plan stakes a recommendation per flag with rationale, expecting codex-review and operator review to push back per-flag during this plan PR:

| Flag | Recommended disposition | Rationale |
|---|---|---|
| `--termination-policy` | **Promote** | Already the explicit cousin of `ideation_policy` — same module:callable factory shape, same "policy decision the experiment owner picks" framing. #214 names it as the natural co-promotion with per-experiment ideation. |
| `--max-quiescent-iterations` | **Promote** | The Compose comment at [`compose.yaml:270-278`](../../reference/compose/compose.yaml) names this as per-experiment ("Manual-UI sessions want a much higher value"); the deployment override exists because it's an experiment-shape parameter today wearing deployment clothes. |
| `--ideas-per-ideation` | **Promote** | Ideation batching is a per-experiment workload-shape parameter. setup-experiment already plumbs `--ideas-per-ideation` from a deployment-level flag into the per-experiment compose env (see [`user-guide.md:116`](../user-guide.md)) — that's evidence the operator already treats it as per-experiment, but the wire is wrong. |
| `--ideation-task-deadline` / `--execution-task-deadline` / `--evaluation-task-deadline` | **Promote** | The `*_command` strings these deadlines bound are *already* in the experiment config (under `extra="allow"`). The deadline travels with the command. The issue's "arguably worker-host SLA" framing is true at the host-implementation level but the workload-shape question (LLM evaluator at 10min vs deterministic eval at 5s) is experiment-level. |
| `--lease-duration-seconds` | **Keep as deployment flag** | Per chapter 11 §4.3 the lease is the contract between an orchestrator replica and the control plane. Experiments don't observe it; orchestrator-replica operators do. Promoting it would force the control-plane to decide whose lease semantics win when replicas hold leases for differently-configured experiments. Out of scope per issue §"Decision". |
| `--claim-ttl-seconds` | **Keep as deployment flag** | UX preference for the web-ui's manual-claim survival window — not a protocol-shaping parameter. Issue §"Decision" agrees. Out of scope. |

**Net scope: 6 of the 8 flags promote; 2 stay deployment-level.** The plan codifies the recommendation now and surfaces it for codex / operator review; the two excluded flags can be re-litigated in a follow-up if the rationale doesn't hold.

## 2. Decisions captured before drafting

Listed here so codex-review can see what was deliberate vs. proposable:

1. **No CLI fallback for promoted flags.** Per CLAUDE.md ("No backwards-compatibility shims in greenfield / pre-external-user projects") and #133's precedent (which **dropped** `--ideation-policy` entirely rather than keeping it as a fallback), each promoted flag is **removed from the CLI** in this chunk. The experiment-config field becomes the only knob. Deployments that today set a non-default value migrate to the YAML in lockstep. Issue #157's wording ("Update the service's CLI to read the field from the loaded experiment-config first, fall back to the flag") is superseded by #133's cleaner posture. If a real follow-up surfaces a deployment-default need (e.g. operator wants "all experiments in this deployment terminate after 1000 variants unless overridden"), that's a separate field shape (`deployment-defaults.yaml`) and a separate chunk — out of scope here.

2. **Termination policy YAML shape mirrors ideation policy: declarative discriminated union, not `module:callable`.** Today's `--termination-policy <module:callable>` is operationally hostile (operators ship a Python module just to wrap `max_wall_time_policy(timedelta(hours=2))`). The YAML field declares the policy by name + parameters; the orchestrator's `build_termination_policy(config)` factory maps to the five shipped reference policies (`never_terminate`, `max_variants`, `max_wall_time`, `convergence_window`, `target_condition`). Custom callables are out of the YAML surface — operators who genuinely need an arbitrary Python callable ship their own `eden-dispatch` distribution (the security posture #133 took).

3. **Each promoted field is *required* in the YAML iff the corresponding `dispatch_mode` key is `auto` OR the field gates a host-loop behavior.** Specifically: `termination_policy` is required when `dispatch_mode.termination == "auto"` (parity with the existing dispatch-mode design); `max_quiescent_iterations` / `ideas_per_ideation` / `*_task_deadline` are optional with reference defaults (the values used today). Validation enforced by the JSON Schema + Pydantic; missing-when-required surfaces as a clear `parser.error` at orchestrator startup, not silent defaults.

4. **Service load discipline.** Every service that consumes a promoted field already loads the experiment-config YAML at startup ([`load_experiment_config()`](../../reference/services/_common/src/eden_service_common/experiment_config.py:19) is wired into ideator-host, executor-host, evaluator-host, web-ui; #133 makes it wired into orchestrator too). So no service gains a NEW config-load this chunk — the field-read is one extra line per startup.

5. **Conformance scope unchanged.** The promoted fields are all implementation-defined per chapter 03 §6 (orchestrator decision-type contracts are specified; the policies that drive them are not normative). The JSON Schema gains the new field shapes (additive); chapter 09 §5 conformance group index does NOT gain a new row. Wire conformance (chapter 7) is unaffected — the wire never exposes these fields directly.

6. **No multi-experiment-mode rework in this chunk.** #214 ("Per-experiment ideation policy in multi-experiment mode") is the follow-up that wires per-experiment configs through the control-plane registry. This chunk's orchestrator changes apply to single-experiment mode and to multi-experiment-mode where the single `--experiment-config` argument is still the canonical source. Multi-experiment mode reading per-experiment configs from `config_uri` is #214's surface; this chunk does not pre-empt it but does not block it either (the `build_termination_policy(config)` factory is the same hook #214 will call per-experiment).

These six decisions are NOT up for re-litigation in codex-review unless review surfaces a load-bearing contradiction with another spec MUST or a CLAUDE.md discipline.

## 3. Design

### 3.1 Schema additions (`spec/v0/schemas/experiment-config.schema.json`)

Six new optional top-level fields, additive to the schema #133 leaves behind. The discriminated-union shapes mirror #133's `ideation_policy` pattern (with `kind` as the discriminator + per-kind parameters; unknown kinds rejected; `allOf`-`if`-`then` gates per-kind requirements).

```jsonc
{
  "properties": {
    // … existing parallel_variants, evaluation_schema, objective,
    //    dispatch_mode, ideation_policy (from #133) …

    "termination_policy": {
      "type": "object",
      "description": "Declarative selection of the termination policy invoked by the orchestrator when dispatch_mode.termination == 'auto' (see 02-data-model.md §2.4 and 03-roles.md §6.2 decision-type 0). Cousin of ideation_policy. The five named kinds are the reference policies; conforming implementations MUST accept the listed kinds. Unknown kinds MUST fail config validation rather than silently fall back. When dispatch_mode.termination == 'auto' the field is required; when 'manual' (the default) the field is ignored.",
      "required": ["kind"],
      "properties": {
        "kind": {"enum": ["never_terminate", "max_variants", "max_wall_time", "convergence_window", "target_condition"]},
        "target": {"type": "integer", "minimum": 1, "description": "kind='max_variants' only — hard ceiling on attempted variants."},
        "duration": {"type": "string", "format": "duration", "description": "kind='max_wall_time' only — ISO 8601 duration (e.g. 'PT2H')."},
        "metric": {"type": "string", "minLength": 1, "description": "kind in {'convergence_window', 'target_condition'} — evaluation key to read."},
        "window": {"type": "integer", "minimum": 1, "description": "kind='convergence_window' only — trailing window of integrated variants."},
        "threshold": {"type": "number", "description": "kind='target_condition' only — comparison threshold."},
        "direction": {"enum": ["maximize", "minimize"], "description": "kind in {'convergence_window', 'target_condition'} — direction of optimization. Defaults to 'maximize'."}
      },
      "allOf": [
        {"if": {"properties": {"kind": {"const": "max_variants"}},      "required": ["kind"]}, "then": {"required": ["kind", "target"]}},
        {"if": {"properties": {"kind": {"const": "max_wall_time"}},     "required": ["kind"]}, "then": {"required": ["kind", "duration"]}},
        {"if": {"properties": {"kind": {"const": "convergence_window"}},"required": ["kind"]}, "then": {"required": ["kind", "metric", "window"]}},
        {"if": {"properties": {"kind": {"const": "target_condition"}},  "required": ["kind"]}, "then": {"required": ["kind", "metric", "threshold"]}}
      ]
    },

    "max_quiescent_iterations": {
      "type": "integer",
      "minimum": 2,
      "description": "Orchestrator quiescence budget — exit after N consecutive no-progress iterations (see 03-roles.md §3.1). MUST be >= 2; N=1 risks exiting while a worker is mid-submit. When omitted, the reference orchestrator uses 3 (the manual-UI Compose stack overrides to 30 — see operator playbook). Implementation-defined: the spec defines the orchestrator's quiescent-exit shape but not the exact iteration count."
    },

    "ideas_per_ideation": {
      "type": "integer",
      "minimum": 1,
      "description": "How many ideas each ideation task asks the ideator host to produce. Default: 1. Forwarded to the spawned ideation_command via the EDEN_IDEAS_PER_IDEATION env variable (see 03-roles.md §2 ideation contract)."
    },

    "ideation_task_deadline": {
      "type": "number",
      "exclusiveMinimum": 0,
      "description": "Seconds the ideator host waits for a single ideation_command invocation to produce its idea-list payload. Default: 120.0. Implementation-defined; bounds the per-task worker-host SLA."
    },

    "execution_task_deadline": {
      "type": "number",
      "exclusiveMinimum": 0,
      "description": "Seconds the executor host waits for a single execution_command invocation to write its variant. Default: 600.0."
    },

    "evaluation_task_deadline": {
      "type": "number",
      "exclusiveMinimum": 0,
      "description": "Seconds the evaluator host waits for a single evaluation_command invocation to produce its metrics. Default: 300.0."
    }
  }
}
```

All six are top-level optional fields (the existing `extra="allow"` posture keeps unknown additions tolerated). The schema uses `format: "duration"` for `max_wall_time.duration` — Pydantic supports ISO 8601 `timedelta` parsing natively (`PT2H`, `P1D`, etc.). Specifying it as a string in the YAML is more operator-friendly than three separate fields (`hours` + `minutes` + `seconds`).

**Why the discriminated-union for `termination_policy` and not for the four numeric fields?** The four numeric fields (`max_quiescent_iterations`, `ideas_per_ideation`, `*_task_deadline`) are scalars with a single semantic — there is no "policy" choice; they're parameters. The discriminated-union pattern is overkill for them.

### 3.2 Pydantic binding (`reference/packages/eden-contracts/src/eden_contracts/config.py`)

Six additions to `ExperimentConfig`. The discriminated-union for `TerminationPolicyConfig` mirrors the `IdeationPolicyConfig` shape #133 lands:

```python
# Discriminated union, parallel to IdeationPolicyConfig.
class NeverTerminateConfig(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")
    kind: Literal["never_terminate"]

class MaxVariantsTerminationConfig(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")
    kind: Literal["max_variants"]
    target: Annotated[int, Field(ge=1)]

class MaxWallTimeTerminationConfig(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")
    kind: Literal["max_wall_time"]
    duration: timedelta  # Pydantic accepts ISO 8601 strings natively.

class ConvergenceWindowTerminationConfig(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")
    kind: Literal["convergence_window"]
    metric: Annotated[str, Field(min_length=1)]
    window: Annotated[int, Field(ge=1)]
    direction: Direction = "maximize"

class TargetConditionTerminationConfig(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")
    kind: Literal["target_condition"]
    metric: Annotated[str, Field(min_length=1)]
    threshold: float
    direction: Direction = "maximize"

TerminationPolicyConfig = Annotated[
    NeverTerminateConfig | MaxVariantsTerminationConfig
    | MaxWallTimeTerminationConfig | ConvergenceWindowTerminationConfig
    | TargetConditionTerminationConfig,
    Field(discriminator="kind"),
]

# In ExperimentConfig:
class ExperimentConfig(BaseModel):
    # … existing fields …
    termination_policy: Annotated[TerminationPolicyConfig | None, NotNone] = None
    max_quiescent_iterations: Annotated[int | None, NotNone, Field(ge=2)] = None
    ideas_per_ideation: Annotated[int | None, NotNone, Field(ge=1)] = None
    ideation_task_deadline: Annotated[float | None, NotNone, Field(gt=0)] = None
    execution_task_deadline: Annotated[float | None, NotNone, Field(gt=0)] = None
    evaluation_task_deadline: Annotated[float | None, NotNone, Field(gt=0)] = None
```

Schema↔model parity is enforced by the existing parity test ([`reference/packages/eden-contracts/tests/test_schema_parity.py`](../../reference/packages/eden-contracts/tests/test_schema_parity.py)). New fixtures land in `tests/cases.py` per AGENTS.md's "Adding or extending a JSON Schema + Pydantic binding" discipline — for each field, at least one accept and one reject fixture per constraint (kind-required, parameter-required-per-kind, minimum/maximum, format). For `termination_policy`, the round-trip test (`tests/test_roundtrip.py`) covers each discriminant variant.

The mandatory-when-`dispatch_mode.termination == "auto"` cross-field validation lives in a Pydantic `@model_validator(mode="after")` on `ExperimentConfig`, not in the JSON Schema (cross-field conditional-required is awkward in JSON Schema and the parity tests would have to special-case it). The validator's reject fixture: `dispatch_mode.termination = "auto"` + missing `termination_policy` raises `ValidationError`.

### 3.3 Factory: `eden_dispatch.build_termination_policy()`

Mirror #133's `build_policy()` for ideation. New function in [`reference/packages/eden-dispatch/src/eden_dispatch/termination.py`](../../reference/packages/eden-dispatch/src/eden_dispatch/termination.py):

```python
def build_termination_policy(config: TerminationPolicyConfig | None) -> TerminationPolicy:
    """Map a declarative TerminationPolicyConfig to a callable TerminationPolicy.

    Pre-dispatch_mode.termination='auto' shipment, this returns
    never_terminate when config is None — same default as the retired
    --termination-policy flag.
    """
    if config is None or config.kind == "never_terminate":
        return never_terminate
    if config.kind == "max_variants":
        return max_variants_policy(target=config.target)
    if config.kind == "max_wall_time":
        return max_wall_time_policy(duration=config.duration)
    if config.kind == "convergence_window":
        return convergence_window_policy(
            metric=config.metric, window=config.window, direction=config.direction,
        )
    if config.kind == "target_condition":
        return target_condition_policy(
            metric=config.metric, threshold=config.threshold, direction=config.direction,
        )
    raise ValueError(f"unknown termination_policy kind: {config.kind!r}")
```

The four reference policies (`max_variants_policy`, `max_wall_time_policy`, `convergence_window_policy`, `target_condition_policy`) are unchanged. The deprecated `env_max_variants_policy` factory + `EDEN_TERMINATION_MAX_VARIANTS` env var are **removed** — their purpose was bridging the pre-12a-3 config-field-removal to the post-12a-3 callable shape; this chunk restores the config-field shape (under a different name) and the env-var bridge is no longer needed. Removing it tightens the surface.

### 3.4 Service CLI changes

**Orchestrator** ([`reference/services/orchestrator/src/eden_orchestrator/cli.py`](../../reference/services/orchestrator/src/eden_orchestrator/cli.py)):

- Drop `--termination-policy` argparse entry (lines 150-166).
- Drop `--max-quiescent-iterations` argparse entry (lines 188-196).
- Drop `_resolve_termination_policy()` + the `_resolve_factory_callable` helper if `--ideation-policy` is also gone post-#133 (it is — #133 drops `_resolve_ideation_policy`). Net: the entire module:callable resolution surface goes.
- Read `config.termination_policy` from the loaded experiment-config (already loaded post-#133 via `args.experiment_config`); call `build_termination_policy(config.termination_policy)`.
- Read `config.max_quiescent_iterations` with `or 3` fallback (the existing reference default).
- The `--lease-duration-seconds` flag stays (per §1 disposition).

**Ideator-host** ([`reference/services/ideator/src/eden_ideator_host/cli.py`](../../reference/services/ideator/src/eden_ideator_host/cli.py)):

- Drop `--ideas-per-ideation` argparse entry (lines 65-69).
- Drop `--ideation-task-deadline` argparse entry (line 120).
- In `_run_subprocess_mode` after `load_experiment_config(args.experiment_config)`, read `config.ideas_per_ideation` (default 1) and `config.ideation_task_deadline` (default 120.0); plumb into the existing `run_ideator_loop(ideas_per_ideation=…)` and `subprocess_config(task_deadline=…)` call sites unchanged.
- The scripted-mode branch (which does NOT load the experiment-config — it has no `--experiment-config` arg in scripted mode per the lines 131-134 mode-conditional check) keeps the default `ideas_per_ideation=1` (unconfigurable in scripted mode; this matches today's no-flag default and is not a regression).

**Executor-host** ([`reference/services/executor/src/eden_executor_host/cli.py`](../../reference/services/executor/src/eden_executor_host/cli.py)):

- Drop `--execution-task-deadline` argparse entry (line 96).
- After `load_experiment_config(args.experiment_config)`, read `config.execution_task_deadline` (default 600.0); plumb into `ExecutorSubprocessConfig(task_deadline=…)`.

**Evaluator-host** ([`reference/services/evaluator/src/eden_evaluator_host/cli.py`](../../reference/services/evaluator/src/eden_evaluator_host/cli.py)):

- Drop `--evaluation-task-deadline` argparse entry (line 100).
- After `load_experiment_config(args.experiment_config)`, read `config.evaluation_task_deadline` (default 300.0); plumb into `EvaluatorSubprocessConfig(task_deadline=…)`.

**Web-ui** (no change): `--claim-ttl-seconds` stays per §1 disposition.

### 3.5 Compose + setup-experiment changes

- [`reference/compose/compose.yaml`](../../reference/compose/compose.yaml): Drop the orchestrator's `--termination-policy ${EDEN_TERMINATION_POLICY:-…}` and `--max-quiescent-iterations ${EDEN_MAX_QUIESCENT_ITERATIONS:-30}` lines (lines 281-289). Drop the `EDEN_TERMINATION_MAX_VARIANTS` env-var (line 300). Drop the worker hosts' deadline flags wherever they appear in compose.yaml.
- [`reference/compose/compose.multi-orchestrator.yaml`](../../reference/compose/compose.multi-orchestrator.yaml): Drop the `--max-quiescent-iterations` override (line 49) — multi-orchestrator quiescence is now per-experiment per the YAML field.
- [`reference/compose/.env.example`](../../reference/compose/.env.example): Drop `EDEN_TERMINATION_POLICY` / `EDEN_TERMINATION_MAX_VARIANTS` / `EDEN_MAX_QUIESCENT_ITERATIONS` entries. Document the smoke-script-managed YAML inject.
- [`reference/scripts/setup-experiment/setup-experiment.sh`](../../reference/scripts/setup-experiment/setup-experiment.sh): The `--ideas-per-ideation <N>` flag (currently plumbed from the deployment-CLI to compose env per [`user-guide.md:116`](../user-guide.md)) becomes obsolete — operators set `ideas_per_ideation: N` in the YAML. Drop the flag entirely; emit a one-line error if it's still passed (clean-break, no compat shim).
- Smoke scripts ([`smoke.sh`](../../reference/compose/healthcheck/smoke.sh) / [`smoke-manual-mode.sh`](../../reference/compose/healthcheck/smoke-manual-mode.sh) / [`smoke-subprocess.sh`](../../reference/compose/healthcheck/smoke-subprocess.sh) / [`smoke-subprocess-docker.sh`](../../reference/compose/healthcheck/smoke-subprocess-docker.sh) / [`smoke-multi-orchestrator.sh`](../../reference/compose/healthcheck/smoke-multi-orchestrator.sh) / [`e2e.sh`](../../reference/compose/healthcheck/e2e.sh) / [`smoke-checkpoint.sh`](../../reference/compose/healthcheck/smoke-checkpoint.sh)): each smoke script today copies the fixture experiment-config and appends an `ideation_policy:` block (per #133's pattern); the same append-loop adds `termination_policy`, `max_quiescent_iterations`, `*_task_deadline`, and `ideas_per_ideation` as needed for the smoke's workload shape. Specifically `max_quiescent_iterations: 30` for every smoke (matches the previous compose-level default); the `e2e.sh` smoke that wants a higher quiescence budget for manual-UI steps continues to set it via the YAML.

### 3.6 Spec prose updates

- [`spec/v0/02-data-model.md`](../../spec/v0/02-data-model.md) §2 (Experiment config): add the six new fields to the field table. Same prose discipline as #133's `ideation_policy` addition (`02-data-model.md §2.4`).
- [`spec/v0/03-roles.md`](../../spec/v0/03-roles.md) §6.2 decision-type 0 (termination): swap the "deployment-supplied termination policy callable" phrasing to "the experiment-config-supplied `termination_policy` (§2 of chapter 02), mapped by the implementation to a `TerminationPolicy` callable". Keep the existing "implementation-defined" caveat: a conforming impl MAY accept additional policy kinds beyond the five named.
- [`spec/v0/03-roles.md`](../../spec/v0/03-roles.md) §3.1 (orchestrator main loop): add one-line cross-reference to `max_quiescent_iterations` for the quiescent-exit shape.
- No conformance-chapter (§09) changes — these fields are all implementation-defined per the chapter 03 §6 framing.

### 3.7 Docs

- [`docs/user-guide.md`](../user-guide.md) §2 experiment-config table: add six rows for the new fields. Add a worked-example YAML block at the end of §2 showing one experiment with all six fields populated (and the existing `ideation_policy` block from #133 for contrast).
- [`docs/operations/experiment-lifecycle.md`](../operations/experiment-lifecycle.md): the existing operator playbook for `--termination-policy` migration moves from "set the env-var + point the orchestrator at the right factory" to "add a `termination_policy` block to the experiment config". Three worked recipes (`max_variants`, `max_wall_time`, `convergence_window`) — one each, replacing the current `module:callable` examples.
- [`docs/glossary.md`](../glossary.md): add a "termination_policy" entry under the "Lifecycle and termination" section + a brief note that the four reference policy kinds (`max_variants` / `max_wall_time` / `convergence_window` / `target_condition`) round-trip the pre-12a-3 schema fields' semantics under a new shape.
- [`CHANGELOG.md`](../../CHANGELOG.md) `[Unreleased]`: chunk-completion entry per the AGENTS.md discipline.
- [`docs/roadmap.md`](../roadmap.md): one-line entry under the appropriate phase pointing at this plan + the eventual PR.

## 4. Scope

In scope:

- 6 new fields in `spec/v0/schemas/experiment-config.schema.json` (`termination_policy` discriminated-union + 5 scalars).
- Corresponding Pydantic additions in `eden-contracts` (incl. parity-test fixtures, round-trip fixtures, cross-field validator for `termination_policy` required-when-`dispatch_mode.termination='auto'`).
- New factory `build_termination_policy()` in `eden-dispatch`; removal of `env_max_variants_policy()` + the `EDEN_TERMINATION_MAX_VARIANTS` env-var bridge.
- Orchestrator CLI drops `--termination-policy` + `--max-quiescent-iterations`; reads from config.
- Ideator-host CLI drops `--ideas-per-ideation` + `--ideation-task-deadline`; reads from config.
- Executor-host CLI drops `--execution-task-deadline`; reads from config.
- Evaluator-host CLI drops `--evaluation-task-deadline`; reads from config.
- Compose + setup-experiment + every smoke script reconciled to the new YAML-driven shape; no CLI fallback layer.
- Spec prose updates in chapters 02 + 03.
- User-guide + operations doc + glossary + CHANGELOG + roadmap updates.

Out of scope (deferred):

- `--lease-duration-seconds` promotion. Stays a deployment flag per §1.
- `--claim-ttl-seconds` promotion. Stays a deployment flag per §1.
- Per-experiment config resolution in multi-experiment mode — that's #214's surface; this chunk's `build_termination_policy(config)` factory is the shared hook #214 will call per-experiment.
- A "deployment defaults" file (e.g. `deployment-defaults.yaml`) that supplies per-experiment-field defaults. Out of scope; raise as a separate issue if a real deployment surfaces the need.
- Hot-reload of policies / deadlines mid-experiment. Same out-of-scope posture as #133's note: set at experiment creation, restart-orchestrator to pick up changes.
- Conformance-suite scenarios asserting any of the new field shapes. Not normative — implementation-defined per chapter 03 §6.

## 5. Naming map

Per [`docs/glossary.md`](../glossary.md) — fields use the artifact noun (gerund-as-noun for tasks) in snake_case; never verb-on-verb. All proposed names already conform.

| Old surface (deployment knob) | New surface (experiment-config field) |
|---|---|
| `--termination-policy <module:callable>` CLI flag (orchestrator) | `termination_policy: {kind: …, …}` block |
| `EDEN_TERMINATION_POLICY` env var | (retired — see above) |
| `EDEN_TERMINATION_MAX_VARIANTS` env var + `env_max_variants_policy()` factory | `termination_policy.kind = "max_variants"` + `target: N` |
| `--max-quiescent-iterations <N>` CLI flag (orchestrator) | `max_quiescent_iterations: <N>` scalar |
| `EDEN_MAX_QUIESCENT_ITERATIONS` env var | (retired) |
| `--ideas-per-ideation <N>` CLI flag (ideator-host) | `ideas_per_ideation: <N>` scalar |
| `--ideas-per-ideation <N>` setup-experiment flag | (retired — operator edits YAML directly) |
| `--ideation-task-deadline <S>` CLI flag (ideator-host) | `ideation_task_deadline: <S>` scalar |
| `--execution-task-deadline <S>` CLI flag (executor-host) | `execution_task_deadline: <S>` scalar |
| `--evaluation-task-deadline <S>` CLI flag (evaluator-host) | `evaluation_task_deadline: <S>` scalar |
| (intermediate during plan-draft) `*-deadline` | `*_task_deadline` — explicitly named with the gerund-as-noun task-kind prefix so the field name itself anchors to the spec's `ideation` / `execution` / `evaluation` task-kind vocabulary. The current CLI flag `--ideation-task-deadline` already follows the pattern; the YAML field preserves it. |
| Module-path callable resolver `_resolve_termination_policy` / `_resolve_ideation_policy` / `_resolve_factory_callable` | (retired — replaced by `build_termination_policy(config)` and `build_policy(config)` factories) |

Vocabulary check against the glossary canon: all six field names are gerund-or-noun-rooted (`termination_policy`, `ideation_*`, `execution_*`, `evaluation_*`) with no verb-on-verb shape. The discriminator string values (`never_terminate`, `max_variants`, `max_wall_time`, `convergence_window`, `target_condition`) preserve the existing function names exactly so the schema↔code lookup is mechanical.

**Rename-discipline citation guard.** `scripts/check-rename-discipline.py` does not currently include any of these identifiers in its retire-list; this plan adds none. The script is unaffected by this chunk.

## 6. Migration / cleanup

Per CLAUDE.md's no-backwards-compat-shims-in-pre-user posture:

- **Deployments that today pass `--termination-policy` / `--max-quiescent-iterations` / `--ideas-per-ideation` / `*-task-deadline` on the CLI break** at the next orchestrator/host start after the impl PR merges. The break is immediate and loud (argparse "unrecognized arguments" error) rather than silent.
- **`EDEN_TERMINATION_POLICY` + `EDEN_TERMINATION_MAX_VARIANTS` + `EDEN_MAX_QUIESCENT_ITERATIONS` env vars are retired**. `.env.example` no longer documents them; existing operator `.env` files carrying them are ignored at compose-up (compose doesn't error on unknown env entries; the orchestrator just doesn't read them).
- **Smoke scripts ship updated experiment-config YAML inline.** No "transition period" where some smokes use the old shape and some the new; one PR flips them all.
- **No CHANGELOG migration note for end users** — EDEN has no external user deployments. The CHANGELOG `[Unreleased]` entry calls out the flag retirements + the per-experiment shape but doesn't write a migration recipe (no one to migrate).
- **Documentation:** `docs/operations/experiment-lifecycle.md` is the only operator-playbook surface that documents the `--termination-policy` flag; rewrite it to use the YAML shape (with worked examples for each of the five policy kinds).

The "abort if both the flag and the YAML field are set" question doesn't arise because the flags are gone. Anyone running `--termination-policy` against the impl-branch orchestrator gets a parser error at startup — the right loud-failure shape.

## 7. Conformance impact

None. The six promoted fields are all implementation-defined per chapter 03 §6 (orchestrator decision-type contracts are specified, but the policies that drive them are not; worker-host SLA-style deadlines are not protocol surface). No new conformance-chapter §5 group row; no new scenario citations; the existing `check_citations.py` passes unchanged.

The audit that matters: chapter 02 §2 (experiment-config schema overview) gains six new field entries in its description table — same shape as #133's `ideation_policy` addition. The schema parity test ([`reference/packages/eden-contracts/tests/test_schema_parity.py`](../../reference/packages/eden-contracts/tests/test_schema_parity.py)) is the mechanical gate that keeps schema + Pydantic + the conformance-relevant invariants in lockstep; no new conformance scenarios.

Wire conformance (chapter 7) is unaffected: none of the promoted fields appear on the wire (they shape orchestrator / host behavior, not request/response payloads).

## 8. Chunked execution plan

Single impl PR is fine for the surface area (similar scale to #133). The wave-internal order matters for landing-on-green:

**Wave 1 — schema + Pydantic + factory (no service wiring yet):**

1. `spec/v0/schemas/experiment-config.schema.json` — add six new fields.
2. `eden-contracts` — add `TerminationPolicyConfig` discriminated union + scalar fields to `ExperimentConfig`; add the cross-field validator.
3. `eden-contracts/tests/cases.py` + `test_schema_parity.py` + `test_roundtrip.py` — fixtures per AGENTS.md's schema-parity discipline.
4. `eden-dispatch` — add `build_termination_policy()`; remove `env_max_variants_policy()`.

**Validation gate at end of wave 1:** `uv run pytest -q reference/packages/eden-contracts` + `uv run pytest -q reference/packages/eden-dispatch` + `pipx run check-jsonschema --check-metaschema spec/v0/schemas/experiment-config.schema.json` + `uv run ruff check` + `uv run pyright`. Wave 1 lands the schema + binding + factory in a state where existing services still pass their own flags (the new fields are optional and unused). PR can compile and pass these tests before service wiring.

**Wave 2 — service wiring + CLI flag removal:**

1. Orchestrator: drop the two flags + the factory-callable helpers; read fields from `config`; call `build_termination_policy()`.
2. Ideator-host / executor-host / evaluator-host: drop deadline flags + `--ideas-per-ideation`; read fields from `config`.

**Validation gate at end of wave 2:** Full `uv run pytest -q` (every existing service test that constructs a CLI invocation needs auditing — the dropped flags become "unrecognized arguments" failures).

**Wave 3 — Compose + setup-experiment + smoke-script reconciliation:**

1. `compose.yaml` + `compose.multi-orchestrator.yaml` + `.env.example` flag/env-var drops.
2. `setup-experiment.sh` drop the `--ideas-per-ideation` flag + its compose-env plumbing.
3. Every smoke script's experiment-config-YAML append loop gains the new field shapes (incl. `max_quiescent_iterations: 30` matching the old Compose-level default).

**Validation gate at end of wave 3:** The full smoke set per AGENTS.md "Commands" — `smoke.sh`, `smoke-subprocess.sh`, `smoke-subprocess-docker.sh`, `smoke-manual-mode.sh`, `smoke-multi-orchestrator.sh`, `smoke-checkpoint.sh`, `e2e.sh`. **All seven are load-bearing for this chunk** because the CLI-flag removal touches every service the smokes spin up; a smoke that still passes the dropped flag fails loud.

**Wave 4 — docs + CHANGELOG + roadmap:**

1. Spec prose updates (`02-data-model.md` §2 + `03-roles.md` §3.1 + §6.2).
2. `docs/user-guide.md` §2 + `docs/operations/experiment-lifecycle.md` + `docs/glossary.md`.
3. `CHANGELOG.md` `[Unreleased]` chunk-completion entry; `docs/roadmap.md` one-liner; AGENTS.md "Current phase" paragraph update if appropriate (probably not — this chunk doesn't move a phase).

**Final validation gate:** the literal "Commands" section of AGENTS.md, in order, end-to-end. Including `markdownlint-cli2` for every touched markdown; `spec-xref-check.py`; `check-rename-discipline.py`; full `pytest`; full `conformance/`; the seven smokes.

## 9. Files to touch

**Spec + schema (3 files):**

- [`spec/v0/schemas/experiment-config.schema.json`](../../spec/v0/schemas/experiment-config.schema.json)
- [`spec/v0/02-data-model.md`](../../spec/v0/02-data-model.md)
- [`spec/v0/03-roles.md`](../../spec/v0/03-roles.md)

**Pydantic + dispatch (4 files):**

- [`reference/packages/eden-contracts/src/eden_contracts/config.py`](../../reference/packages/eden-contracts/src/eden_contracts/config.py)
- [`reference/packages/eden-contracts/tests/cases.py`](../../reference/packages/eden-contracts/tests/cases.py)
- [`reference/packages/eden-contracts/tests/test_roundtrip.py`](../../reference/packages/eden-contracts/tests/test_roundtrip.py)
- [`reference/packages/eden-dispatch/src/eden_dispatch/termination.py`](../../reference/packages/eden-dispatch/src/eden_dispatch/termination.py)

**Service CLIs (4 files):**

- [`reference/services/orchestrator/src/eden_orchestrator/cli.py`](../../reference/services/orchestrator/src/eden_orchestrator/cli.py)
- [`reference/services/ideator/src/eden_ideator_host/cli.py`](../../reference/services/ideator/src/eden_ideator_host/cli.py)
- [`reference/services/executor/src/eden_executor_host/cli.py`](../../reference/services/executor/src/eden_executor_host/cli.py)
- [`reference/services/evaluator/src/eden_evaluator_host/cli.py`](../../reference/services/evaluator/src/eden_evaluator_host/cli.py)

**Service tests** (audit each that constructs a CLI invocation against any dropped flag):

- `reference/services/orchestrator/tests/test_*.py`
- `reference/services/ideator/tests/test_*.py`
- `reference/services/executor/tests/test_*.py`
- `reference/services/evaluator/tests/test_*.py`

**Compose + setup-experiment + smokes (10 files):**

- [`reference/compose/compose.yaml`](../../reference/compose/compose.yaml)
- [`reference/compose/compose.multi-orchestrator.yaml`](../../reference/compose/compose.multi-orchestrator.yaml)
- [`reference/compose/.env.example`](../../reference/compose/.env.example)
- [`reference/scripts/setup-experiment/setup-experiment.sh`](../../reference/scripts/setup-experiment/setup-experiment.sh)
- [`reference/compose/healthcheck/smoke.sh`](../../reference/compose/healthcheck/smoke.sh)
- [`reference/compose/healthcheck/smoke-manual-mode.sh`](../../reference/compose/healthcheck/smoke-manual-mode.sh)
- [`reference/compose/healthcheck/smoke-subprocess.sh`](../../reference/compose/healthcheck/smoke-subprocess.sh)
- [`reference/compose/healthcheck/smoke-subprocess-docker.sh`](../../reference/compose/healthcheck/smoke-subprocess-docker.sh)
- [`reference/compose/healthcheck/smoke-multi-orchestrator.sh`](../../reference/compose/healthcheck/smoke-multi-orchestrator.sh)
- [`reference/compose/healthcheck/smoke-checkpoint.sh`](../../reference/compose/healthcheck/smoke-checkpoint.sh)
- [`reference/compose/healthcheck/e2e.sh`](../../reference/compose/healthcheck/e2e.sh)

**Docs (5 files):**

- [`docs/user-guide.md`](../user-guide.md)
- [`docs/operations/experiment-lifecycle.md`](../operations/experiment-lifecycle.md)
- [`docs/glossary.md`](../glossary.md)
- [`CHANGELOG.md`](../../CHANGELOG.md)
- [`docs/roadmap.md`](../roadmap.md)

Approximate diff size: ~120 lines schema, ~140 lines Pydantic + tests, ~70 lines factory + factory-tests, ~80 lines service-CLI deletions/additions (mostly deletions), ~60 lines Compose/setup-experiment cleanup, ~110 lines smoke-script appends, ~180 lines spec/docs/CHANGELOG. Total ~760 lines added, ~280 lines removed; net additive ~480 lines.

## 10. Verification gates

Run before any push from impl branch — literally per AGENTS.md "Commands":

```text
uv sync
uv run ruff check .
uv run pyright
uv run pytest -q
uv run pytest -q conformance/
uv run python conformance/src/conformance/tools/check_citations.py
npx --yes markdownlint-cli2@0.14.0 "**/*.md" "#node_modules" "#.venv" "#docs/archive/**" "#docs/plans/review/**"
pipx run 'check-jsonschema==0.29.4' --check-metaschema spec/v0/schemas/*.schema.json
pipx run 'check-jsonschema==0.29.4' --schemafile spec/v0/schemas/experiment-config.schema.json tests/fixtures/experiment/.eden/config.yaml
python3 scripts/spec-xref-check.py
python3 scripts/check-rename-discipline.py
python3 scripts/check-complexity.py
bash reference/compose/healthcheck/smoke.sh
bash reference/compose/healthcheck/smoke-subprocess.sh
bash reference/compose/healthcheck/smoke-subprocess-docker.sh
bash reference/compose/healthcheck/smoke-manual-mode.sh
bash reference/compose/healthcheck/smoke-multi-orchestrator.sh
bash reference/compose/healthcheck/smoke-checkpoint.sh
bash reference/compose/healthcheck/e2e.sh
```

All seven smoke scripts are load-bearing because the orchestrator + every worker host's CLI surface changes; a smoke that doesn't reconcile its experiment-config-YAML append loop will fail loud either at setup-experiment time or orchestrator-startup time.

The schema↔Pydantic parity test in `reference/packages/eden-contracts/tests/test_schema_parity.py` is the mechanical guard against the most common bug-class on this surface: forgetting to teach one side about a new field. The `tests/cases.py` fixture audit (per AGENTS.md "Adding or extending a JSON Schema + Pydantic binding") gates that for every new field there is at least one accept + one reject case per constraint.

## 11. Risks / things to watch

- **Rebase ordering against #133.** PR #215 ("Fix #133: surface ideation policy in experiment config") is the direct prerequisite — it lands the `--experiment-config`-required orchestrator surface, the `IdeationPolicyConfig` discriminated-union, and the `build_policy()` factory pattern this chunk extends. If #215 hasn't merged at impl-start, rebase impl branch against `main` after #215 lands. Do NOT start impl against the current `main` — too much surface drift to absorb cleanly mid-PR.

- **Cross-field `dispatch_mode.termination == "auto"` ↔ `termination_policy` required validation.** The mandatory-when-auto rule has to live in the Pydantic layer (a `@model_validator(mode="after")`) because JSON Schema's conditional-required across two siblings is awkward (`allOf`-`if`-`then` over `dispatch_mode.termination`). Parity-test discipline applies — for every accept/reject pair on this validator, both schema-side and model-side must agree on the outcome. The schema-side test uses the existing `jsonschema` library's `if-then` support; the model-side uses the validator. If the two disagree (e.g. schema accepts a config the model rejects), the parity test fails — that's the gate.

- **Smoke-script YAML-append-loop scale.** Every smoke today copies the fixture experiment-config and appends an `ideation_policy:` block. This chunk extends each append loop with five more fields (`termination_policy`, four scalars). The append-loop is hand-written bash heredoc; risk of typo or yaml-indentation slip. Mitigation: factor the append loop into a small bash helper sourced by every smoke, so the YAML shape lives in one place. Same posture as the `setup-experiment.sh` helpers already do for `.env` writes.

- **Manual-UI Compose `max_quiescent_iterations` budget.** Today's [`compose.yaml:270-278`](../../reference/compose/compose.yaml) sets `EDEN_MAX_QUIESCENT_ITERATIONS:-30` with an explicit comment that manual-UI sessions want a much higher value. After promotion, the experiment-config YAML carries `max_quiescent_iterations: 30` as the default smoke value, and manual-UI experiments are expected to set a high value (3600+ for a human-paced session) in their own YAML. The retired `EDEN_MAX_QUIESCENT_ITERATIONS` deployment-default is replaced by per-experiment authoring. Document this swap explicitly in `docs/operations/experiment-lifecycle.md` so the manual-UI operator workflow doesn't silently break.

- **Cherry-pick contamination from impl branch into the plan PR.** Per the [`AGENTS.md`](../../AGENTS.md) pitfall on cherry-pick contamination from dev-only workarounds: the impl branch's WIP commits during smoke-debugging may bump `max_quiescent_iterations` for manual testing, and that bumped value is now in the YAML rather than an env-var — easier to accidentally cherry-pick into a real PR branch. Discipline: scrub WIP commits before squash/merge.

- **`env_max_variants_policy()` removal is a real surface drop.** That factory + the `EDEN_TERMINATION_MAX_VARIANTS` env-var was the bridge from pre-12a-3 schema fields to the post-12a-3 callable shape. After this chunk it's gone. If a deployment was using it (none in `main`, but a downstream fork might), they need to migrate to `termination_policy: {kind: "max_variants", target: N}` in YAML. Document explicitly in CHANGELOG.

- **Pydantic `timedelta` parsing of ISO 8601 durations.** Pydantic accepts ISO 8601 duration strings (`PT2H`) into `timedelta` natively. The JSON Schema-side validation uses the standard library's `isodate` / regex check (the existing `_common.py` `FormatChecker` plumbing per AGENTS.md "Adding or extending a JSON Schema + Pydantic binding"). Risk: format-handler asymmetry on `duration` format. Mitigation: add the `duration` format handler on the schema side using `pydantic.TypeAdapter(timedelta).validate_python` so the two sides resolve via the same code path. Add accept fixture (`PT2H`), reject fixture (`2h` — not ISO 8601), and a per-side equivalence assertion.

- **Reference test e2e timing.** The orchestrator/ideator/executor/evaluator e2e tests at [`reference/services/orchestrator/tests/test_subprocess_e2e.py`](../../reference/services/orchestrator/tests/test_subprocess_e2e.py) construct deeply argv-dependent CLI invocations. Every test that today passes any of the dropped flags will fail loud on impl branch; audit all `test_*.py` files in the four affected services systematically. Don't catch this at smoke time — catch it in wave 2's `uv run pytest -q` gate.

- **`build_termination_policy()` vs `build_policy()` naming.** #133 names the ideation factory `build_policy()` (not `build_ideation_policy()`). For symmetry this chunk could call the termination factory `build_termination_policy()` (asymmetric — namespacing the new one but not the existing one), or rename #133's `build_policy()` → `build_ideation_policy()` in lockstep (cleaner but rebases #133's work). Recommendation: take the asymmetric name now (`build_termination_policy()`), file a follow-up issue for the rename to symmetric pair (`build_ideation_policy()` / `build_termination_policy()`). The rename is mechanical and not blocking.

- **Naming asymmetry across `*_task_deadline` vs `max_quiescent_iterations`.** Three of the four scalar fields use the `<task-kind>_task_deadline` pattern; the fourth uses `max_quiescent_iterations` with no role/kind prefix. The asymmetry is real — `max_quiescent_iterations` is an orchestrator-loop budget, not a per-task budget, so prefixing with `orchestrator_` would be misleading (it's already implicitly orchestrator-only). The unprefixed name reads cleaner and matches the existing CLI flag spelling. Accept the asymmetry; document in the spec prose that this field is the orchestrator's quiescent-exit budget (per chapter 03 §3.1).

## 12. Sequence within the chunk

1. **Rebase against `main` at impl-start.** Especially against #215 ("Fix #133"). Resolve any conflicts in `experiment-config.schema.json`, `config.py`, the orchestrator CLI, and the smoke scripts.
2. **Wave 1 — schema + Pydantic + factory.** Land in a state where `uv run pytest -q` passes with the new fields wired but unused.
3. **Wave 2 — service wiring.** Drop CLI flags; read from config; full pytest passes.
4. **Wave 3 — Compose + smokes.** All seven smokes pass locally.
5. **Wave 4 — docs.** Spec prose, user-guide, operations playbook, glossary, CHANGELOG, roadmap.
6. **Codex-review to convergence.** Plan PR's codex-review (this PR) iterates 3-5 rounds; impl PR's codex-review iterates separately. Issues to expect: per-flag disposition pushback (see §1's debate-flagged rows); discriminated-union variant exhaustiveness on `termination_policy`; cross-field validator scope.
7. **Open impl PR.** Body: per-flag disposition summary, file list with one-line description, validation-gate checklist, codex round count.

The plan PR and impl PR are separate surfaces; operator merges each after codex convergence.

## 13. Out of scope (followups to file as issues if not already)

- **Per-experiment config resolution in multi-experiment mode.** Tracked in [#214](https://github.com/ealt/eden/issues/214). This chunk's `build_termination_policy(config)` factory is the shared hook #214 will call per-experiment; same posture as #133's `build_policy(config)`.
- **Symmetric naming `build_policy()` → `build_ideation_policy()`.** New follow-up issue; file at chunk-completion review per AGENTS.md deferral-tracking discipline.
- **Deployment-level defaults file** (`deployment-defaults.yaml`) that supplies per-experiment-field defaults across all experiments in a deployment. Conceptually clean but no concrete operator demand; file as an issue if/when a deployment surfaces the need.
- **Promotion of `--lease-duration-seconds` and `--claim-ttl-seconds`.** Both held back per §1 disposition. If the disposition is wrong, file follow-up issues.
- **Custom callable `termination_policy.kind: "custom"` with a `module:callable` parameter** for advanced deployments that want arbitrary Python callables. Out of scope per #133's security posture; revisit if a real deployment surfaces it.

## 14. Estimated effort

| Activity | Estimate |
|---|---|
| Wave 1 (schema + Pydantic + factory) | ~1 day |
| Wave 2 (service CLI drops + per-service test audit) | ~0.75 day |
| Wave 3 (Compose + setup-experiment + smoke YAML-append loops) | ~0.5 day |
| Wave 4 (docs + CHANGELOG + roadmap) | ~0.5 day |
| Validation gates (full Commands gate, including 7 smokes) | ~0.5 day |
| Codex-review iterations (plan PR + impl PR, ~2-3 rounds each) | ~1 day |
| **Total** | **~4.25 days** |

Comparable to #133's effort: #133 was one flag + one schema block + one factory + one orchestrator-CLI rewire + smoke YAML-append loops; this chunk is 1.5 schema blocks + 4 service-CLI rewires + a larger smoke-loop edit + more doc surface. Codex-review is the dominant variable.
