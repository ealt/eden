# eden-checkpoint

Reader and writer for the EDEN portable checkpoint format defined in
[`spec/v0/10-checkpoints.md`](../../../spec/v0/10-checkpoints.md).

This is a **reference binding**, not the authoritative definition. The
on-wire format (tar envelope + manifest + JSONL files + git bundle +
content-addressed artifacts) is specified by the chapter; this package
is one valid Python implementation that produces and consumes it.

A third-party EDEN implementation in another language MUST NOT depend
on this package — it derives its own reader/writer from the spec.

## Targeted spec version

`eden-protocol/v0` — see [`spec/v0/10-checkpoints.md`](../../../spec/v0/10-checkpoints.md).

The package emits and accepts `checkpoint_format_version == "1"` (the
only format version defined under spec v0).

## Public API

| Name | What it is |
|---|---|
| `CheckpointManifest` | Pydantic model for `manifest.json` (mirror of `checkpoint-manifest.schema.json`). |
| `ManifestCounts`, `ManifestFiles`, `ExporterInfo` | Sub-models of the manifest. |
| `CheckpointWriter` | Streaming writer; context manager. Append entries (manifest, JSONL files, repo bundle, artifacts) into a tar stream. |
| `CheckpointReader` | Reader rooted at an extracted directory; exposes the parsed manifest plus per-file accessors. |
| `extract_checkpoint(stream, dest_dir)` | Untars a checkpoint archive into `dest_dir`; returns a `CheckpointReader`. |
| `CHECKPOINT_FORMAT_VERSION` | The format version this binding emits (currently `"1"`). |
| `CHECKPOINT_MEDIA_TYPE` | `application/x-eden-checkpoint+tar`. |
| `ARTIFACT_URI_PREFIX` | `checkpoint:sha256:`. |

Error types (mapped to the chapter 07 §9 error vocabulary by the wire layer):

- `CheckpointError` — base class.
- `CheckpointInvalid` — cross-reference validation failed; archive malformed.
- `UnsupportedCheckpointVersion` — `checkpoint_format_version` not recognized.
- `SpecVersionMismatch` — `spec_version` does not match the importer's spec.
- `ExperimentIdConflict` — manifest `experiment_id` collides with an existing experiment.
- `ExperimentIdMismatch` — `X-Eden-Experiment-Id` header disagrees with the manifest.

## Format reference

```text
<checkpoint>/
  manifest.json
  experiment-config.yaml
  experiment.json
  tasks.jsonl
  ideas.jsonl
  variants.jsonl
  submissions.jsonl
  events.jsonl
  workers.jsonl
  groups.jsonl
  repo.bundle
  artifacts/sha256/<hex>
```

See [`spec/v0/10-checkpoints.md`](../../../spec/v0/10-checkpoints.md) §3 for the directory contract, §5 for the manifest, §7 for content-addressed artifact addressing.
