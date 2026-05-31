"""Artifact writer + bundle helpers — re-exported from ``eden_service_common``.

Issue #168 moved the artifact path-builder and bundle writer into
:mod:`eden_service_common.artifacts` so the web-UI routes and the ideator
subprocess host (a worker-host package that must not depend on the web-UI) share
one implementation. This module re-exports the public surface so existing
``from ..artifacts import …`` call sites keep working unchanged.

New code may import from :mod:`eden_service_common.artifacts` directly.
"""

from __future__ import annotations

from pathlib import Path

from eden_service_common.artifacts import (
    MANIFEST_NAME,
    MANIFEST_VERSION,
    ArtifactNaming,
    UploadedFile,
    entity_artifact_dir,
    idea_naming,
    is_bundle_uri,
    predict_artifact_uri,
    read_bundle_entry,
    read_bundle_manifest,
    submission_naming,
    write_artifact_bundle,
)

__all__ = [
    "MANIFEST_NAME",
    "MANIFEST_VERSION",
    "ArtifactNaming",
    "UploadedFile",
    "entity_artifact_dir",
    "idea_naming",
    "is_bundle_uri",
    "predict_artifact_uri",
    "read_bundle_entry",
    "read_bundle_manifest",
    "submission_naming",
    "write_artifact_bundle",
    "write_idea_artifact",
]


def write_idea_artifact(
    artifacts_dir: Path | str,
    idea_id: str,
    markdown: str,
) -> str:
    """Write a text-only idea artifact at ``ideas/<idea_id>/content.md``.

    Back-compat public helper preserved across the issue-#168 relocation. New
    code should call :func:`entity_artifact_dir` + :func:`write_artifact_bundle`
    directly; this thin wrapper keeps the pre-existing import surface stable.
    """
    target_dir = entity_artifact_dir(
        artifacts_dir, producer="ideator", entity_id=idea_id
    )
    return write_artifact_bundle(
        target_dir, idea_naming(), text_content=markdown, uploads=[]
    )
