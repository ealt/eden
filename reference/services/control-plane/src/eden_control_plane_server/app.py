"""FastAPI app + Store factory for the EDEN control-plane server.

`make_app(store, *, admin_token=None, lease_duration_seconds=30)`
exposes the 19 chapter-07 §15 endpoints over `store` (a
`ControlPlaneStore`). Auth dispatch mirrors `eden_wire.server`'s
chapter-07 §13 pattern but with `store` as the
`verify_worker_credential` source for the deployment-scoped
worker registry per chapter 11 §6.
"""

from __future__ import annotations

import logging
from typing import Any

from eden_control_plane import (
    ControlPlaneStore,
    InMemoryControlPlaneStore,
    LeaseAcquireRequest,
    LeaseError,
    LeaseExpired,
    LeaseHeldByOther,
    LeaseInstanceMismatch,
    LeaseNotHeld,
    LeaseReleaseRequest,
    LeaseRenewRequest,
    PostgresControlPlaneStore,
    RegisterExperimentRequest,
)
from eden_storage.errors import (
    AlreadyExists,
    CycleDetected,
    InvalidPrecondition,
    NotFound,
    ReservedIdentifier,
)
from eden_wire.errors import (
    BadRequest,
    Forbidden,
    ProblemJson,
    Unauthorized,
    envelope_for_error,
)
from fastapi import Body, FastAPI, Path, Query, Request, Response
from fastapi.responses import JSONResponse

from .auth import (
    Principal,
    authenticate,
    require_admin,
    require_worker,
)
from .state_sync import StateSyncPoller

log = logging.getLogger(__name__)

PROBLEM_JSON = "application/problem+json"

__all__ = ["build_store", "make_app"]


def build_store(store_url: str) -> ControlPlaneStore:
    """Open a `ControlPlaneStore` backend selected by URL scheme.

    Dispatch:
    * `:memory:` → `InMemoryControlPlaneStore` (non-durable).
    * `postgresql://…` / `postgres://…` → `PostgresControlPlaneStore`.
    """
    if store_url == ":memory:":
        return InMemoryControlPlaneStore()
    if store_url.startswith("postgresql://") or store_url.startswith("postgres://"):
        return PostgresControlPlaneStore(store_url)
    msg = (
        f"unsupported store_url {store_url!r}; "
        f"expected ':memory:' or 'postgresql://…'"
    )
    raise ValueError(msg)


_ERROR_CLASSES_FOR_WIRE: dict[type[Exception], tuple[str, int, str]] = {
    LeaseHeldByOther: ("eden://error/lease-held-by-other", 409, "Lease Held By Other"),
    LeaseNotHeld: ("eden://error/lease-not-held", 410, "Lease Not Held"),
    LeaseExpired: ("eden://error/lease-expired", 410, "Lease Expired"),
    LeaseInstanceMismatch: (
        "eden://error/lease-instance-mismatch",
        409,
        "Lease Instance Mismatch",
    ),
}


def _envelope(exc: Exception, *, instance: str) -> ProblemJson:
    """Build a ProblemJson for the lease-specific OR eden-wire-known errors."""
    entry = _ERROR_CLASSES_FOR_WIRE.get(type(exc))
    if entry is not None:
        wire_type, status, title = entry
        return ProblemJson(
            type=wire_type,
            title=title,
            status=status,
            detail=str(exc) or None,
            instance=instance,
        )
    return envelope_for_error(exc, instance=instance)


def _json_error(exc: Exception, *, request: Request) -> JSONResponse:
    env = _envelope(exc, instance=str(request.url))
    return JSONResponse(
        status_code=env.status,
        media_type=PROBLEM_JSON,
        content=env.to_dict(),
    )


# ---------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------


def make_app(
    store: ControlPlaneStore,
    *,
    admin_token: str | None = None,
    lease_duration_seconds: int = 30,
    state_poller: StateSyncPoller | None = None,
) -> FastAPI:
    """Build the FastAPI app exposing chapter-07 §15 over `store`.

    `admin_token`, when non-`None`, installs the §13 normative
    authentication middleware: every `/v0/control/` request MUST
    carry a valid `Authorization: Bearer <principal>:<secret>`.
    When `None` (test posture), auth is bypassed — convenient for
    unit tests but NOT spec-conformant.

    `lease_duration_seconds` (default 30) is the deployment-wide
    chapter 11 §4.3 lease duration; every acquire/renew sets
    `expires_at = now + lease_duration_seconds`.

    `state_poller`, when non-`None`, is consulted by the
    `read_experiment_metadata` route to surface chapter 11 §3.4
    stale-state warnings, and by `acquire_lease` to trigger the §3.3
    on-demand state refresh. Production wires this up via
    `cli.main`; tests can pass a fake reader or omit the poller
    entirely.
    """
    app = FastAPI(title="EDEN control plane", version="0")

    if admin_token is not None:
        _install_auth_middleware(app, admin_token=admin_token, store=store)

    @app.exception_handler(Unauthorized)
    async def _h_unauthorized(request: Request, exc: Unauthorized) -> Response:
        return _json_error(exc, request=request)

    @app.exception_handler(Forbidden)
    async def _h_forbidden(request: Request, exc: Forbidden) -> Response:
        return _json_error(exc, request=request)

    @app.exception_handler(NotFound)
    async def _h_not_found(request: Request, exc: NotFound) -> Response:
        return _json_error(exc, request=request)

    @app.exception_handler(AlreadyExists)
    async def _h_already_exists(request: Request, exc: AlreadyExists) -> Response:
        return _json_error(exc, request=request)

    @app.exception_handler(InvalidPrecondition)
    async def _h_invalid_precondition(
        request: Request, exc: InvalidPrecondition
    ) -> Response:
        return _json_error(exc, request=request)

    @app.exception_handler(ReservedIdentifier)
    async def _h_reserved(request: Request, exc: ReservedIdentifier) -> Response:
        return _json_error(exc, request=request)

    @app.exception_handler(CycleDetected)
    async def _h_cycle(request: Request, exc: CycleDetected) -> Response:
        return _json_error(exc, request=request)

    @app.exception_handler(LeaseError)
    async def _h_lease(request: Request, exc: LeaseError) -> Response:
        return _json_error(exc, request=request)

    @app.exception_handler(BadRequest)
    async def _h_bad_request(request: Request, exc: BadRequest) -> Response:
        return _json_error(exc, request=request)

    # The auth-disabled "test posture" still needs gate helpers that
    # return useful Principal objects. We share one impl that respects
    # `admin_token is None`.
    def _enforce_admin(request: Request) -> Principal:
        if admin_token is None:
            return Principal(kind="admin", worker_id=None)
        return require_admin(request)

    def _enforce_worker(request: Request) -> Principal:
        if admin_token is None:
            # Permit a non-normative X-Eden-Worker-Id header for tests
            # that need a non-admin identity without configuring auth.
            return Principal(
                kind="worker",
                worker_id=request.headers.get("X-Eden-Worker-Id", "anonymous"),
            )
        return require_worker(request)

    def _get_principal(request: Request) -> Principal:
        if admin_token is None:
            return Principal(
                kind="worker",
                worker_id=request.headers.get("X-Eden-Worker-Id", "anonymous"),
            )
        principal = getattr(request.state, "principal", None)
        if not isinstance(principal, Principal):
            raise Unauthorized("no authenticated principal on request.state")
        return principal

    def _require_orchestrators(request: Request) -> Principal:
        """Worker-gated + deployment-scoped `orchestrators` group required."""
        principal = _enforce_worker(request)
        if admin_token is None:
            return principal
        assert principal.worker_id is not None
        if not store.resolve_worker_in_group(principal.worker_id, "orchestrators"):
            raise Forbidden(
                f"endpoint requires membership in 'orchestrators'; "
                f"worker {principal.worker_id!r} is not a transitive member"
            )
        return principal

    base = "/v0/control"

    # ------------------------------------------------------------------
    # §15.1 Experiment registry
    # ------------------------------------------------------------------

    @app.post(f"{base}/experiments")
    def register_experiment(
        request: Request, body: RegisterExperimentRequest = Body(...)
    ) -> Response:
        _enforce_admin(request)
        entry = store.register_experiment(body.experiment_id, body.config_uri)
        return _ok(entry, status=201)

    @app.delete(f"{base}/experiments/{{experiment_id}}")
    def unregister_experiment(
        request: Request, experiment_id: str = Path(...)
    ) -> Response:
        _enforce_admin(request)
        store.unregister_experiment(experiment_id)
        return Response(status_code=204)

    @app.get(f"{base}/experiments")
    def list_experiments(request: Request) -> Response:
        _get_principal(request)
        entries = store.list_experiments()
        body = {
            "experiments": [
                _dump(e) for e in entries
            ],
        }
        return JSONResponse(content=body)

    @app.get(f"{base}/experiments/{{experiment_id}}")
    def read_experiment_metadata(
        request: Request, experiment_id: str = Path(...)
    ) -> Response:
        _get_principal(request)
        entry = store.read_experiment_metadata(experiment_id)
        # §3.4: inject stale-state warning when the poller's
        # consecutive-failure counter has crossed the threshold.
        if state_poller is not None:
            warnings = state_poller.warnings.warnings_for(experiment_id)
            if warnings:
                body = entry.model_dump(mode="json", exclude_none=True)
                body["warnings"] = list(warnings)
                return JSONResponse(content=body)
        return _ok(entry)

    # ------------------------------------------------------------------
    # §15.2 Lease operations
    # ------------------------------------------------------------------

    @app.post(f"{base}/experiments/{{experiment_id}}/leases")
    def acquire_lease(
        request: Request,
        experiment_id: str = Path(...),
        body: LeaseAcquireRequest = Body(...),
    ) -> Response:
        principal = _require_orchestrators(request)
        # No impersonation: body.holder MUST equal the authenticated
        # worker_id. The `admin_token is None` test posture skips this
        # — Principal.worker_id is the header value.
        if (
            admin_token is not None
            and principal.worker_id is not None
            and body.holder != principal.worker_id
        ):
            raise Forbidden(
                f"body holder {body.holder!r} does not match authenticated "
                f"worker_id {principal.worker_id!r}"
            )
        lease = store.acquire_lease(
            experiment_id,
            body.holder,
            body.holder_instance,
            lease_duration_seconds=lease_duration_seconds,
        )
        # §3.3 on-demand state refresh: a freshly-leased experiment
        # MUST have up-to-date `last_known_state` regardless of the
        # polling cadence. Failure here is logged but not surfaced —
        # the next polling tick will reconcile.
        if state_poller is not None:
            try:
                state_poller.refresh_one(experiment_id)
            except Exception:  # noqa: BLE001 — defensive
                log.warning("acquire_lease_refresh_failed")
        return _ok(lease, status=201)

    @app.post(f"{base}/leases/{{lease_id}}/renew")
    def renew_lease(
        request: Request,
        lease_id: str = Path(...),
        body: LeaseRenewRequest = Body(...),
    ) -> Response:
        principal = _require_orchestrators(request)
        # Holder check: the authenticated worker_id MUST be the lease's
        # current holder. (The lease-not-held / instance-mismatch
        # errors below also catch impersonation, but this gate emits
        # `forbidden` for the wrong-worker case to match chapter 11.)
        try:
            lease = store.read_lease(lease_id)
        except NotFound:
            raise LeaseNotHeld(
                f"lease {lease_id!r} has been replaced or never existed"
            ) from None
        if (
            admin_token is not None
            and principal.worker_id is not None
            and lease.holder != principal.worker_id
        ):
            raise Forbidden(
                f"lease {lease_id!r} is held by {lease.holder!r}; "
                f"authenticated worker_id {principal.worker_id!r} cannot renew it"
            )
        renewed = store.renew_lease(
            lease_id,
            body.holder_instance,
            lease_duration_seconds=lease_duration_seconds,
        )
        return _ok(renewed)

    @app.post(f"{base}/leases/{{lease_id}}/release")
    def release_lease(
        request: Request,
        lease_id: str = Path(...),
        body: LeaseReleaseRequest = Body(...),
    ) -> Response:
        principal = _require_orchestrators(request)
        # Best-effort holder check: silently no-op on an unknown lease
        # to preserve `release_lease` idempotency (chapter 11 §4.5).
        try:
            lease = store.read_lease(lease_id)
        except NotFound:
            store.release_lease(lease_id, body.holder_instance)
            return JSONResponse(content={})
        if (
            admin_token is not None
            and principal.worker_id is not None
            and lease.holder != principal.worker_id
        ):
            raise Forbidden(
                f"lease {lease_id!r} is held by {lease.holder!r}; "
                f"authenticated worker_id {principal.worker_id!r} cannot release it"
            )
        store.release_lease(lease_id, body.holder_instance)
        return JSONResponse(content={})

    @app.get(f"{base}/leases")
    def list_active_leases(
        request: Request, holder: str = Query(...)
    ) -> Response:
        principal = _get_principal(request)
        # Authorization: caller MUST be authenticated as `holder` OR
        # be admin. Test posture (admin_token is None) skips this.
        if (
            admin_token is not None
            and not principal.is_admin()
            and principal.worker_id != holder
        ):
            raise Forbidden(
                f"holder filter {holder!r} does not match authenticated "
                f"worker_id {principal.worker_id!r}; cross-worker reads "
                f"are admin-only"
            )
        leases = store.list_active_leases(holder)
        return JSONResponse(content={"leases": [_dump(lease) for lease in leases]})

    # ------------------------------------------------------------------
    # §15.3 Deployment-scoped worker registry
    # ------------------------------------------------------------------

    @app.post(f"{base}/workers")
    def register_worker(request: Request, body: dict[str, Any] = Body(...)) -> Response:
        _enforce_admin(request)
        worker_id = body.get("worker_id")
        if not isinstance(worker_id, str) or not worker_id:
            raise BadRequest("body MUST include a non-empty 'worker_id' string")
        labels = body.get("labels")
        if labels is not None and not isinstance(labels, dict):
            raise BadRequest("'labels' MUST be an object when present")
        worker, token = store.register_worker(worker_id, labels=labels)
        out: dict[str, Any] = _dump(worker)
        if token is not None:
            out["registration_token"] = token
        return JSONResponse(content=out, status_code=201)

    @app.post(f"{base}/workers/{{worker_id}}/reissue-credential")
    def reissue_credential(
        request: Request, worker_id: str = Path(...)
    ) -> Response:
        _enforce_admin(request)
        token = store.reissue_credential(worker_id)
        worker = store.read_worker(worker_id)
        out: dict[str, Any] = _dump(worker)
        out["registration_token"] = token
        return JSONResponse(content=out)

    @app.get(f"{base}/workers")
    def list_workers(request: Request) -> Response:
        _enforce_admin(request)
        workers = store.list_workers()
        return JSONResponse(
            content={"workers": [_dump(w) for w in workers]}
        )

    @app.get(f"{base}/workers/{{worker_id}}")
    def read_worker(
        request: Request, worker_id: str = Path(...)
    ) -> Response:
        _enforce_admin(request)
        worker = store.read_worker(worker_id)
        return JSONResponse(content=_dump(worker))

    @app.get(f"{base}/whoami")
    def whoami(request: Request) -> Response:
        principal = _enforce_worker(request)
        return JSONResponse(content={"worker_id": principal.worker_id})

    # ------------------------------------------------------------------
    # §15.3 Deployment-scoped group registry
    # ------------------------------------------------------------------

    @app.post(f"{base}/groups")
    def register_group(
        request: Request, body: dict[str, Any] = Body(...)
    ) -> Response:
        _enforce_admin(request)
        group_id = body.get("group_id")
        if not isinstance(group_id, str) or not group_id:
            raise BadRequest("body MUST include a non-empty 'group_id' string")
        members = body.get("members")
        if members is not None and not isinstance(members, list):
            raise BadRequest("'members' MUST be an array when present")
        group = store.register_group(group_id, members=members)
        return JSONResponse(content=_dump(group), status_code=201)

    @app.post(f"{base}/groups/{{group_id}}/members")
    def add_to_group(
        request: Request,
        group_id: str = Path(...),
        body: dict[str, Any] = Body(...),
    ) -> Response:
        _enforce_admin(request)
        worker_id = body.get("worker_id")
        if not isinstance(worker_id, str) or not worker_id:
            raise BadRequest("body MUST include a non-empty 'worker_id' string")
        group = store.add_to_group(group_id, worker_id)
        return JSONResponse(content=_dump(group))

    @app.delete(f"{base}/groups/{{group_id}}/members/{{worker_id}}")
    def remove_from_group(
        request: Request,
        group_id: str = Path(...),
        worker_id: str = Path(...),
    ) -> Response:
        _enforce_admin(request)
        group = store.remove_from_group(group_id, worker_id)
        return JSONResponse(content=_dump(group))

    @app.delete(f"{base}/groups/{{group_id}}")
    def delete_group(
        request: Request, group_id: str = Path(...)
    ) -> Response:
        _enforce_admin(request)
        store.delete_group(group_id)
        return Response(status_code=204)

    @app.get(f"{base}/groups")
    def list_groups(request: Request) -> Response:
        _enforce_admin(request)
        groups = store.list_groups()
        return JSONResponse(content={"groups": [_dump(g) for g in groups]})

    @app.get(f"{base}/groups/{{group_id}}")
    def read_group(
        request: Request, group_id: str = Path(...)
    ) -> Response:
        _enforce_admin(request)
        group = store.read_group(group_id)
        return JSONResponse(content=_dump(group))

    return app


def _dump(model: Any) -> dict[str, Any]:
    """Serialize a Pydantic model via JSON-mode dump excluding None fields."""
    return model.model_dump(mode="json", exclude_none=True)


def _ok(model: Any, *, status: int = 200) -> JSONResponse:
    return JSONResponse(content=_dump(model), status_code=status)


# ---------------------------------------------------------------------
# Auth middleware (deployment-scoped registry as principal source)
# ---------------------------------------------------------------------


def _install_auth_middleware(
    app: FastAPI,
    *,
    admin_token: str,
    store: ControlPlaneStore,
) -> None:
    """Install §13 bearer auth using `store` for worker-credential verify.

    Parallel to `eden_wire.auth.install_auth_middleware` but the
    Store source is the control plane's deployment-scoped worker
    registry (chapter 11 §6), not the task-store-server's
    per-experiment registry.
    """

    @app.middleware("http")
    async def _auth_mw(
        request: Request, call_next: Any
    ) -> Response:
        # Only `/v0/control/...` paths are normative; skip everything
        # else (e.g. `/docs`) to keep parity with eden-wire's posture.
        if not request.url.path.startswith("/v0/control"):
            return await call_next(request)
        try:
            principal = authenticate(
                request.headers.get("authorization"),
                admin_token=admin_token,
                store=store,
            )
        except Unauthorized as exc:
            return _json_error(exc, request=request)
        request.state.principal = principal
        return await call_next(request)
