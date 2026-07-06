# eden-blob (unused placeholder)

This directory was reserved for a standalone blob-store package. The
work it anticipated shipped elsewhere: the chapter-8 §5 / chapter-7
§16 artifact-store surface is implemented by the `ArtifactBackend`
protocol and its `FileArtifactBackend` / `S3Backend` / `GcsBackend`
implementations in
[`eden-storage`](../eden-storage/src/eden_storage/artifact_backend.py)
(issue #166 wire contract; Phase 13d cloud backends, issue #174).
Backend selection is server-side only (`--blob-backend file|s3|gcs`
on the task-store-server); clients always see opaque
`eden://artifacts/<id>` URIs, so no client-side blob package is
needed.

This directory is intentionally NOT a `pyproject.toml` workspace
member and contains no code. It is retained only so the historical
roadmap references resolve; it can be deleted outright if the
standalone-package split is never revisited.
