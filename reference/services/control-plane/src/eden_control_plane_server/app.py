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
    InvalidName,
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


_PROBLEM_JSON_EXCEPTION_TYPES: tuple[type[Exception], ...] = (
    Unauthorized,
    Forbidden,
    NotFound,
    AlreadyExists,
    InvalidPrecondition,
    ReservedIdentifier,
    InvalidName,
    CycleDetected,
    LeaseError,
    BadRequest,
)


def _install_problem_json_exception_handlers(app: FastAPI) -> None:
    """Register one ``_json_error`` handler per chapter-07 §16 problem type.

    Every entry in :data:`_PROBLEM_JSON_EXCEPTION_TYPES` maps to the
    same response shape (``_json_error``); registering them in a
    loop keeps the table compact and easy to extend.
    """
    async def _h(request: Request, exc: Exception) -> Response:
        return _json_error(exc, request=request)

    for exc_type in _PROBLEM_JSON_EXCEPTION_TYPES:
        app.add_exception_handler(exc_type, _h)


# slop-allow: FastAPI app factory; ~20 nested route handlers as closures
# over (store, admin_token, lease_duration_seconds, state_poller, gate
# helpers). Exception-handler block already extracted to
# _install_problem_json_exception_handlers (~36 lines saved). Further
# decomposition is symmetric with F-3 (eden-wire APIRouter regroup) —
# deferred to follow-up issue #115 so both wire-binding factories move
# together with the same Deps-object discipline. (audit L-E)
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

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        """Unauthenticated liveness probe.

        Lives outside the ``/v0/control`` prefix, so the §13 auth
        middleware (which only guards ``/v0/control/...``) lets it
        through. Mirrors the web-ui's ``/healthz`` so the Compose
        healthcheck can poll the control-plane the same way.
        """
        return {"status": "ok"}

    if admin_token is not None:
        _install_auth_middleware(app, admin_token=admin_token, store=store)

    _install_problem_json_exception_handlers(app)

    # The auth-disabled "test posture" still needs gate helpers that
    # return useful Principal objects. We share one impl that respects
    # `admin_token is None`.
    def _enforce_admin(request: Request) -> Principal:
        if admin_token is None:
            return Principal(kind="admin", worker_id=None)
        return require_admin(request)

    def _enforce_worker(request: Request) -> Principal:
        if admin_token is None:
            # Test / in-process posture: collapse to the anonymous
            # sentinel. Tests that need per-worker identity must
            # configure ``admin_token`` and authenticate via bearer.
            return Principal(kind="worker", worker_id="anonymous")
        return require_worker(request)

    def _get_principal(request: Request) -> Principal:
        if admin_token is None:
            return Principal(kind="worker", worker_id="anonymous")
        principal = getattr(request.state, "principal", None)
        if not isinstance(principal, Principal):
            raise Unauthorized("no authenticated principal on request.state")
        return principal

    def _require_orchestrators(request: Request) -> Principal:
        """Worker-gated + deployment-scoped `orchestrators` group required.

        Since the identity rename (#128), group ids are opaque,
        system-minted `grp_*` values; the authority group is identified
        by its reserved display NAME (`orchestrators`). Resolve that
        name to its minted `group_id` via `list_groups(name=...)`, then
        check transitive membership. A reserved group not yet created
        resolves to no id and contributes no membership.
        """
        principal = _enforce_worker(request)
        if admin_token is None:
            return principal
        assert principal.worker_id is not None
        for group in store.list_groups(name="orchestrators"):
            if store.resolve_worker_in_group(principal.worker_id, group.group_id):
                return principal
        raise Forbidden(
            f"endpoint requires membership in 'orchestrators'; "
            f"worker {principal.worker_id!r} is not a transitive member"
        )

    base = "/v0/control"

    # ------------------------------------------------------------------
    # §15.1 Experiment registry
    # ------------------------------------------------------------------

    @app.post(f"{base}/experiments")
    def register_experiment(
        request: Request, body: RegisterExperimentRequest = Body(...)
    ) -> Response:
        _enforce_admin(request)
        # Identity rename (#128): the caller no longer supplies an
        # `experiment_id`; the server mints a fresh `exp_*` (chapter 11
        # §2). Each registration is a distinct entry, so `created` is
        # always True (the old idempotent-replay branch is moot once
        # ids are minted). The optional `name` is an operator-supplied
        # display label (data-model §1.7).
        entry, created = store.register_experiment(
            body.config_uri, name=body.name
        )
        return _registry_response(entry, status=201 if created else 200)

    @app.delete(f"{base}/experiments/{{experiment_id}}")
    def unregister_experiment(
        request: Request, experiment_id: str = Path(...)
    ) -> Response:
        _enforce_admin(request)
        store.unregister_experiment(experiment_id)
        return Response(status_code=204)

    @app.get(f"{base}/experiments")
    def list_experiments(
        request: Request, name: str | None = Query(default=None)
    ) -> Response:
        _get_principal(request)
        # Optional ``?name=<n>`` exact, case-sensitive filter (§2.x):
        # resolve a registered experiment by display name.
        entries = store.list_experiments(name=name)
        # §3.4: inject stale-state warnings per entry so the cross-
        # experiment dashboard can render them in the same single
        # round trip. The shape mirrors read_experiment_metadata's
        # injection — same `warnings` field on each entry.
        out: list[dict[str, Any]] = []
        for entry in entries:
            payload = _dump_registry_entry(entry)
            if state_poller is not None:
                warnings = state_poller.warnings.warnings_for(entry.experiment_id)
                if warnings:
                    payload["warnings"] = list(warnings)
            out.append(payload)
        return JSONResponse(content={"experiments": out})

    @app.get(f"{base}/experiments/{{experiment_id}}")
    def read_experiment_metadata(
        request: Request, experiment_id: str = Path(...)
    ) -> Response:
        _get_principal(request)
        entry = store.read_experiment_metadata(experiment_id)
        body = _dump_registry_entry(entry)
        # §3.4: inject stale-state warning when the poller's
        # consecutive-failure counter has crossed the threshold.
        if state_poller is not None:
            warnings = state_poller.warnings.warnings_for(experiment_id)
            if warnings:
                body["warnings"] = list(warnings)
        return JSONResponse(content=body)

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
        # Identity rename (#128): the server mints the opaque `wkr_*`
        # id; the caller supplies only an optional display `name` and
        # deployment `labels`. The one-time `registration_token` is
        # always present (every mint creates a fresh credential).
        name = body.get("name")
        if name is not None and not isinstance(name, str):
            raise BadRequest("'name' MUST be a string when present")
        labels = body.get("labels")
        if labels is not None and not isinstance(labels, dict):
            raise BadRequest("'labels' MUST be an object when present")
        worker, token = store.register_worker(name, labels=labels)
        out: dict[str, Any] = _dump(worker)
        if token is not None:
            out["registration_token"] = token
        return JSONResponse(content=out)

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
    def list_workers(
        request: Request, name: str | None = Query(default=None)
    ) -> Response:
        _enforce_admin(request)
        # Optional ``?name=<n>`` exact, case-sensitive filter (§6.2).
        workers = store.list_workers(name=name)
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
        body: dict[str, Any] = {"worker_id": principal.worker_id}
        # Echo the worker's display name when one was registered (§6.4).
        # The anonymous test-posture principal has no registry row.
        if principal.worker_id is not None and principal.worker_id != "anonymous":
            try:
                worker = store.read_worker(principal.worker_id)
            except NotFound:
                worker = None
            if worker is not None and worker.name is not None:
                body["name"] = worker.name
        return JSONResponse(content=body)

    # ------------------------------------------------------------------
    # §15.3 Deployment-scoped group registry
    # ------------------------------------------------------------------

    @app.post(f"{base}/groups")
    def register_group(
        request: Request, body: dict[str, Any] = Body(...)
    ) -> Response:
        principal = _enforce_admin(request)
        # Identity rename (#128): the server mints the opaque `grp_*`
        # id; the caller supplies only an optional display `name` and
        # initial `members`. The deployment admin may create
        # reserved-named groups (the setup-experiment bootstrap path);
        # ordinary workers cannot reach this admin-gated route.
        name = body.get("name")
        if name is not None and not isinstance(name, str):
            raise BadRequest("'name' MUST be a string when present")
        members = body.get("members")
        if members is not None and not isinstance(members, list):
            raise BadRequest("'members' MUST be an array when present")
        allow_reserved = principal.is_admin()
        group = store.register_group(
            name, members=members, allow_reserved=allow_reserved
        )
        return JSONResponse(content=_dump(group))

    @app.post(f"{base}/groups/{{group_id}}/members")
    def add_to_group(
        request: Request,
        group_id: str = Path(...),
        body: dict[str, Any] = Body(...),
    ) -> Response:
        _enforce_admin(request)
        member_id = body.get("member_id")
        if not isinstance(member_id, str) or not member_id:
            raise BadRequest("body MUST include a non-empty 'member_id' string")
        group = store.add_to_group(group_id, member_id)
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
    def list_groups(
        request: Request, name: str | None = Query(default=None)
    ) -> Response:
        _enforce_admin(request)
        # Optional ``?name=<n>`` exact, case-sensitive filter (§7.2).
        groups = store.list_groups(name=name)
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


def _dump_registry_entry(entry: Any) -> dict[str, Any]:
    """Serialize a `RegisteredExperiment` with explicit `lease: null`.

    Codex round 6 BLOCKER: the chapter 11 §4.4 amended rule says
    `RegisteredExperiment.lease` MUST be `null` when no active lease
    exists. The default `exclude_none=True` projection omits the key
    entirely, which is a different wire shape from "present and
    null" — third-party clients that key-check on `lease` (rather
    than `.get("lease")`) would behave differently between the two
    shapes. Keep `lease` always present; only drop the optional
    `warnings` field when absent.
    """
    body = entry.model_dump(mode="json", exclude_none=True)
    if "lease" not in body:
        body["lease"] = None
    return body


def _registry_response(entry: Any, *, status: int = 200) -> JSONResponse:
    """Build a JSON response from a `RegisteredExperiment`."""
    return JSONResponse(
        content=_dump_registry_entry(entry), status_code=status
    )


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
