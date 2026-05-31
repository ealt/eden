"""Artifact writer + bundle helpers — re-exported from ``eden_service_common``.

Issue #168 moved the artifact path-builder and bundle writer into
:mod:`eden_service_common.artifacts` so the web-UI routes and the ideator
subprocess host (a worker-host package that must not depend on the web-UI) share
one implementation. This module re-exports the public surface so existing
``from ..artifacts import …`` call sites keep working unchanged.

New code may import from :mod:`eden_service_common.artifacts` directly.
"""

from __future__ import annotations

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
]
