"""Experiment-config loading for the worker hosts.

Each subprocess-mode host reads ``plan_command`` /
``implement_command`` / ``evaluate_command`` from the same
experiment-config YAML the task-store-server consumes. The
``ExperimentConfig`` model accepts these as extra fields
(``model_config = ConfigDict(extra='allow')``), which puts them in
``model_extra``.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from eden_contracts import ExperimentConfig


def load_experiment_config(path: str | Path) -> ExperimentConfig:
    """Parse a YAML experiment-config file."""
    with Path(path).open() as f:
        data = yaml.safe_load(f)
    return ExperimentConfig.model_validate(data)


def require_command(config: ExperimentConfig, key: str) -> str:
    """Fetch a required command string from the config's extras.

    Raises ``ValueError`` if the key is missing or empty.
    """
    extras = config.model_extra or {}
    value = extras.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"experiment-config is missing required key {key!r} "
            "(subprocess-mode hosts read commands from the experiment YAML)"
        )
    return value
