"""Experiment — observed runtime state of an EDEN experiment.

Mirrors ``spec/v0/schemas/experiment.schema.json``. Distinct from
``ExperimentConfig`` (declarative input written by the operator at
experiment creation): this model carries the lifecycle ``state`` field
introduced in 12a-3 (``02-data-model.md`` §2.5) and any other observed
runtime metadata. The split mirrors task vs task-payload — declarative
input lives in the config, observed runtime in the experiment object.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from ._common import CommitSha, DateTimeStr, DisplayName, ExperimentId, NotNone

ExperimentState = Literal["running", "terminated"]
"""Lifecycle state per ``02-data-model.md`` §2.5.

``"running"`` is the default at experiment creation. ``"terminated"``
is a one-way transition committed by the operator wire op
(``04-task-protocol.md`` §8.1) or the orchestrator's policy-driven
termination path (``03-roles.md`` §6.2 decision-type 0).
"""


class ImportProvenance(BaseModel):
    """Provenance recorded on an experiment that was created by import.

    Mirrors the ``imported_from`` object on
    ``spec/v0/schemas/experiment.schema.json``. Carried verbatim from the
    source checkpoint's manifest at import time; the recovery-probe
    anchor for the lost-201 case in
    ``spec/v0/10-checkpoints.md`` §10.
    """

    model_config = ConfigDict(strict=True, extra="allow")

    checkpoint_exported_at: DateTimeStr
    checkpoint_format_version: Annotated[str, Field(min_length=1)]
    source_experiment_id: Annotated[ExperimentId | None, NotNone] = None
    """Export-side ``experiment_id`` of the source experiment, stamped at
    import time when the receiver minted a fresh ``exp_*`` (no
    ``as_experiment_id`` override). Provenance only — never the PK
    (spec/v0/10-checkpoints.md §10, 02-data-model.md §2.5)."""


class Experiment(BaseModel):
    """Runtime object backing the ``experiment.state`` lifecycle field.

    Constructed in-memory by the Store; mutated only via the lifecycle
    ops (``terminate_experiment`` / ``update_experiment_state``).
    """

    model_config = ConfigDict(strict=True, extra="allow")

    experiment_id: ExperimentId
    name: Annotated[DisplayName | None, NotNone] = None
    state: ExperimentState
    created_at: DateTimeStr
    base_commit_sha: Annotated[CommitSha | None, NotNone] = None
    """The experiment seed commit on ``main`` (``02-data-model.md`` §2.5),
    recorded at registration / repo-init time. The orchestrator reads it to
    create the seed baseline variant (``02-data-model.md`` §9.4) in both
    single- and multi-experiment modes. Absent (never JSON ``null``) on
    experiments registered before this field existed; such experiments never
    acquire a baseline. Immutable once written; round-trips through portable
    checkpoint export/import (``10-checkpoints.md`` §5)."""
    imported_from: ImportProvenance | None = None
    """Set at import time on experiments produced by ``import_checkpoint``;
    ``None`` (serialized as JSON ``null``) on natively-created experiments.
    The wire response intentionally allows the literal ``null`` (see
    ``spec/v0/schemas/experiment.schema.json``'s ``oneOf: [null, object]``)
    so callers can detect "field present, no import" without branching on
    key-presence. Written exactly once and immutable thereafter."""
