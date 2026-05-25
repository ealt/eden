"""Cross-cutting ASGI middleware for the web-ui.

The admin-gate middleware enforces ``admins``-group membership at
every ``/admin/*`` path so that read-only pages (tasks / variants /
events / work-refs / workers / groups / experiments) cannot be
loaded by a non-admin operator. The wire layer already gates the
mutating endpoints; this gate closes the read-side leak per
issue #144.

A path-prefix middleware was chosen over per-handler decorators or
router-level dependencies so that any future route added under
``/admin/*`` is automatically gated — there is no way to forget the
check on a new handler. The handlers' own ``get_session`` redirects
remain in place as defense-in-depth and to satisfy the type checker
(handler bodies still need a non-``None`` session for CSRF and
worker_id reads).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from .routes._helpers import get_session

ADMINS_GROUP_ID = "admins"


def _is_admin_path(path: str) -> bool:
    return path == "/admin" or path.startswith("/admin/")


def _forbidden_response(request: Request) -> Response:
    return request.app.state.templates.TemplateResponse(
        request,
        "_error.html",
        {
            "title": "Forbidden",
            "message": (
                "This page requires membership in the `admins` group. "
                "Ask a deployment administrator to add your worker to "
                "the `admins` group, then refresh."
            ),
        },
        status_code=403,
    )


def _membership_check_failure_response(request: Request) -> Response:
    return request.app.state.templates.TemplateResponse(
        request,
        "_error.html",
        {
            "title": "Transport failure",
            "message": (
                "could not verify admins-group membership; refresh to "
                "retry. If the failure persists, check the "
                "task-store-server logs."
            ),
        },
        status_code=502,
    )


class AdminGateMiddleware(BaseHTTPMiddleware):
    """Reject ``/admin/*`` requests whose session is not in ``admins``.

    Order of checks:

    1. Non-``/admin`` paths pass through untouched.
    2. Missing or invalid session → 303 redirect to ``/signin`` (matches
       the per-handler behavior we are otherwise replacing).
    3. ``Store.resolve_worker_in_group(worker_id, "admins")`` raises →
       502 ``_error.html`` (same shape as the chunk-9e dashboard read
       failures; the operator refreshes to retry).
    4. Membership returns ``False`` → 403 ``_error.html``.
    5. Membership returns ``True`` → request proceeds.

    The membership check adds one extra wire round-trip per
    ``/admin/*`` page load when the store is a ``StoreClient``; that
    cost is acceptable at the reference-stack scale. If pages slow
    materially the per-request cache would attach to
    ``request.state``.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Apply the admin gate on /admin/* paths; pass through everything else."""
        if not _is_admin_path(request.url.path):
            return await call_next(request)
        session = get_session(request)
        if session is None:
            return RedirectResponse(url="/signin", status_code=303)
        store = request.app.state.store
        try:
            in_admins = store.resolve_worker_in_group(
                session.worker_id, ADMINS_GROUP_ID
            )
        except Exception:  # noqa: BLE001 — transport/store-domain
            return _membership_check_failure_response(request)
        if not in_admins:
            return _forbidden_response(request)
        return await call_next(request)


__all__ = ["AdminGateMiddleware", "ADMINS_GROUP_ID"]
