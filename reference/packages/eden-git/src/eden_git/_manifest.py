"""Eval-manifest builder for ``spec/v0/06-integrator.md`` §4.2.

The integrator writes one eval-manifest file per ``variant/*`` commit
at ``.eden/variants/<variant_id>/eval.json``. This module produces the
manifest bytes from a ``Variant`` object.

Required-field values come directly from the variant per §4.2; the
builder MUST NOT synthesize or transform them. Optional fields
(``description``, ``artifacts_uri``) are emitted only when present on
the variant.

Byte stability is load-bearing: the integrator's idempotency check in
``integrator.py`` step 2 re-derives the manifest from the replayed
variant and compares byte-for-byte against the committed blob. Any
non-determinism here produces false ``CorruptIntegrationState``
errors on re-invocation.
"""

from __future__ import annotations

import json

from eden_contracts import Variant


class ManifestFieldMissing(ValueError):
    """A required §4.2 field is absent from the variant."""


def build_manifest(variant: Variant) -> bytes:
    """Serialize ``variant`` as the §4.2 eval manifest.

    The output is UTF-8 JSON with ``sort_keys=True``, ``indent=2``,
    and a trailing newline. Required-field absences raise
    ``ManifestFieldMissing``; callers translate these into
    ``NotReadyForIntegration`` at the integrator layer.
    """
    if variant.commit_sha is None:
        raise ManifestFieldMissing("variant.commit_sha is required (§4.2)")
    if variant.evaluation is None:
        raise ManifestFieldMissing("variant.evaluation is required (§4.2)")
    if variant.completed_at is None:
        raise ManifestFieldMissing("variant.completed_at is required (§4.2)")

    payload: dict[str, object] = {
        "variant_id": variant.variant_id,
        "idea_id": variant.idea_id,
        "commit_sha": variant.commit_sha,
        "parent_commits": list(variant.parent_commits),
        "evaluation": dict(variant.evaluation),
        "completed_at": variant.completed_at,
    }
    if variant.artifacts_uri is not None:
        payload["artifacts_uri"] = variant.artifacts_uri
    if variant.description is not None:
        payload["description"] = variant.description

    # allow_nan=False rejects NaN / +-inf — those are not valid JSON and
    # the spec requires eval.json to be a JSON file. Defense in depth
    # behind Store.validate_evaluation's finite-float check.
    serialized = json.dumps(
        payload, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False
    )
    return (serialized + "\n").encode("utf-8")
