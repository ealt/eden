"""Bearer authentication for the EDEN wire binding (chapter 7 §13).

Implements the normative per-worker + admin authentication scheme
specified in [`spec/v0/07-wire-protocol.md`](../../../../spec/v0/07-wire-protocol.md)
§13. Every request to a ``/v0/`` endpoint MUST carry an
``Authorization: Bearer <principal>:<secret>`` header; the bearer is
parsed, the principal classified as either ``admin`` or a registered
``worker_id``, and the secret verified against the deployment-wide
admin token (constant-time compare) or the worker's stored credential
hash (delegated to ``Store.verify_worker_credential``).

The module exposes:

- :class:`Principal` — the result of a successful authentication.
- :func:`parse_bearer` — split the header into ``(principal, secret)``.
- :func:`install_auth_middleware` — install the FastAPI middleware
  that authenticates and dispatches every request before the route
  handler runs.

Endpoint authorization (admin-gated vs worker-gated vs either) is the
*server's* responsibility; this module only authenticates the bearer
and stashes :class:`Principal` on ``request.state`` for handlers to
inspect.
"""

from __future__ import annotations

import hmac
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from eden_contracts._common import WORKER_ID_PATTERN
from eden_storage import Store
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .errors import (
    Forbidden,
    Unauthorized,
    envelope_for_error,
)

PROBLEM_JSON = "application/problem+json"

PrincipalKind = Literal["admin", "worker"]


@dataclass(frozen=True)
class Principal:
    """The authenticated identity for one request.

    ``kind="admin"`` carries no ``worker_id`` (the admin principal is
    a deployment-singleton, not a registered worker; per
    [`spec/v0/02-data-model.md`](../../../../spec/v0/02-data-model.md)
    §6.1 the literal id ``"admin"`` is reserved from the worker
    registry, so there is no Worker row to point at).
    ``kind="worker"`` carries the registered ``worker_id`` whose
    credential matched.
    """

    kind: PrincipalKind
    worker_id: str | None

    @property
    def actor_id(self) -> str:
        """The ActorId this principal stamps into ``*_by`` audit fields.

        The admin principal stamps the literal ``"admin"`` sentinel (it
        has no minted ``worker_id``); a worker principal stamps its
        opaque ``wkr_*`` id. Both satisfy the ActorId grammar
        ``^(admin|wkr_[0-9a-hjkmnp-tv-z]{26})$``
        ([`spec/v0/02-data-model.md`](../../../../spec/v0/02-data-model.md)
        §1.6).
        """
        if self.kind == "admin":
            return "admin"
        assert self.worker_id is not None
        return self.worker_id

    def is_admin(self) -> bool:
        """True iff this principal is the deployment-wide admin."""
        return self.kind == "admin"

    def is_worker(self) -> bool:
        """True iff this principal is a registered worker."""
        return self.kind == "worker"


def parse_bearer(header: str | None) -> tuple[str, str]:
    """Split the Authorization header into ``(principal, secret)``.

    Raises :class:`Unauthorized` when the header is missing, uses the
    wrong scheme, or doesn't contain a ``:`` separator.

    Worker-id grammar excludes ``:`` (per
    [`spec/v0/07-wire-protocol.md`](../../../../spec/v0/07-wire-protocol.md)
    §13.2) so splitting on the first colon is unambiguous.
    """
    if header is None:
        raise Unauthorized("missing Authorization header")
    parts = header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise Unauthorized("Authorization header must use the Bearer scheme")
    bearer = parts[1].strip()
    if ":" not in bearer:
        raise Unauthorized(
            "bearer must be in the form '<principal>:<secret>' (§13.1)"
        )
    principal, secret = bearer.split(":", 1)
    if not principal or not secret:
        raise Unauthorized("bearer principal and secret MUST both be non-empty")
    # §13.2: the principal is either the literal ``admin`` sentinel or an
    # opaque ``wkr_*`` worker id (spec/v0/02-data-model.md §1.6). Reject
    # anything else (e.g. a legacy kebab id) before consulting the store.
    if principal != "admin" and re.fullmatch(WORKER_ID_PATTERN, principal) is None:
        raise Unauthorized(
            "bearer principal MUST be 'admin' or an opaque wkr_* worker id "
            "(§13.2)"
        )
    return principal, secret


def authenticate(
    header: str | None,
    *,
    admin_token: str,
    store: Store,
) -> Principal:
    """Verify the presented bearer and return the authenticated principal.

    ``admin_token`` is the deployment's ``EDEN_ADMIN_TOKEN``; ``store``
    is consulted via ``verify_worker_credential`` for worker bearers.
    Constant-time comparison guards both branches against timing
    oracles. Raises :class:`Unauthorized` on any failure.
    """
    principal, secret = parse_bearer(header)
    if principal == "admin":
        if not hmac.compare_digest(
            secret.encode("utf-8"), admin_token.encode("utf-8")
        ):
            raise Unauthorized("admin token mismatch")
        return Principal(kind="admin", worker_id=None)
    # `principal` is a worker_id; let the Store hash-compare.
    if not store.verify_worker_credential(principal, secret):
        raise Unauthorized(
            "worker credential mismatch (no such worker, or wrong token)"
        )
    return Principal(kind="worker", worker_id=principal)


def install_auth_middleware(
    app: FastAPI,
    *,
    admin_token: str,
    store: Store,
    skip_paths: set[str] | None = None,
) -> None:
    """Install the §13 authentication middleware on ``app``.

    Every request to a ``/v0/`` endpoint is required to carry a valid
    bearer; the middleware authenticates the request, sets
    ``request.state.principal`` (a :class:`Principal`) for downstream
    handlers, and passes the request through. Failures emit a
    problem+json body under ``eden://error/unauthorized`` (HTTP 401).

    Paths in ``skip_paths`` (e.g. ``/healthz``) bypass the middleware
    unauthenticated. By convention ``/_reference/`` paths are NOT in
    the normative auth surface and remain anonymous unless the
    deployment opts otherwise; ``skip_paths`` is the explicit knob.
    """
    bypass = set(skip_paths or set())

    @app.middleware("http")
    async def _auth_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path in bypass:
            return await call_next(request)
        # The /_reference/ surface is non-normative; skip auth there
        # too unless the deployment included it in `skip_paths` and
        # then explicitly added the route paths back. (Same posture as
        # chapter 7 §11 — reference helpers are not part of the
        # normative binding.)
        if request.url.path.startswith("/_reference/"):
            return await call_next(request)
        try:
            principal = authenticate(
                request.headers.get("authorization"),
                admin_token=admin_token,
                store=store,
            )
        except Unauthorized as exc:
            envelope = envelope_for_error(exc, instance=str(request.url))
            return JSONResponse(
                status_code=envelope.status,
                media_type=PROBLEM_JSON,
                content=envelope.to_dict(),
            )
        request.state.principal = principal
        return await call_next(request)


def require_admin(request: Request) -> Principal:
    """Return the authenticated principal, requiring it to be ``admin``.

    Used by admin-gated route handlers. Raises :class:`Forbidden` (403,
    ``eden://error/forbidden``) when the principal is a worker; raises
    :class:`Unauthorized` (401) when no principal is on
    ``request.state`` (which means auth was disabled — e.g. the test
    harness omitted ``admin_token``; routes calling this helper assume
    auth is enabled).
    """
    principal = getattr(request.state, "principal", None)
    if principal is None:
        raise Unauthorized(
            "endpoint requires authentication; no principal on request.state"
        )
    if not isinstance(principal, Principal):
        raise Unauthorized("invalid principal on request.state")
    if not principal.is_admin():
        raise Forbidden(
            "endpoint is admin-gated; worker bearers MUST NOT access it (§13.3)"
        )
    return principal


def require_worker(request: Request) -> Principal:
    """Return the authenticated principal, requiring it to be a worker."""
    principal = getattr(request.state, "principal", None)
    if principal is None:
        raise Unauthorized(
            "endpoint requires authentication; no principal on request.state"
        )
    if not isinstance(principal, Principal):
        raise Unauthorized("invalid principal on request.state")
    if not principal.is_worker():
        raise Forbidden(
            "endpoint is worker-gated; admin bearers MUST NOT access it (§13.3)"
        )
    return principal


def get_principal(request: Request) -> Principal:
    """Return the authenticated principal (admin or worker), or raise.

    Used by routes that admit either kind (e.g. read endpoints in §6 /
    §7). Raises :class:`Unauthorized` if auth was not run.
    """
    principal = getattr(request.state, "principal", None)
    if principal is None:
        raise Unauthorized(
            "endpoint requires authentication; no principal on request.state"
        )
    if not isinstance(principal, Principal):
        raise Unauthorized("invalid principal on request.state")
    return principal
