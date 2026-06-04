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

from .routes._helpers import ActiveContext, get_session, resolve_active_context

# The reserved display NAME of the admins group (identity rename #128).
# Group ids are now opaque (``grp_*``); the gate resolves this name to
# the group's minted id via ``list_groups(name=…)`` before checking
# transitive membership. Multiple groups MAY share a name in principle
# (names are not unique), so the gate treats a worker as an admin if it
# is a transitive member of ANY group named ``admins``.
ADMINS_GROUP_NAME = "admins"


def _worker_in_admins(store: object, worker_id: str) -> bool:
    """Return True iff ``worker_id`` is a transitive member of an ``admins`` group.

    Resolves the reserved ``admins`` NAME to its opaque ``grp_*`` id(s)
    via ``list_groups(name=…)`` (identity rename #128), then runs the
    existing transitive ``resolve_worker_in_group`` membership probe
    against each. Raises on transport/store failures (the caller maps
    that to a 502); returns False when no group named ``admins`` exists.
    """
    admins_groups = store.list_groups(name=ADMINS_GROUP_NAME)  # type: ignore[attr-defined]
    return any(
        store.resolve_worker_in_group(worker_id, group.group_id)  # type: ignore[attr-defined]
        for group in admins_groups
    )


def _is_admin_path(path: str) -> bool:
    return path == "/admin" or path.startswith("/admin/")


# Deployment-scoped admin pages operate against the control plane, not a
# per-experiment store (the cross-experiment dashboard + the deployment
# worker/group registries). They are also the redirect target for
# active-experiment resolution failures, so they MUST NOT be subject to
# per-experiment resolution themselves — otherwise a stale/unreachable
# selection would redirect the dashboard to itself in a loop. Their
# admins-group gate runs against the deployment-default experiment,
# preserving the pre-#145 behavior.
_DEPLOYMENT_SCOPED_ADMIN_PREFIXES = ("/admin/experiments", "/admin/control")


def _is_deployment_scoped_admin_path(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in _DEPLOYMENT_SCOPED_ADMIN_PREFIXES)


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
    3. The active experiment cannot be resolved (stale selection,
       control-plane / task-store unreachable, or a missing credential)
       → the dashboard redirect / unseeded page that
       ``resolve_active_context`` returns. The admins-group membership
       check runs against the ACTIVE experiment's worker store, so the
       gate follows the operator's experiment selection (issue #145).
    4. ``Store.resolve_worker_in_group(worker_id, "admins")`` raises →
       502 ``_error.html`` (same shape as the chunk-9e dashboard read
       failures; the operator refreshes to retry).
    5. Membership returns ``False`` → 403 ``_error.html``.
    6. Membership returns ``True`` → request proceeds.

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
        if _is_deployment_scoped_admin_path(request.url.path):
            # Gate the deployment-scoped pages against the default
            # experiment's store; they don't follow the active selection.
            store = request.app.state.store_factory.for_experiment(
                request.app.state.experiment_id, role="worker"
            )
            assert store is not None  # worker role always returns a store
        else:
            active = resolve_active_context(request)
            if not isinstance(active, ActiveContext):
                # A dashboard redirect (stale / unreachable / credential)
                # or the unseeded-experiment page — surface it directly.
                # Both short-circuit the membership check, which has no
                # store to run against in those states.
                return active
            store = active.store
        try:
            in_admins = _worker_in_admins(store, session.worker_id)
        except Exception:  # noqa: BLE001 — transport/store-domain
            return _membership_check_failure_response(request)
        if not in_admins:
            return _forbidden_response(request)
        return await call_next(request)


__all__ = ["AdminGateMiddleware", "ADMINS_GROUP_NAME"]
