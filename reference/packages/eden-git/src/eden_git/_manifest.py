"""Eval-manifest builder for ``spec/v0/06-integrator.md`` §4.2.

The integrator writes one eval-manifest file per ``trial/*`` commit
at ``.eden/trials/<trial_id>/eval.json``. This module produces the
manifest bytes from a ``Trial`` object.

Required-field values come directly from the trial per §4.2; the
builder MUST NOT synthesize or transform them. Optional fields
(``description``, ``artifacts_uri``) are emitted only when present on
the trial.

Byte stability is load-bearing: the integrator's idempotency check in
``integrator.py`` step 2 re-derives the manifest from the replayed
trial and compares byte-for-byte against the committed blob. Any
non-determinism here produces false ``CorruptIntegrationState``
errors on re-invocation.
"""

from __future__ import annotations

import json

from eden_contracts import Trial


class ManifestFieldMissing(ValueError):
    """A required §4.2 field is absent from the trial."""


def build_manifest(trial: Trial) -> bytes:
    """Serialize ``trial`` as the §4.2 eval manifest.

    The output is UTF-8 JSON with ``sort_keys=True``, ``indent=2``,
    and a trailing newline. Required-field absences raise
    ``ManifestFieldMissing``; callers translate these into
    ``NotReadyForIntegration`` at the integrator layer.
    """
    if trial.commit_sha is None:
        raise ManifestFieldMissing("trial.commit_sha is required (§4.2)")
    if trial.metrics is None:
        raise ManifestFieldMissing("trial.metrics is required (§4.2)")
    if trial.completed_at is None:
        raise ManifestFieldMissing("trial.completed_at is required (§4.2)")

    payload: dict[str, object] = {
        "trial_id": trial.trial_id,
        "proposal_id": trial.proposal_id,
        "commit_sha": trial.commit_sha,
        "parent_commits": list(trial.parent_commits),
        "metrics": dict(trial.metrics),
        "completed_at": trial.completed_at,
    }
    if trial.artifacts_uri is not None:
        payload["artifacts_uri"] = trial.artifacts_uri
    if trial.description is not None:
        payload["description"] = trial.description

    # allow_nan=False rejects NaN / +-inf — those are not valid JSON and
    # the spec requires eval.json to be a JSON file. Defense in depth
    # behind Store.validate_metrics's finite-float check.
    serialized = json.dumps(
        payload, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False
    )
    return (serialized + "\n").encode("utf-8")
