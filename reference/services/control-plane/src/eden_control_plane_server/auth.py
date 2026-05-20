"""Bearer auth for the control plane.

Mirrors `eden_wire.auth` but the `verify_worker_credential` source is
the **deployment-scoped** worker registry from chapter 11 §6, not the
task-store-server's per-experiment registry. The two registries are
independent: a `worker_id` registered against the control plane is
unrelated to a same-named worker registered against any per-experiment
task-store-server.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Literal

from eden_control_plane import ControlPlaneStore
from eden_wire.errors import Forbidden, Unauthorized
from fastapi import Request

__all__ = [
    "Principal",
    "authenticate",
    "parse_bearer",
    "require_admin",
    "require_worker",
]

PrincipalKind = Literal["admin", "worker"]


@dataclass(frozen=True)
class Principal:
    """The authenticated identity for one request."""

    kind: PrincipalKind
    worker_id: str | None

    def is_admin(self) -> bool:
        """True iff this principal is the deployment-wide admin."""
        return self.kind == "admin"

    def is_worker(self) -> bool:
        """True iff this principal is a registered worker."""
        return self.kind == "worker"


def parse_bearer(header: str | None) -> tuple[str, str]:
    """Split `Authorization` header into `(principal, secret)`."""
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
        raise Unauthorized(
            "bearer principal and secret MUST both be non-empty"
        )
    return principal, secret


def authenticate(
    header: str | None,
    *,
    admin_token: str,
    store: ControlPlaneStore,
) -> Principal:
    """Verify the presented bearer and return the principal."""
    principal, secret = parse_bearer(header)
    if principal == "admin":
        if not hmac.compare_digest(
            secret.encode("utf-8"), admin_token.encode("utf-8")
        ):
            raise Unauthorized("admin token mismatch")
        return Principal(kind="admin", worker_id=None)
    if not store.verify_worker_credential(principal, secret):
        raise Unauthorized(
            "worker credential mismatch (no such worker, or wrong token)"
        )
    return Principal(kind="worker", worker_id=principal)


def require_admin(request: Request) -> Principal:
    """Return the principal, requiring it to be admin."""
    principal = getattr(request.state, "principal", None)
    if not isinstance(principal, Principal):
        raise Unauthorized(
            "endpoint requires authentication; no principal on request.state"
        )
    if not principal.is_admin():
        raise Forbidden(
            "endpoint is admin-gated; worker bearers MUST NOT access it (§13.3)"
        )
    return principal


def require_worker(request: Request) -> Principal:
    """Return the principal, requiring it to be a worker."""
    principal = getattr(request.state, "principal", None)
    if not isinstance(principal, Principal):
        raise Unauthorized(
            "endpoint requires authentication; no principal on request.state"
        )
    if not principal.is_worker():
        raise Forbidden(
            "endpoint is worker-gated; admin bearers MUST NOT access it (§13.3)"
        )
    return principal
