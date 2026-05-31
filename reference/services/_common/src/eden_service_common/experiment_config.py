"""Experiment-config loading for the worker hosts.

Each subprocess-mode host reads ``ideation_command`` /
``execution_command`` / ``evaluation_command`` from the same
experiment-config YAML the task-store-server consumes. The
``ExperimentConfig`` model accepts these as extra fields
(``model_config = ConfigDict(extra='allow')``), which puts them in
``model_extra``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from eden_contracts import ExperimentConfig


def load_experiment_config(
    path: str | Path,
    *,
    validation_context: dict[str, Any] | None = None,
) -> ExperimentConfig:
    """Parse a YAML experiment-config file.

    ``validation_context`` is forwarded to ``model_validate`` for callers
    that need to toggle context-dependent cross-field rules (e.g. the
    orchestrator's multi-experiment mode passes
    ``{"require_termination_policy": False}`` to skip the
    single-experiment termination-policy requirement — see
    ``ExperimentConfig._termination_required_when_auto``).
    """
    with Path(path).open() as f:
        data = yaml.safe_load(f)
    return ExperimentConfig.model_validate(data, context=validation_context)


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
