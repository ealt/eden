"""Pydantic models for the portable-checkpoint manifest.

Mirrors ``spec/v0/schemas/checkpoint-manifest.schema.json``. CI's
schema-parity discipline keeps this in lockstep with the JSON Schema.
"""

from __future__ import annotations

from typing import Annotated, Final

from eden_contracts import DateTimeStr
from pydantic import BaseModel, ConfigDict, Field

CHECKPOINT_FORMAT_VERSION: Final[str] = "1"
"""The portable-checkpoint format version emitted by this binding.

Defined in ``spec/v0/10-checkpoints.md`` §5. An importer encountering a
manifest with a different value MUST reject the archive with
``eden://error/unsupported-checkpoint-version``.
"""

CHECKPOINT_SPEC_VERSION: Final[str] = "v0"
"""The EDEN spec version this binding targets.

An importer encountering a manifest with a different value MUST reject
the archive with ``eden://error/spec-version-mismatch``.
"""

CHECKPOINT_MEDIA_TYPE: Final[str] = "application/x-eden-checkpoint+tar"
"""The HTTP ``Content-Type`` carrying a portable-checkpoint archive."""

ARTIFACT_URI_PREFIX: Final[str] = "checkpoint:sha256:"
"""URI prefix for content-addressed artifact references inside an archive.

Per ``spec/v0/10-checkpoints.md`` §7, every ``artifacts_uri`` value
inside the JSONL files in a checkpoint MUST use this prefix; the importer
rewrites them to deployment-local URIs on commit.
"""


class ExporterInfo(BaseModel):
    """Informative metadata describing the producing implementation."""

    model_config = ConfigDict(strict=True, extra="allow")

    implementation: str | None = None
    """Free-form identifier (e.g., ``"eden-reference/0.x"``)."""

    atomicity_mechanism: str | None = None
    """Free-form indicator of which §6 atomicity strategy was used."""


class ManifestCounts(BaseModel):
    """Per-component object counts; consumers MAY use for early validation."""

    model_config = ConfigDict(strict=True, extra="allow")

    tasks: Annotated[int, Field(ge=0)]
    ideas: Annotated[int, Field(ge=0)]
    variants: Annotated[int, Field(ge=0)]
    submissions: Annotated[int, Field(ge=0)]
    events: Annotated[int, Field(ge=0)]
    workers: Annotated[int, Field(ge=0)]
    groups: Annotated[int, Field(ge=0)]


class ManifestFiles(BaseModel):
    """Per-component file paths within the archive (v0 fixed shape)."""

    model_config = ConfigDict(strict=True, extra="allow")

    experiment_config: Annotated[str, Field(min_length=1)]
    experiment: Annotated[str, Field(min_length=1)]
    tasks: Annotated[str, Field(min_length=1)]
    ideas: Annotated[str, Field(min_length=1)]
    variants: Annotated[str, Field(min_length=1)]
    submissions: Annotated[str, Field(min_length=1)]
    events: Annotated[str, Field(min_length=1)]
    workers: Annotated[str, Field(min_length=1)]
    groups: Annotated[str, Field(min_length=1)]
    repo_bundle: Annotated[str, Field(min_length=1)]
    artifacts_dir: Annotated[str, Field(min_length=1)]


DEFAULT_FILES: Final[ManifestFiles] = ManifestFiles(
    experiment_config="experiment-config.yaml",
    experiment="experiment.json",
    tasks="tasks.jsonl",
    ideas="ideas.jsonl",
    variants="variants.jsonl",
    submissions="submissions.jsonl",
    events="events.jsonl",
    workers="workers.jsonl",
    groups="groups.jsonl",
    repo_bundle="repo.bundle",
    artifacts_dir="artifacts/sha256",
)
"""The canonical file-path layout this binding emits.

Importers are not required to assume these names — they MUST read
``manifest.files`` and look at the values — but exporters in this
binding use this exact shape so the format mirrors
``spec/v0/10-checkpoints.md`` §3 verbatim.
"""


class CheckpointManifest(BaseModel):
    """Top-level descriptor for a portable-checkpoint archive.

    Mirrors ``spec/v0/schemas/checkpoint-manifest.schema.json``. The
    serialized form (``model_dump(mode="json")``) round-trips through
    the JSON Schema by construction; the parity test
    ``test_manifest_schema_parity`` pins this.
    """

    model_config = ConfigDict(strict=True, extra="allow")

    checkpoint_format_version: Annotated[str, Field(min_length=1)]
    spec_version: Annotated[str, Field(min_length=1)]
    experiment_id: Annotated[str, Field(min_length=1)]
    exported_at: DateTimeStr
    exporter: ExporterInfo | None = None
    requires_credential_reissue: bool
    counts: ManifestCounts
    files: ManifestFiles
