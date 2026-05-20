"""Cross-experiment admin views (chapter 11 §2 / §3 / §4).

When the web-ui is configured with `--control-plane-url`, this module
registers the deployment-wide views the operator uses to:

- See every registered experiment + its current `last_known_state`
  + lease holder (read-only dashboard at `/admin/experiments/`).
- Register / unregister experiments against the control plane
  (admin-only, behind CSRF).
- Force-release a wedged lease (admin-only, behind CSRF). Useful
  when an orchestrator replica is wedged and won't release on its
  own; the natural lease expiry is the alternative.

The session also gains a `selected_experiment_id` field via
`POST /admin/experiments/{experiment_id}/select`. The session field
is wired up to be load-bearing in a future refactor that swaps the
per-route store binding per session; v0 records the selection but
the existing routes still operate against the deployment's default
experiment.
"""

from __future__ import annotations

from typing import Any

from eden_control_plane import ControlPlaneClient
from eden_storage.errors import (
    AlreadyExists,
    InvalidPrecondition,
    NotFound,
)
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..sessions import Session, new_csrf_token
from ._helpers import (
    csrf_ok,
    get_session,
    htmx_aware_redirect,
    write_session_cookie,
)

router = APIRouter(prefix="/admin/experiments")


_REGISTER_OUTCOMES: dict[str, tuple[str, str]] = {
    "ok": ("ok", "experiment registered"),
    "already-exists": (
        "error",
        "an experiment with that id is already registered under a different "
        "config_uri; pick a different id or reuse the existing one",
    ),
    "missing-experiment-id": ("error", "experiment_id is required"),
    "missing-config-uri": ("error", "config_uri is required"),
    "transport": (
        "error",
        "transport failure; refresh and verify whether the registration landed",
    ),
}

_UNREGISTER_OUTCOMES: dict[str, tuple[str, str]] = {
    "ok": ("ok", "experiment unregistered"),
    "invalid-precondition": (
        "error",
        "experiment cannot be unregistered: not terminated, or an active "
        "lease exists",
    ),
    "not-found": ("error", "no such experiment"),
    "transport": ("error", "transport failure; refresh and verify"),
}

_SELECT_OUTCOMES: dict[str, tuple[str, str]] = {
    "ok": ("ok", "experiment selected"),
    "missing-experiment-id": ("error", "experiment_id is required"),
}


def _outcome(
    outcomes: dict[str, tuple[str, str]], key: str | None
) -> tuple[str, str] | None:
    if key is None:
        return None
    return outcomes.get(key)


def _require_session(request: Request) -> Session | RedirectResponse:
    """Return the decoded session, or a redirect to /signin."""
    session = get_session(request)
    if session is None:
        return RedirectResponse(url="/signin", status_code=303)
    return session


def _control_plane(request: Request) -> ControlPlaneClient:
    cp: ControlPlaneClient | None = request.app.state.control_plane
    assert cp is not None, "admin_experiments routes registered without control_plane"
    return cp


# ---------------------------------------------------------------------
# Dashboard (read-only)
# ---------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse, response_model=None)
async def dashboard(
    request: Request,
    registered: str | None = None,
    unregistered: str | None = None,
    selected: str | None = None,
) -> Response:
    """Cross-experiment dashboard.

    One row per registered experiment; each row shows the
    experiment_id, last-known state, current lease holder (or none),
    and per-row admin actions (unregister, force-release lease,
    select for this session).
    """
    session_or_redirect = _require_session(request)
    if isinstance(session_or_redirect, RedirectResponse):
        return session_or_redirect
    session = session_or_redirect
    cp = _control_plane(request)
    rows: list[dict[str, Any]] = []
    transport_error: str | None = None
    try:
        experiments = cp.list_experiments()
    except Exception as exc:  # noqa: BLE001 — surface to operator
        experiments = []
        transport_error = f"failed to read experiments from control plane: {exc}"
    for entry in experiments:
        rows.append(
            {
                "experiment_id": entry.experiment_id,
                "config_uri": entry.config_uri,
                "created_at": entry.created_at,
                "last_known_state": entry.last_known_state,
                "lease": entry.lease,
                "warnings": entry.warnings or [],
                "is_selected": entry.experiment_id
                == session.selected_experiment_id,
            }
        )
    outcomes: list[tuple[str, str]] = []
    for key, label in (
        (registered, _REGISTER_OUTCOMES),
        (unregistered, _UNREGISTER_OUTCOMES),
        (selected, _SELECT_OUTCOMES),
    ):
        result = _outcome(label, key)
        if result is not None:
            outcomes.append(result)
    return request.app.state.templates.TemplateResponse(
        request,
        "admin_experiments.html",
        {
            "session": session,
            "rows": rows,
            "transport_error": transport_error,
            "outcomes": outcomes,
        },
    )


# ---------------------------------------------------------------------
# Mutations (admin-only; CSRF-protected)
# ---------------------------------------------------------------------


@router.post("/register", response_model=None)
async def register(
    request: Request,
    csrf_token: str = Form(default=""),
    experiment_id: str = Form(default=""),
    config_uri: str = Form(default=""),
) -> Response:
    session_or_redirect = _require_session(request)
    if isinstance(session_or_redirect, RedirectResponse):
        return session_or_redirect
    session = session_or_redirect
    if not csrf_ok(session, csrf_token):
        return htmx_aware_redirect(
            request, "/admin/experiments/?registered=transport"
        )
    if not experiment_id:
        return htmx_aware_redirect(
            request, "/admin/experiments/?registered=missing-experiment-id"
        )
    if not config_uri:
        return htmx_aware_redirect(
            request, "/admin/experiments/?registered=missing-config-uri"
        )
    cp = _control_plane(request)
    try:
        cp.register_experiment(experiment_id, config_uri)
    except AlreadyExists:
        return htmx_aware_redirect(
            request, "/admin/experiments/?registered=already-exists"
        )
    except Exception:  # noqa: BLE001 — transport / unknown
        return htmx_aware_redirect(
            request, "/admin/experiments/?registered=transport"
        )
    return htmx_aware_redirect(request, "/admin/experiments/?registered=ok")


@router.post("/{experiment_id}/unregister", response_model=None)
async def unregister(
    request: Request,
    experiment_id: str,
    csrf_token: str = Form(default=""),
) -> Response:
    session_or_redirect = _require_session(request)
    if isinstance(session_or_redirect, RedirectResponse):
        return session_or_redirect
    session = session_or_redirect
    if not csrf_ok(session, csrf_token):
        return htmx_aware_redirect(
            request, "/admin/experiments/?unregistered=transport"
        )
    cp = _control_plane(request)
    try:
        cp.unregister_experiment(experiment_id)
    except InvalidPrecondition:
        return htmx_aware_redirect(
            request, "/admin/experiments/?unregistered=invalid-precondition"
        )
    except NotFound:
        return htmx_aware_redirect(
            request, "/admin/experiments/?unregistered=not-found"
        )
    except Exception:  # noqa: BLE001 — transport
        return htmx_aware_redirect(
            request, "/admin/experiments/?unregistered=transport"
        )
    return htmx_aware_redirect(
        request, "/admin/experiments/?unregistered=ok"
    )


# Force-release was previously exposed here; removed per codex
# round-1 finding M6. Chapter 11 §9 explicitly defers admin-driven
# force-release to a future spec amendment, and the web-ui's admin
# bearer would have been rejected by the worker-gated
# `release_lease` endpoint anyway. Operators must wait for natural
# lease expiration (default `lease_duration_seconds`, 30s). A
# future amendment will introduce a real admin force-release
# endpoint with explicit safeguards; tracked at issue #104.


@router.post("/{experiment_id}/select", response_model=None)
async def select(
    request: Request,
    experiment_id: str,
    csrf_token: str = Form(default=""),
) -> Response:
    """Record the operator's selected experiment in the session.

    v0 records the selection but does NOT swap the per-route store
    binding — the existing routes still operate against the
    deployment's default experiment. The session field is exposed
    so a follow-up refactor can wire per-route routing.
    """
    session_or_redirect = _require_session(request)
    if isinstance(session_or_redirect, RedirectResponse):
        return session_or_redirect
    session = session_or_redirect
    if not csrf_ok(session, csrf_token):
        return htmx_aware_redirect(
            request, "/admin/experiments/?selected=missing-experiment-id"
        )
    if not experiment_id:
        return htmx_aware_redirect(
            request, "/admin/experiments/?selected=missing-experiment-id"
        )
    new_session = Session(
        worker_id=session.worker_id,
        csrf=session.csrf or new_csrf_token(),
        selected_experiment_id=experiment_id,
    )
    encoded = request.app.state.session_codec.encode(new_session)
    response = htmx_aware_redirect(request, "/admin/experiments/?selected=ok")
    write_session_cookie(
        response,
        encoded=encoded,
        secure=request.app.state.secure_cookies,
    )
    return response
