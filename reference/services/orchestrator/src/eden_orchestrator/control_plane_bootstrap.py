"""Deployment-scoped worker bootstrap for the control plane (chapter 11 §6).

Mirrors `eden_service_common.auth.bootstrap_worker_credential`'s
register/verify/reissue flow at the deployment scope: the
orchestrator registers itself against the chapter 11 §6 worker
registry, persists the issued credential, and uses the resulting
worker bearer for every chapter 11 §4.5 lease operation (which
the control plane gates on the deployment-scoped `orchestrators`
group, per chapter 07 §15.2).

The chapter 11 §6 surface is admin-gated for registration +
reissue; the orchestrator's bootstrap therefore requires
`--control-plane-admin-token` (or `$EDEN_CONTROL_PLANE_ADMIN_TOKEN`)
on first run. After the credential persists, subsequent restarts
re-authenticate via the persisted token alone (no admin token
required) — same posture as `bootstrap_worker_credential`.

The bootstrap also joins the deployment-scoped `orchestrators`
group, mirroring the per-experiment `_ensure_orchestrators_membership`
helper. Registration of the group is admin-gated; `AlreadyExists`
on the group register is silently swallowed.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from eden_control_plane import ControlPlaneClient
from eden_service_common import get_logger
from eden_storage.errors import AlreadyExists, NotFound
from eden_wire.errors import Unauthorized

log = get_logger(__name__)

__all__ = [
    "ControlPlaneCredential",
    "bootstrap_control_plane_worker",
]


@dataclass(frozen=True)
class ControlPlaneCredential:
    """One control-plane worker credential."""

    worker_id: str
    token: str

    @property
    def bearer(self) -> str:
        """The §13.1 bearer form for this credential."""
        return f"{self.worker_id}:{self.token}"


def _credential_path(credentials_dir: Path, worker_id: str) -> Path:
    return credentials_dir / "control-plane" / f"{worker_id}.token"


def _read_token(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        return None


def _write_token(path: Path, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


@contextmanager
def _bootstrap_lock(credentials_dir: Path, worker_id: str) -> Iterator[None]:
    """Serialize the bootstrap critical section per worker_id.

    Mirrors `eden_service_common.auth._bootstrap_lock`'s pattern:
    two concurrent startups would otherwise race the persisted-token
    write and the idempotent-register flow.
    """
    lock_dir = credentials_dir / "control-plane"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{worker_id}.lock"
    with lock_path.open("a+") as fp:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fp.fileno(), fcntl.LOCK_UN)


def bootstrap_control_plane_worker(
    *,
    control_plane_url: str,
    worker_id: str,
    credentials_dir: Path,
    admin_token: str | None,
    timeout: float = 30.0,
    labels: dict[str, str] | None = None,
) -> ControlPlaneCredential:
    """Return a usable deployment-scoped worker credential.

    Three branches (matches `bootstrap_worker_credential`):

    1. Persisted token verifies via `/v0/control/whoami` → use it.
    2. Persisted token stale → admin reissues, persist new token.
    3. No persisted token → admin registers, persist issued token.

    Raises `RuntimeError` when branches (2) or (3) need the admin
    token but none is available.
    """
    if admin_token is None:
        admin_token = os.environ.get("EDEN_CONTROL_PLANE_ADMIN_TOKEN") or os.environ.get(
            "EDEN_ADMIN_TOKEN"
        )
    path = _credential_path(credentials_dir, worker_id)

    with _bootstrap_lock(credentials_dir, worker_id):
        persisted = _read_token(path)
        if persisted is not None:
            bearer = f"{worker_id}:{persisted}"
            with ControlPlaneClient(
                control_plane_url, bearer=bearer, timeout=timeout
            ) as probe:
                returned_id: str | None
                try:
                    returned_id = probe.whoami()
                except Unauthorized:
                    returned_id = None
            if returned_id == worker_id:
                return ControlPlaneCredential(worker_id=worker_id, token=persisted)
            # Stale persisted token → reissue.
            if admin_token is None:
                msg = (
                    f"persisted control-plane credential for worker_id="
                    f"{worker_id!r} is stale (whoami returned "
                    f"{returned_id!r}); reissue requires the admin token "
                    "(set --control-plane-admin-token, "
                    "$EDEN_CONTROL_PLANE_ADMIN_TOKEN, or --admin-token)"
                )
                raise RuntimeError(msg)
            with ControlPlaneClient(
                control_plane_url,
                bearer=f"admin:{admin_token}",
                timeout=timeout,
            ) as admin:
                response = admin.reissue_credential(worker_id)
            new_token = response["registration_token"]
            _write_token(path, new_token)
            return ControlPlaneCredential(worker_id=worker_id, token=new_token)

        # No persisted token → register.
        if admin_token is None:
            msg = (
                f"no persisted control-plane credential for worker_id="
                f"{worker_id!r} at {path}; registration requires the admin "
                "token (set --control-plane-admin-token, "
                "$EDEN_CONTROL_PLANE_ADMIN_TOKEN, or --admin-token)"
            )
            raise RuntimeError(msg)
        with ControlPlaneClient(
            control_plane_url,
            bearer=f"admin:{admin_token}",
            timeout=timeout,
        ) as admin:
            register_response = admin.register_worker(worker_id, labels=labels)
            token = register_response.get("registration_token")
            if token is None:
                # Idempotent re-register: a prior credential exists in
                # the registry but is not persisted here (volume wipe,
                # fresh container, etc.). Per `bootstrap_worker_credential`
                # §D.1 the only safe recovery under the lock is reissue.
                reissue_response = admin.reissue_credential(worker_id)
                token = reissue_response["registration_token"]
            _write_token(path, token)
            return ControlPlaneCredential(worker_id=worker_id, token=token)


def ensure_orchestrators_group_membership(
    *,
    control_plane_url: str,
    worker_id: str,
    admin_token: str | None,
    timeout: float = 30.0,
) -> None:
    """Join the deployment-scoped `orchestrators` group.

    Mirrors `_ensure_orchestrators_membership` from the per-experiment
    bootstrap. Idempotent on existing group + existing membership.
    Skipped (with a WARN log) when `admin_token` is unavailable so
    the orchestrator can still run in test posture; the §15.2 lease
    ops then 403 and surface to the operator.
    """
    if admin_token is None:
        log.warning(
            "control_plane_orchestrators_membership_skipped",
            reason="no admin token; chapter 11 §6 group ops are admin-gated",
            worker_id=worker_id,
        )
        return
    with ControlPlaneClient(
        control_plane_url, bearer=f"admin:{admin_token}", timeout=timeout
    ) as admin:
        with contextlib.suppress(AlreadyExists):
            admin.register_group("orchestrators")
        try:
            admin.add_to_group("orchestrators", worker_id)
        except NotFound:
            # Race: group disappeared between register + add. Retry once.
            admin.register_group("orchestrators")
            admin.add_to_group("orchestrators", worker_id)
