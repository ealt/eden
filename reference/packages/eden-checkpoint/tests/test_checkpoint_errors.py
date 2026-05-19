"""Error-type taxonomy: each maps to one entry in the chapter-07 §9 vocab."""

from __future__ import annotations

from eden_checkpoint import (
    CheckpointError,
    CheckpointInvalid,
    ExperimentIdConflict,
    ExperimentIdMismatch,
    SpecVersionMismatch,
    UnsupportedCheckpointVersion,
)


def test_typed_errors_subclass_checkpoint_error() -> None:
    for cls in (
        CheckpointInvalid,
        UnsupportedCheckpointVersion,
        SpecVersionMismatch,
        ExperimentIdConflict,
        ExperimentIdMismatch,
    ):
        assert issubclass(cls, CheckpointError)


def test_typed_errors_carry_messages() -> None:
    err = CheckpointInvalid("missing artifact: sha256:abc")
    assert "missing artifact" in str(err)


def test_distinct_classes() -> None:
    """Each error class is distinct so catch-clauses can discriminate."""
    classes = (
        CheckpointInvalid,
        UnsupportedCheckpointVersion,
        SpecVersionMismatch,
        ExperimentIdConflict,
        ExperimentIdMismatch,
    )
    assert len({cls for cls in classes}) == len(classes)
