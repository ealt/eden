"""Reader / writer for the portable EDEN checkpoint format.

The format is specified in
``spec/v0/10-checkpoints.md``. This package is one reference Python
binding; a conforming third-party implementation in any language can
produce / consume the same bytes from the spec alone.

Public surface:

- :class:`CheckpointManifest`, :class:`ManifestCounts`,
  :class:`ManifestFiles`, :class:`ExporterInfo` — manifest models.
- :class:`CheckpointWriter` — streaming tar producer.
- :class:`CheckpointReader` — extracted-archive reader.
- :func:`extract_checkpoint` — untar a stream + return a reader.
- :data:`CHECKPOINT_FORMAT_VERSION`, :data:`CHECKPOINT_SPEC_VERSION`,
  :data:`CHECKPOINT_MEDIA_TYPE`, :data:`ARTIFACT_URI_PREFIX`,
  :data:`DEFAULT_FILES` — format constants.
- Error types: :class:`CheckpointError`, :class:`CheckpointInvalid`,
  :class:`UnsupportedCheckpointVersion`, :class:`SpecVersionMismatch`,
  :class:`ExperimentIdConflict`, :class:`ExperimentIdMismatch`.
"""

from ._hashing import is_valid_sha256_hex, sha256_hex
from .errors import (
    CheckpointError,
    CheckpointInvalid,
    ExperimentIdConflict,
    ExperimentIdMismatch,
    SpecVersionMismatch,
    UnsupportedCheckpointVersion,
)
from .format import CheckpointReader, CheckpointWriter, extract_checkpoint
from .manifest import (
    ARTIFACT_URI_PREFIX,
    CHECKPOINT_FORMAT_VERSION,
    CHECKPOINT_MEDIA_TYPE,
    CHECKPOINT_SPEC_VERSION,
    DEFAULT_FILES,
    CheckpointManifest,
    ExporterInfo,
    ManifestCounts,
    ManifestFiles,
)

__all__ = [
    "ARTIFACT_URI_PREFIX",
    "CHECKPOINT_FORMAT_VERSION",
    "CHECKPOINT_MEDIA_TYPE",
    "CHECKPOINT_SPEC_VERSION",
    "CheckpointError",
    "CheckpointInvalid",
    "CheckpointManifest",
    "CheckpointReader",
    "CheckpointWriter",
    "DEFAULT_FILES",
    "ExperimentIdConflict",
    "ExperimentIdMismatch",
    "ExporterInfo",
    "ManifestCounts",
    "ManifestFiles",
    "SpecVersionMismatch",
    "UnsupportedCheckpointVersion",
    "extract_checkpoint",
    "is_valid_sha256_hex",
    "sha256_hex",
]
