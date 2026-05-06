# eden-contracts

Pydantic v2 bindings for the EDEN protocol wire formats. These models are **reference bindings**, not the authoritative definition:

- The authoritative definitions live in [`spec/v0/schemas/`](../../../spec/v0/schemas/) as JSON Schemas.
- The models here track the schemas. CI's `schema-parity` job enforces that every fixture accepted by the JSON Schema is also accepted by the model, and vice versa.
- A third-party EDEN implementation in another language MUST NOT depend on this package. Implementations derive their own bindings from the JSON Schemas.

## Targeted spec version

`eden-protocol/v0` — see [`../../../spec/v0/`](../../../spec/v0/).

## Exports

| Model | Schema |
|---|---|
| `ExperimentConfig`, `ObjectiveSpec` | `experiment-config.schema.json` |
| `EvaluationSchema` | `evaluation-schema.schema.json` |
| `Task` (discriminated: `IdeationTask` / `ExecutionTask` / `EvaluationTask`), `TaskClaim`, `IdeationPayload`, `ExecutionPayload`, `EvaluationPayload` | `task.schema.json` |
| `Event` | `event.schema.json` |
| `Idea` | `idea.schema.json` |
| `Variant` | `variant.schema.json` |

`Task` is a discriminated union (`Field(discriminator="kind")`); use `TaskAdapter` (a `pydantic.TypeAdapter`) to validate arbitrary task objects.

## Usage

```python
from eden_contracts import TaskAdapter, ExperimentConfig

task = TaskAdapter.validate_python({
    "task_id": "t-1",
    "kind": "ideation",
    "state": "pending",
    "payload": {"experiment_id": "exp-1"},
    "created_at": "2026-04-23T12:00:00Z",
    "updated_at": "2026-04-23T12:00:00Z",
})
```

## Extra fields

Top-level and payload models are configured with `extra="allow"`: any field the JSON Schema permits (the schemas do not set `additionalProperties: false`) is preserved on round-trip. Extra fields not named by the model are returned verbatim in `model_dump()`.

## Serializing back to wire format

Use `model.model_dump(mode="json", exclude_none=True)` when emitting JSON intended to validate against the schemas. The JSON Schemas treat optional fields as *absent-or-present*, not *nullable*; a default dump (which would include `"field": null` for absent optional fields) does not validate. CI's `schema-parity` job runs a round-trip test that dumps every accept fixture through the model and re-validates it against the schema with this flag.

## Constraints not expressible as pure schema

A handful of cross-field constraints are enforced by model validators because JSON Schema cannot (or can only clumsily) express them. These constraints are also present in the JSON Schema via `if/then/else`; the models and schemas agree on every fixture in `tests/fixtures/`.

- `Task`: `claim` MUST be present iff `state ∈ {claimed, submitted}`.
- `EvaluationSchema`: keys MUST NOT collide with reserved variant-object field names.
