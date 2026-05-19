"""Checkpoint recovery-probe conformance scenarios — chapter 10 §10.

Per chapter 9 §5 "Recovery probe": after an import, ``read_experiment``
returns ``imported_from.checkpoint_exported_at`` matching the source
manifest's ``exported_at``; on a natively-created experiment, the
field is absent.

The contract anchors the lost-import-response recovery flow: a client
whose 201 was dropped probes ``GET /v0/experiments/{E}`` and matches
``imported_from`` against the local manifest to decide whether the
import already committed.
"""

from __future__ import annotations

import io
import tarfile
from typing import Any

import pytest
from conformance.harness import _seed
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = "Recovery probe"


def _parse_manifest(archive_bytes: bytes) -> dict[str, Any]:
    import json

    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r|") as tar:
        for member in tar:
            if member.name.endswith("manifest.json"):
                f = tar.extractfile(member)
                assert f is not None
                return json.loads(f.read())
    raise AssertionError("manifest.json not found in archive")


def test_native_experiment_has_no_imported_from(
    wire_client: WireClient,
) -> None:
    """spec/v0/02-data-model.md §2.5 — natively-created experiments have imported_from absent.

    Per chapter 02 §2.5 ``imported_from`` is the recovery-probe anchor;
    on a natively-created experiment it MUST be absent (JSON null on
    the wire). The reference impl writes ``imported_from: null`` on
    every native experiment.
    """
    body = _seed.read_experiment(wire_client)
    assert body.get("imported_from") is None


def test_imported_from_matches_source_manifest(
    sender_wire_client: WireClient,
    receiver_wire_client: WireClient,
) -> None:
    """spec/v0/10-checkpoints.md §10 — imported_from.checkpoint_exported_at matches source.

    Per chapter 10 §10 the recovery-probe anchor on a post-import
    Experiment ``imported_from.checkpoint_exported_at`` equals the
    source manifest's ``exported_at`` value, verbatim. The
    ``checkpoint_format_version`` also round-trips verbatim.
    """
    archive = _seed.export_checkpoint(sender_wire_client)
    manifest = _parse_manifest(archive)

    resp = _seed.import_checkpoint(
        receiver_wire_client,
        archive,
        as_experiment_id=receiver_wire_client.experiment_id,
    )
    assert resp.status_code == 201, resp.text

    received = _seed.read_experiment(receiver_wire_client)
    imported = received.get("imported_from")
    assert imported is not None
    assert imported["checkpoint_exported_at"] == manifest["exported_at"]
    assert (
        imported["checkpoint_format_version"]
        == manifest["checkpoint_format_version"]
    )


def test_double_import_returns_conflict(
    sender_wire_client: WireClient,
    receiver_wire_client: WireClient,
) -> None:
    """spec/v0/10-checkpoints.md §10 — recovery-probe contract requires conflict on second import.

    The §10 recovery-probe contract is anchored by the §11 collision
    rule: a second import into an already-populated receiver MUST
    return 409 ``eden://error/experiment-id-conflict``. The client
    then probes ``read_experiment`` and disambiguates "we won
    earlier" from "another writer beat us" via the
    ``imported_from.checkpoint_exported_at`` match.
    """
    archive = _seed.export_checkpoint(sender_wire_client)
    first = _seed.import_checkpoint(
        receiver_wire_client,
        archive,
        as_experiment_id=receiver_wire_client.experiment_id,
    )
    assert first.status_code == 201, first.text

    second = _seed.import_checkpoint(
        receiver_wire_client,
        archive,
        as_experiment_id=receiver_wire_client.experiment_id,
    )
    assert second.status_code == 409
    assert second.json()["type"] == "eden://error/experiment-id-conflict"
