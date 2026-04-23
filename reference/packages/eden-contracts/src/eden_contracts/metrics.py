"""MetricsSchema — declarative map from metric name to storage type.

Mirrors ``spec/v0/schemas/metrics-schema.schema.json``. Appears as
``metrics_schema`` on :class:`ExperimentConfig` and constrains every
metrics payload an evaluator produces.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal, Self

from pydantic import ConfigDict, RootModel, StringConstraints, model_validator

MetricType = Literal["integer", "real", "text"]
"""The three storage types a metric value may declare."""

METRIC_NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"
_METRIC_NAME_RE = re.compile(METRIC_NAME_PATTERN)

RESERVED_METRIC_NAMES = frozenset(
    {
        "trial_id",
        "commit_sha",
        "parent_commits",
        "branch",
        "status",
        "artifacts_uri",
        "description",
        "timestamp",
        "started_at",
        "completed_at",
    }
)
"""Keys owned by the trial object; a metrics schema MUST NOT reuse them."""

MetricName = Annotated[str, StringConstraints(pattern=METRIC_NAME_PATTERN)]


class MetricsSchema(RootModel[dict[str, MetricType]]):
    """Map from metric name to storage type; at least one entry required."""

    model_config = ConfigDict(strict=True)

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if not self.root:
            raise ValueError("metrics_schema MUST declare at least one metric")
        for name in self.root:
            if not _METRIC_NAME_RE.match(name):
                raise ValueError(
                    f"metric name {name!r} does not match {METRIC_NAME_PATTERN}"
                )
            if name in RESERVED_METRIC_NAMES:
                raise ValueError(
                    f"metric name {name!r} is reserved by the trial schema"
                )
        return self
