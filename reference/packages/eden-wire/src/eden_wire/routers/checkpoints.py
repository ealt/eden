"""Portable-checkpoint routes (chapter 7 §14, chapter 10).

Two endpoints with distinct prefixes, so this module declares full paths
per route rather than an ``APIRouter(prefix=...)``:

- ``POST /v0/experiments/{experiment_id}/checkpoint`` — export (per-experiment).
- ``POST /v0/checkpoints/import`` — import (top-level; the one route NOT
  under the ``/v0/experiments/{id}`` base per the chapter-7 §1.3
  carve-out).
"""

from __future__ import annotations

import io
import os
import tempfile
from pathlib import Path
from typing import Any

from eden_checkpoint import CHECKPOINT_MEDIA_TYPE, CheckpointInvalid
from eden_checkpoint import ExperimentIdMismatch as CheckpointExperimentIdMismatch
from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse, Response

from .._dependencies import RouterDeps, check_experiment
from ..auth import require_admin
from ..errors import BadRequest, ExperimentIdMismatch


def build_router(deps: RouterDeps) -> APIRouter:
    """Return the checkpoints ``APIRouter`` bound to ``deps``."""
    router = APIRouter()
    router.post("/v0/experiments/{experiment_id}/checkpoint")(
        _export_checkpoint(deps)
    )
    router.post("/v0/checkpoints/import")(_import_checkpoint(deps))
    return router


def _export_checkpoint(deps: RouterDeps):
    async def export_checkpoint(
        request: Request,
        experiment_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> Response:
        """Chapter 7 §14.1: stream a portable-checkpoint archive.

        Admin-gated (literal ``admin`` principal per §13.1). Returns the
        tar bytes with ``Content-Type: application/x-eden-checkpoint+tar``.
        The wave-4 binding materializes the archive to an in-memory
        buffer; future revisions MAY switch to a streaming temp-file
        model for very large experiments (chapter 10 §6 leaves the
        materialization strategy implementation-defined).

        Caller-supplied substrate-external pieces (``experiment_config``
        text and ``repo_bundle`` bytes) are NOT carried on this wave-4
        endpoint — wave 4 surfaces only the Store-managed JSONL data plus
        zero-byte placeholders for those fields. Wave 5 wires the
        substrate-external integration.
        """
        check_experiment(deps, experiment_id, x_eden_experiment_id)
        if deps.admin_token is not None:
            require_admin(request)
        # Compose substrate-external pieces. The bundle is generated
        # per-request inside a temp file (git bundle is a write-then-read
        # flow; can't stream directly to the export buffer). When the
        # repo path is unset (e.g. test fixtures) the bundle stays empty
        # — the resulting archive is structurally valid but receiver-side
        # resume requires both substrate pieces.
        bundle_bytes = b""
        if deps.checkpoint_repo_root is not None:
            from eden_checkpoint.repo_bundle import create_bundle

            with tempfile.TemporaryDirectory(prefix="eden-checkpoint-bundle-") as td:
                bundle_path = Path(td) / "repo.bundle"
                try:
                    create_bundle(deps.checkpoint_repo_root, bundle_path)
                    bundle_bytes = bundle_path.read_bytes()
                except CheckpointInvalid:
                    # Empty repo / unreachable bundle: emit a zero-byte
                    # placeholder rather than 5xx-ing. The receiver's
                    # chapter-10 §12 cross-reference validation will
                    # surface any inconsistency at import time.
                    bundle_bytes = b""
        buffer = io.BytesIO()
        deps.store.export_checkpoint(
            buffer,
            experiment_config=deps.checkpoint_config_text,
            repo_bundle=bundle_bytes,
        )
        return Response(
            content=buffer.getvalue(),
            media_type=CHECKPOINT_MEDIA_TYPE,
        )

    return export_checkpoint


def _import_checkpoint(deps: RouterDeps):
    async def import_checkpoint(
        request: Request,
        as_experiment_id: str | None = Query(None),
        x_eden_experiment_id: str | None = Header(None),
    ) -> Response:
        """Chapter 7 §14.2: import a portable-checkpoint archive.

        Admin-gated (literal ``admin`` principal per §13.1;
        bootstrap-class because a fresh receiver has no ``admins``- group
        member). The §1.3 experiment-scoping carve-out applies: the
        ``X-Eden-Experiment-Id`` header is OPTIONAL on this endpoint, but
        if present MUST equal the receiving experiment's own id (a #128
        import lands under the receiver's minted id; the manifest's id is
        recorded as ``imported_from.source_experiment_id`` provenance). The wire
        layer's ``ExperimentIdMismatch`` covers that surface; the
        eden-checkpoint ``ExperimentIdMismatch`` covers the
        store-target-vs-manifest mismatch and is re-raised through the
        same wire type per the spec error-vocabulary uniformity rule.

        The body MUST be the raw tar archive bytes; this wave does not
        accept multipart/form-data — operators using the script wrapper
        or the StoreClient send the bytes directly.
        """
        if deps.admin_token is not None:
            require_admin(request)
        archive_bytes = await request.body()
        if not archive_bytes:
            raise BadRequest("empty request body; expected tar archive")
        # Pre-route ExperimentIdMismatch: when the optional header is
        # supplied, fail fast against the post-rewrite id BEFORE
        # extracting the archive (avoids creating a tempdir for a request
        # we'll reject anyway).
        target_id = as_experiment_id or deps.store.experiment_id
        if x_eden_experiment_id is not None and x_eden_experiment_id != target_id:
            raise ExperimentIdMismatch(
                f"X-Eden-Experiment-Id header {x_eden_experiment_id!r} does "
                f"not match the post-rewrite experiment_id {target_id!r}"
            )
        with tempfile.TemporaryDirectory(prefix="eden-checkpoint-wire-") as td:
            extract_dir = Path(td)
            try:
                result = deps.store.import_checkpoint(
                    io.BytesIO(archive_bytes),
                    as_experiment_id=as_experiment_id,
                    extract_dir=extract_dir,
                )
            except CheckpointExperimentIdMismatch as exc:
                # Surface the chapter-10 §11 mismatch through the same
                # wire vocabulary as the §1.3 header check.
                raise ExperimentIdMismatch(str(exc)) from exc
        warnings = _apply_reissued_credentials(deps, result)
        # Chapter 7 §14.2 mandates 201 Created on a successful import (a
        # new experiment row is materialized). FastAPI's default would be
        # 200; an explicit JSONResponse sets the spec-pinned status
        # without losing the problem+json envelope wiring above.
        return JSONResponse(
            status_code=201,
            content={
                "experiment_id": result.experiment_id,
                "warnings": warnings,
            },
        )

    return import_checkpoint


def _apply_reissued_credentials(deps: RouterDeps, result: Any) -> list[str]:
    """Persist post-import reissued credentials and return the warnings list.

    Per `10-checkpoints.md` §8 step 4 the import already minted fresh
    credentials for every imported worker atomically with the rest of the
    commit; the new tokens are on ``result.reissued_credentials``. The
    wire binding's implementation-defined side channel (§8 last
    paragraph) is to persist each ``<worker_id>:<token>`` bearer to the
    operator-configured credentials directory so the worker hosts pick it
    up at startup (no manual `reissue_credential` from the operator is
    needed for the steady-state import → resume flow). When no directory
    is configured, the tokens stay ephemeral and a warning calls that
    out.
    """
    warnings: list[str] = list(result.warnings)
    reissued = dict(result.reissued_credentials)
    if not reissued:
        return warnings
    if deps.credentials_dir_root is not None:
        persisted_paths = _persist_reissued_credentials(
            deps.credentials_dir_root, reissued
        )
        warnings.append(
            "credentials reissued and persisted for "
            f"{len(reissued)} worker(s): "
            + ", ".join(str(p) for p in persisted_paths)
        )
    else:
        warnings.append(
            "credentials reissued for "
            f"{len(reissued)} worker(s) "
            f"({', '.join(sorted(reissued))}) but no "
            "checkpoint_import_credentials_dir is configured; "
            "tokens were NOT persisted — operators must reissue via the "
            "admin endpoint before the workers can claim"
        )
    return warnings


def _persist_reissued_credentials(
    credentials_dir: Path, reissued: dict[str, str]
) -> list[Path]:
    """Write the post-import per-worker tokens to ``credentials_dir``.

    Per ``10-checkpoints.md`` §8 step 4 the importer mints fresh
    credentials for every imported worker atomically with the commit;
    this helper is the receiver-side implementation-defined side channel
    for the reference deployment. The file layout matches
    :func:`eden_service_common.auth.bootstrap_worker_credential`'s
    ``<credentials_dir>/<worker_id>.token`` convention so a worker host
    whose credentials volume is bind-mounted from ``credentials_dir``
    picks up the freshly-issued bearer at startup with no
    `reissue_credential` round-trip.

    Each file holds the raw token plaintext (NOT the
    ``<worker_id>:<token>`` bearer; the host's bootstrap helper assembles
    the bearer). Files are written atomically via a randomly-suffixed tmp
    file + ``os.replace`` (the random suffix is load-bearing — two
    concurrent import handlers targeting the same worker_id MUST NOT
    share a tmp filename or one process's in-flight write could clobber
    the other's, and ``os.replace`` could lose a write entirely). Mode is
    locked to ``0o600`` per chapter 7 §13.5 token-storage hygiene. The
    directory is created on first use with ``parents=True`` so a fresh
    deployment doesn't fail when the bind-mount target hasn't been
    pre-populated.
    """
    import secrets

    credentials_dir.mkdir(parents=True, exist_ok=True)
    persisted: list[Path] = []
    for worker_id in sorted(reissued):
        token = reissued[worker_id]
        path = credentials_dir / f"{worker_id}.token"
        suffix = secrets.token_hex(8)
        tmp = path.with_suffix(f"{path.suffix}.{suffix}.tmp")
        try:
            tmp.write_text(token)
            tmp.chmod(0o600)
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        persisted.append(path)
    return persisted
