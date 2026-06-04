"""Per-``make_app`` dependency bundle and shared route guards.

The pre-F-3 monolithic :func:`eden_wire.server.make_app` nested every
route handler as a closure over ``store`` / ``admin_token`` /
``subscribe_*`` / the checkpoint substrate paths, plus four guard
closures (``_enforce_worker`` / ``_enforce_in_any_group`` /
``_stamp_created_by`` / ``_check_experiment``). F-3 (issue #115) splits
the handlers into per-resource ``routers/`` modules; this module is the
shared carrier for the captured dependencies and the guards every router
needs.

:class:`RouterDeps` is constructed exactly once per ``make_app`` call and
threaded into each ``build_router(deps)`` factory. It is frozen so an
accidental mutation surfaces as ``FrozenInstanceError`` rather than silent
cross-router state drift, and it is NOT exported through
``eden_wire.__init__`` — it is a wire-binding internal carrier.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eden_storage import ArtifactBackend, Store
from fastapi import Request

from .auth import require_worker
from .errors import BadRequest, ExperimentIdMismatch, Forbidden


@dataclass(frozen=True)
class RouterDeps:
    """Per-``make_app`` bundle threaded into each router factory.

    Mirrors the closure-capture set of the pre-F-3 monolithic
    ``make_app``: the ``Store``, the auth posture (``admin_token``;
    ``None`` disables auth — the test / in-process default), the §8.2
    long-poll knobs, and the optional checkpoint / artifact substrate
    roots.
    """

    store: Store
    admin_token: str | None
    subscribe_timeout: float
    subscribe_poll_interval: float
    artifact_root: Path | None
    checkpoint_repo_root: Path | None
    checkpoint_config_text: str
    credentials_dir_root: Path | None
    # Issue #166: the blob backend behind the §16 deposit / fetch
    # endpoints, plus the §16.1 deposit size cap (bytes).
    artifact_backend: ArtifactBackend
    max_artifact_bytes: int


def check_experiment(
    deps: RouterDeps, path_exp: str, header_exp: str | None
) -> None:
    """Enforce the chapter-7 §1.3 ``X-Eden-Experiment-Id`` invariant.

    Runs on every experiment-scoped route before any store access. The
    raised :class:`ExperimentIdMismatch` carries only a human-readable
    message; the route's problem+json ``instance`` is built from
    ``str(request.url)`` by the app-level exception handler, so this
    helper does not need the request URL.
    """
    if header_exp is None:
        raise ExperimentIdMismatch(
            f"missing X-Eden-Experiment-Id header (expected {path_exp!r})"
        )
    if header_exp != path_exp:
        raise ExperimentIdMismatch(
            f"X-Eden-Experiment-Id header {header_exp!r} does not match "
            f"URL segment {path_exp!r}"
        )
    if path_exp != deps.store.experiment_id:
        raise ExperimentIdMismatch(
            f"URL segment {path_exp!r} does not match server's experiment "
            f"{deps.store.experiment_id!r}"
        )


def enforce_worker(deps: RouterDeps, request: Request) -> None:
    """Worker-gated route guard (§13.3).

    When auth is disabled (``admin_token is None``), the middleware
    hasn't installed a principal and no enforcement runs — that's the
    in-process / TestClient posture. When auth is enabled, any admin
    bearer hitting a worker-gated route MUST 403 per the chapter-7 §13.3
    dispatcher contract.
    """
    if deps.admin_token is None:
        return
    require_worker(request)


def enforce_in_any_group(
    deps: RouterDeps, request: Request, group_ids: tuple[str, ...]
) -> str:
    """Worker-gated route guard plus group-membership check (§3.7).

    Requires the request to carry a worker bearer (admin bearers are
    rejected — these endpoints exist for operator workflows that the
    deployment surfaces through registered workers in ``admins`` /
    ``orchestrators``; the literal ``admin`` principal is a
    bootstrap-only identity for registry mgmt per 12a-1 §D.5). Then
    checks the worker's transitive membership in any of ``group_ids`` via
    ``Store.resolve_worker_in_group``; membership in ANY listed group
    passes (OR semantics).

    Returns the authenticated worker_id on success so the caller can
    stamp attribution fields (``reassigned_by`` / ``updated_by``).
    Returns the literal ``"anonymous"`` when auth is disabled (test
    posture) — group-membership enforcement is a no-op in that mode and
    the attribution stamp collapses to the sentinel.

    Raises :class:`Forbidden` (403 ``eden://error/forbidden``) on
    membership miss.
    """
    if deps.admin_token is None:
        return "anonymous"
    principal = require_worker(request)
    assert principal.worker_id is not None
    for gid in group_ids:
        if deps.store.resolve_worker_in_group(principal.worker_id, gid):
            return principal.worker_id
    groups_str = " or ".join(repr(g) for g in group_ids)
    raise Forbidden(
        f"endpoint requires membership in {groups_str}; worker "
        f"{principal.worker_id!r} is not a transitive member"
    )


def stamp_created_by(
    deps: RouterDeps,
    request: Request,
    body: dict[str, Any],
    field: str = "created_by",
) -> dict[str, Any]:
    """Stamp ``created_by`` on a create-* request body from the auth principal.

    Per chapter 02 §3.1 / §5.1, ``created_by`` records the actor
    identifier of the caller that produced the artifact. To prevent a
    client from spoofing the attribution, the binding overrides the
    field with the authenticated principal's identity:

    - Worker bearer → ``created_by = principal.worker_id``.
    - Admin bearer → ``created_by = "admin"`` (the §13.1 admin principal
      name; carried through chapter 02 §3.1 / §5.1).

    If the body supplied a different ``created_by`` value, the binding
    rejects with `BadRequest`. When auth is disabled (test / in-process
    posture, ``admin_token is None``), the body is passed through
    unchanged so existing test fixtures keep working.
    """
    if deps.admin_token is None:
        return body
    principal = getattr(request.state, "principal", None)
    if principal is None or not hasattr(principal, "is_worker"):
        return body
    if principal.is_worker():
        assert principal.worker_id is not None
        stamp = principal.worker_id
    else:
        stamp = "admin"
    supplied = body.get(field)
    if supplied is not None and supplied != stamp:
        raise BadRequest(
            f"{field}={supplied!r} disagrees with authenticated "
            f"principal {stamp!r}; the binding overrides this "
            f"field from the bearer's identity per chapter 02 §3.1"
        )
    return {**body, field: stamp}


def worker_id_from_request(request: Request) -> str:
    """Extract the authenticated ``worker_id`` from a request.

    When auth is enabled (``admin_token`` was passed to ``make_app``),
    the §13 middleware sets ``request.state.principal``; this helper
    returns the principal's ``worker_id`` and rejects admin bearers on
    worker-gated routes (§13.3) with :class:`Forbidden`.

    When auth is disabled (test / in-process default), there is no
    principal on ``request.state``; this helper returns the sentinel
    ``"anonymous"``. Tests that need per-worker identity MUST opt into
    auth-enabled mode by passing ``admin_token`` to ``make_app`` and
    authenticating with per-worker bearers.
    """
    principal = getattr(request.state, "principal", None)
    if principal is not None:
        if not principal.is_worker():
            raise Forbidden(
                "endpoint is worker-gated; admin bearers MUST NOT access it (§13.3)"
            )
        assert principal.worker_id is not None
        return principal.worker_id
    # Auth disabled — collapse all callers onto the sentinel.
    return "anonymous"
