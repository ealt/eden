"""Typed errors raised by the portable-checkpoint reader / writer.

Each error maps to one entry in the closed wire-error vocabulary
(``spec/v0/07-wire-protocol.md`` §9). The wire binding translates these
exceptions to ``problem+json`` envelopes; in-process callers may catch
the typed exception directly.
"""

from __future__ import annotations


class CheckpointError(Exception):
    """Base class for portable-checkpoint reader / writer errors."""


class CheckpointInvalid(CheckpointError):
    """The uploaded archive failed structural or cross-reference validation.

    Mapped to ``eden://error/checkpoint-invalid`` (HTTP 400). Surfaced when
    the manifest fails schema validation, JSONL entries are malformed, the
    git bundle is unreadable, a referenced artifact hash is missing under
    ``artifacts/sha256/``, or any of the
    ``spec/v0/10-checkpoints.md`` §12 cross-reference checks fail.
    """


class UnsupportedCheckpointVersion(CheckpointError):
    """The manifest's ``checkpoint_format_version`` is not recognized.

    Mapped to ``eden://error/unsupported-checkpoint-version`` (HTTP 409).
    """


class SpecVersionMismatch(CheckpointError):
    """The manifest's ``spec_version`` does not match the importer's spec.

    Mapped to ``eden://error/spec-version-mismatch`` (HTTP 409). The
    detail message SHOULD reference any implementation-provided migration
    mechanism.
    """


class ExperimentIdConflict(CheckpointError):
    """The manifest's ``experiment_id`` collides with an existing experiment.

    Mapped to ``eden://error/experiment-id-conflict`` (HTTP 409). The
    operator MAY retry the import with ``as_experiment_id=<new>`` per
    ``spec/v0/10-checkpoints.md`` §11.
    """


class ExperimentIdMismatch(CheckpointError):
    """The ``X-Eden-Experiment-Id`` header disagrees with the manifest.

    Mapped to ``eden://error/experiment-id-mismatch`` (HTTP 400) on the
    portable-checkpoint endpoints per ``spec/v0/07-wire-protocol.md``
    §1.3 carve-out.
    """
