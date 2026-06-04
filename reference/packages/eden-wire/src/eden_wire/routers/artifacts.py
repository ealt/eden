"""Artifact deposit / fetch endpoints (chapter 7 §16, issue #166).

Two normative ``/v0/`` endpoints hosted by the task-store-server:

- ``POST /v0/experiments/{E}/artifacts`` — deposit: a single multipart
  ``file`` part is streamed under a configurable size cap, persisted to
  the :class:`~eden_storage.ArtifactBackend`, and recorded in the Store
  with the depositing principal as ``created_by``. Returns an opaque
  ``eden://artifacts/<id>`` URI.
- ``GET /v0/experiments/{E}/artifacts/{A}`` — fetch: resolves the opaque
  id, enforces the §13.3 per-row ACL (depositor or admin-class), and
  returns the exact deposited bytes with safe-delivery headers.

The size cap is enforced **during** the multipart stream via Starlette's
``request.form(max_part_size=…)`` — over-cap parts raise
``MultiPartException`` before the whole body is buffered, satisfying the
§16.1 "MUST NOT require buffering the entire upload in memory" rule.
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import Response
from starlette.datastructures import UploadFile

from .._artifact_fd import artifact_response_headers
from .._dependencies import RouterDeps, check_experiment
from ..auth import get_principal
from ..errors import BadRequest, Forbidden, PayloadTooLarge
from ..models import DepositArtifactResponse

# Multipart framing overhead (boundary lines, the part's Content-
# Disposition / Content-Type headers) above the artifact bytes. The raw
# request body is streamed under ``cap + _MULTIPART_SLACK`` so a hostile
# over-cap upload is rejected before it can exhaust memory or disk; the
# exact artifact cap is then re-checked against the parsed part's bytes.
_MULTIPART_SLACK = 64 * 1024


def build_router(deps: RouterDeps) -> APIRouter:
    """Build the artifact deposit / fetch router (§16)."""
    router = APIRouter(prefix="/v0/experiments/{experiment_id}/artifacts")
    router.post("", status_code=201)(_deposit_artifact(deps))
    router.get("/{opaque_id}")(_fetch_artifact(deps))
    return router


def _depositor_id(deps: RouterDeps, request: Request) -> str:
    """Return the ``created_by`` stamp for a deposit (§13.3).

    Worker bearer → its ``worker_id``; admin bearer → the literal
    ``admin`` principal. When auth is disabled (test / in-process
    posture) every caller collapses onto the ``anonymous`` sentinel.
    """
    if deps.admin_token is None:
        return "anonymous"
    principal = get_principal(request)
    if principal.is_worker():
        assert principal.worker_id is not None
        return principal.worker_id
    return "admin"


def _authorize_fetch(deps: RouterDeps, request: Request, created_by: str) -> None:
    """Enforce the §16.2 per-row fetch ACL (depositor or admin-class).

    A no-op when auth is disabled. Otherwise the principal must be the
    artifact's depositor, the literal ``admin`` bearer, or a member of
    the ``admins`` group; anyone else — including a *different* worker —
    gets 403 ``eden://error/forbidden``.
    """
    if deps.admin_token is None:
        return
    principal = get_principal(request)
    if principal.is_admin():
        return
    assert principal.worker_id is not None
    if principal.worker_id == created_by:
        return
    if deps.store.resolve_worker_in_group(principal.worker_id, "admins"):
        return
    raise Forbidden(
        f"worker {principal.worker_id!r} may not fetch an artifact deposited "
        f"by {created_by!r} (§16.2: depositor or admin-class only)"
    )


def _sanitize_content_type(declared: str | None) -> str:
    """Reduce a declared content type to a header-safe single line.

    Strips CR/LF and other control characters (header-injection defense)
    and falls back to ``application/octet-stream`` when absent or empty.
    """
    if not declared:
        return "application/octet-stream"
    cleaned = "".join(ch for ch in declared if 32 <= ord(ch) < 127).strip()
    return cleaned or "application/octet-stream"


async def _read_body_capped(request: Request, limit: int) -> bytes:
    """Stream the raw request body, aborting once it crosses ``limit``.

    Enforces the §16.1 "MUST NOT buffer the entire upload before the size
    check" rule: a hostile over-cap upload is rejected mid-stream before
    it can exhaust memory or spill unbounded to disk. Starlette's
    ``request.form(max_part_size=…)`` does NOT cover file parts (it caps
    only non-file fields), so the cap is enforced here instead.
    """
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise PayloadTooLarge(
                f"artifact upload exceeds the {limit}-byte streamed cap"
            )
    return bytes(body)


def _replay_receive(body: bytes) -> Callable[[], Awaitable[dict[str, Any]]]:
    """Build an ASGI ``receive`` that replays an already-read body once.

    Lets the handler re-run Starlette's multipart parser over the
    capped-in-memory body without re-reading the (already-consumed)
    network stream.
    """
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


def _deposit_artifact(deps: RouterDeps):  # noqa: ANN202 — FastAPI handler factory
    async def deposit_artifact(
        request: Request,
        experiment_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, object]:
        check_experiment(deps, experiment_id, x_eden_experiment_id)
        created_by = _depositor_id(deps, request)
        cap = deps.max_artifact_bytes
        raw = await _read_body_capped(request, cap + _MULTIPART_SLACK)
        # Re-parse the capped body through Starlette's multipart machinery
        # (a fresh Request over a replay receive — the network stream is
        # already consumed).
        reparsed = Request(request.scope, _replay_receive(raw))
        form = await reparsed.form()
        try:
            upload = form.get("file")
            if not isinstance(upload, UploadFile):
                raise BadRequest(
                    "deposit body must be multipart/form-data with a single "
                    "'file' part"
                )
            data = await upload.read()
            content_type = _sanitize_content_type(upload.content_type)
        finally:
            await form.close()
        if len(data) > cap:
            raise PayloadTooLarge(
                f"artifact ({len(data)} bytes) exceeds the {cap}-byte deposit cap"
            )
        opaque_id = secrets.token_hex(16)
        deps.artifact_backend.store(opaque_id, data)
        deps.store.create_artifact(
            opaque_id=opaque_id,
            created_by=created_by,
            size_bytes=len(data),
            content_type=content_type,
        )
        return DepositArtifactResponse(
            artifacts_uri=f"eden://artifacts/{opaque_id}",
            size_bytes=len(data),
            content_type=content_type,
        ).model_dump(mode="json")

    return deposit_artifact


def _fetch_artifact(deps: RouterDeps):  # noqa: ANN202 — FastAPI handler factory
    async def fetch_artifact(
        request: Request,
        experiment_id: str,
        opaque_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> Response:
        check_experiment(deps, experiment_id, x_eden_experiment_id)
        # read_artifact raises NotFound (→ 404) for an absent id, BEFORE
        # the ACL check so a different worker cannot use 403-vs-404 to
        # probe which ids exist beyond their own — the reference posture
        # returns 404 for absent, 403 for present-but-unauthorized (§16.2).
        metadata = deps.store.read_artifact(opaque_id)
        _authorize_fetch(deps, request, metadata.created_by)
        data = deps.artifact_backend.load(opaque_id)
        headers = artifact_response_headers(opaque_id)
        return Response(
            content=data,
            media_type=metadata.content_type,
            headers=headers,
        )

    return fetch_artifact


__all__ = ["build_router"]
