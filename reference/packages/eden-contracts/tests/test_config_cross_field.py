"""Cross-field validation of ``ExperimentConfig.termination_policy`` (issue #157).

``termination_policy`` is required when ``dispatch_mode.termination == "auto"``
— a single-experiment-orchestrator contract enforced on both the JSON Schema
side (top-level ``allOf-if-then``, covered by the parity corpus in
``cases.py``) and the Pydantic side (``_termination_required_when_auto``).

These tests pin the Pydantic-side context behavior the parity corpus can't
express: the rule is enforced by default but can be skipped via the
``{"require_termination_policy": False}`` validation context, which the
orchestrator's multi-experiment mode uses (termination there flows from the
``--termination-policy`` CLI flag pending #214, not from this config). Without
the skip, a ``termination=auto`` bootstrap config valid pre-#157 would fail to
load on the multi-experiment path.
"""

from __future__ import annotations

import pytest
from eden_contracts import ExperimentConfig
from pydantic import ValidationError

_AUTO_NO_POLICY = {
    "parallel_variants": 1,
    "evaluation_schema": {"accuracy": "real"},
    "objective": {"expr": "accuracy", "direction": "maximize"},
    "dispatch_mode": {"termination": "auto"},
}


def test_auto_without_policy_rejected_by_default() -> None:
    with pytest.raises(ValidationError):
        ExperimentConfig.model_validate(_AUTO_NO_POLICY)


def test_auto_without_policy_skipped_with_context() -> None:
    # Multi-experiment mode loads with this context; the rule is bypassed.
    config = ExperimentConfig.model_validate(
        _AUTO_NO_POLICY, context={"require_termination_policy": False}
    )
    assert config.termination_policy is None
    assert config.dispatch_mode is not None
    assert config.dispatch_mode.termination == "auto"


def test_skip_context_still_validates_other_fields() -> None:
    # The skip is narrow — it only relaxes the termination cross-field rule,
    # not the rest of the model (e.g. parallel_variants >= 1).
    bad = {**_AUTO_NO_POLICY, "parallel_variants": 0}
    with pytest.raises(ValidationError):
        ExperimentConfig.model_validate(
            bad, context={"require_termination_policy": False}
        )


def test_auto_with_policy_accepted_with_and_without_context() -> None:
    ok = {**_AUTO_NO_POLICY, "termination_policy": {"kind": "never_terminate"}}
    assert ExperimentConfig.model_validate(ok).termination_policy is not None
    assert (
        ExperimentConfig.model_validate(
            ok, context={"require_termination_policy": False}
        ).termination_policy
        is not None
    )
