"""Web-ui credential-directory resolution + control-plane bootstrap.

Two credential domains matter to the multi-experiment web-ui (issue
#145 §3.2):

1. **Per-experiment worker credentials** — one ``<worker_id>.token`` per
   experiment, bootstrapped JIT against the *task-store-server* by
   :class:`eden_web_ui.store_factory.BearerCache`, which reuses
   :func:`eden_service_common.auth.bootstrap_worker_credential` verbatim.
   This module does NOT touch that path.

2. **The deployment-scoped control-plane credential** — one long-lived
   worker the ``ControlPlaneClient`` uses for its read calls
   (``list_experiments`` / ``read_experiment_metadata``), so the
   switcher keeps working after the operator rotates the admin token
   out of the runtime environment (Posture C). The control plane is a
   different transport than the task-store-server, so
   ``bootstrap_worker_credential`` cannot be reused; this module
   provides :func:`bootstrap_control_plane_credential`, which mirrors
   its register / verify / reissue shape against the
   :class:`~eden_control_plane.ControlPlaneClient`. The lock and
   atomic-write disciplines ARE reused from ``eden_service_common.auth``
   (never reimplemented). Those two reads gate on *any* authenticated
   principal (``_get_principal`` — any registered worker bearer or the
   admin token), not membership in a specific group, so the bootstrap
   only needs to register the worker; no group-add is required.

Credential layout under ``<credential-dir>`` (resolved by
:func:`resolve_credential_dir`)::

    <credential-dir>/
      control-plane/<cp-worker-id>.token        # deployment-scoped
      control-plane/<cp-worker-id>.token.lock
      <experiment_id>/<worker-id>.token          # per-experiment (BearerCache)
      <experiment_id>/<worker-id>.token.lock
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from eden_control_plane import ControlPlaneClient
from eden_service_common.auth import (
    WorkerCredential,
    _bootstrap_lock,
    _read_token,
    _write_token,
    credential_path,
)
from eden_wire import Unauthorized
from eden_wire.errors import WireError as ControlPlaneWireError

CONTROL_PLANE_SCOPE = "control-plane"
"""Subdirectory under the credential dir holding the deployment-scoped
control-plane worker credential."""


def default_credential_dir() -> Path:
    """The XDG-based default for the web-ui credential directory."""
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home) if state_home else Path.home() / ".local" / "state"
    return base / "eden" / "web-ui"


def resolve_credential_dir(args: argparse.Namespace) -> Path:
    """Resolve the base credential dir for the web-ui's per-experiment tokens.

    Precedence (first set wins):

    1. ``--credential-dir`` / ``$EDEN_CREDENTIAL_DIR`` — the web-ui-specific
       override (issue #145).
    2. ``--credentials-dir`` / ``$EDEN_WORKER_CREDENTIALS_DIR`` — the common
       worker-host credential dir. The web-ui IS a worker host, so it shares
       this dir with the other reference hosts by default; honoring it keeps
       deployments (and the isolated per-test dirs the e2e suite passes) on a
       single credential location. Per-experiment tokens live under
       ``<base>/<experiment_id>/<worker_id>.token``.
    3. ``${XDG_STATE_HOME:-~/.local/state}/eden/web-ui/`` — the final fallback
       when no credential dir is configured at all.
    """
    candidates = (
        getattr(args, "credential_dir", None),
        os.environ.get("EDEN_CREDENTIAL_DIR"),
        getattr(args, "credentials_dir", None),
        os.environ.get("EDEN_WORKER_CREDENTIALS_DIR"),
    )
    for candidate in candidates:
        if candidate:
            return Path(candidate)
    return default_credential_dir()


def bootstrap_control_plane_credential(
    *,
    base_url: str,
    worker_id: str,
    credential_dir: Path,
    admin_token: str | None,
    timeout: float = 30.0,
) -> WorkerCredential:
    """Return the deployment-scoped control-plane worker credential.

    Mirrors :func:`eden_service_common.auth.bootstrap_worker_credential`
    but against the control plane (chapter 11 §6):

    1. If a persisted ``<credential-dir>/control-plane/<worker_id>.token``
       verifies via ``GET /v0/control/whoami``, use it.
    2. If persisted but stale (401), reissue with ``admin_token``.
    3. If absent, register with ``admin_token`` (``POST
       /v0/control/workers``) and persist the issued token.

    The switcher's reads (``list_experiments`` /
    ``read_experiment_metadata``) accept any authenticated principal, so
    no group membership is needed beyond a successful registration.

    Raises ``RuntimeError`` when a register / reissue is required but no
    admin token is available — the caller surfaces this as a startup
    warning (Posture D: switcher hidden) rather than a hard failure.
    """
    scope_dir = credential_dir / CONTROL_PLANE_SCOPE
    path = credential_path(scope_dir, worker_id)
    with _bootstrap_lock(scope_dir, worker_id):
        persisted = _read_token(path)
        if persisted is not None:
            bearer = f"{worker_id}:{persisted}"
            if _verify_control_plane(base_url, bearer, worker_id, timeout):
                return WorkerCredential(worker_id=worker_id, token=persisted)
            if admin_token is None:
                raise RuntimeError(
                    f"persisted control-plane credential for worker_id="
                    f"{worker_id!r} is stale; reissue requires the admin token"
                )
            new_token = _reissue_control_plane(
                base_url, admin_token, worker_id, timeout
            )
            _write_token(path, new_token)
            return WorkerCredential(worker_id=worker_id, token=new_token)

        if admin_token is None:
            raise RuntimeError(
                f"no persisted control-plane credential for worker_id="
                f"{worker_id!r} at {path}; registration requires the admin token"
            )
        token = _register_control_plane(base_url, admin_token, worker_id, timeout)
        _write_token(path, token)
        return WorkerCredential(worker_id=worker_id, token=token)


def _verify_control_plane(
    base_url: str, bearer: str, worker_id: str, timeout: float
) -> bool:
    with ControlPlaneClient(base_url, bearer=bearer, timeout=timeout) as probe:
        try:
            return probe.whoami() == worker_id
        except Unauthorized:
            return False
        except ControlPlaneWireError:
            return False


def _reissue_control_plane(
    base_url: str, admin_token: str, worker_id: str, timeout: float
) -> str:
    with ControlPlaneClient(
        base_url, bearer=f"admin:{admin_token}", timeout=timeout
    ) as admin:
        data = admin.reissue_credential(worker_id)
    token = data.get("registration_token")
    if not isinstance(token, str):
        raise RuntimeError("control-plane reissue_credential returned no token")
    return token


def _register_control_plane(
    base_url: str, admin_token: str, worker_id: str, timeout: float
) -> str:
    with ControlPlaneClient(
        base_url, bearer=f"admin:{admin_token}", timeout=timeout
    ) as admin:
        data = admin.register_worker(worker_id, labels={"role": "web-ui"})
        token = data.get("registration_token")
        if token is None:
            # Idempotent re-register hit an existing row with no fresh
            # token; the only recovery is reissue (mirrors the
            # task-store bootstrap's idempotent-register branch).
            reissued = admin.reissue_credential(worker_id)
            token = reissued.get("registration_token")
    if not isinstance(token, str):
        raise RuntimeError("control-plane register_worker returned no token")
    return token
