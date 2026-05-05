# eden-blob (placeholder)

This directory is a placeholder for the future blob-store backend.
Phase 13 of the roadmap will populate it; see
[`docs/roadmap.md`](../../../docs/roadmap.md) "Phase 13 — Kubernetes
reference deployment" → `S3/GCS blob backend`.

The chapter-8 §5 artifact-store contract is the spec-side surface
this package will implement. Until Phase 13, the reference deployment
satisfies the contract via host-local file paths under
`/var/lib/eden/artifacts/` (see the web-ui's `--artifacts-dir` flag).

This package is intentionally NOT a `pyproject.toml` workspace
member; it stays out of the build until there is something to build.
