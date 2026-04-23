"""Validate the Phase 1 experiment fixture against the ExperimentConfig model.

The fixture lives at ``tests/fixtures/experiment/.eden/config.yaml`` and
already has JSON Schema validation wired into the schema-validity CI
job. This test adds the symmetric model-side check so schema and model
agreement on this concrete artifact is covered by the python-test job.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from eden_contracts import ExperimentConfig

FIXTURE: Path = (
    Path(__file__).resolve().parents[4]
    / "tests"
    / "fixtures"
    / "experiment"
    / ".eden"
    / "config.yaml"
)


def test_experiment_fixture_validates() -> None:
    data = yaml.safe_load(FIXTURE.read_text())
    config = ExperimentConfig.model_validate(data)
    assert config.parallel_trials >= 1
    assert config.max_trials >= 1
    assert config.objective.expr
    assert config.metrics_schema.root
