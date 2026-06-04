"""Deployment-scoped worker bootstrap for the control plane (chapter 11 §6).

Unlike the per-experiment hosts (whose `wkr_*` ids are minted once by
setup-experiment and read from `.env`), the DEPLOYMENT-scoped
orchestrator worker has no setup-experiment that pre-mints it — the
orchestrator SELF-registers against the chapter 11 §6 worker registry.
Since the identity rename (#128), `register_worker` MINTS an opaque
`wkr_*`; a mint-always call on every restart would orphan the prior
identity, so this bootstrap:

1. Persists the minted `worker_id` locally (alongside the credential)
   the first time it self-registers under a given worker NAME.
2. On restart, reads the persisted `worker_id` back and verifies /
   reissues its credential — never re-registers (which would mint a
   different id).

The resulting worker bearer drives every chapter 11 §4.5 lease
operation (the control plane gates these on the deployment-scoped
`orchestrators` group, per chapter 07 §15.2).

The chapter 11 §6 surface is admin-gated for registration + reissue;
the orchestrator's bootstrap therefore requires
`--control-plane-admin-token` (or `$EDEN_CONTROL_PLANE_ADMIN_TOKEN`)
on first run. After the credential persists, subsequent restarts
re-authenticate via the persisted token alone (no admin token
required) — same posture as `bootstrap_worker_credential`.

The bootstrap also joins the deployment-scoped `orchestrators` group,
resolving the reserved group NAME to its minted `grp_*` id (#128) and
adding the minted worker id. Group creation is admin-gated;
`AlreadyExists` on the group register is silently swallowed.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from eden_control_plane import ControlPlaneClient
from eden_service_common import get_logger
from eden_storage.errors import AlreadyExists, NotFound, ReservedIdentifier
from eden_wire.errors import Unauthorized

log = get_logger(__name__)

__all__ = [
    "ControlPlaneCredential",
    "bootstrap_control_plane_worker",
    "ensure_orchestrators_group_membership",
    "worker_id_path",
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


def credential_path(credentials_dir: Path, worker_id: str) -> Path:
    """Return the on-disk path for the deployment-scoped credential."""
    return credentials_dir / "control-plane" / f"{worker_id}.token"


def _slugify(name: str) -> str:
    """Map a worker NAME to a filesystem-safe slug for the worker-id record.

    The persisted-worker-id file is keyed by the deployment worker NAME
    (the operator-stable handle), but display names can contain
    arbitrary visible Unicode. Reduce to a conservative
    ``[A-Za-z0-9._-]`` slug so the path is portable; collisions across
    distinct names are acceptable here because a deployment runs ONE
    orchestrator-name (the slug just needs to be stable per name).
    """
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in name)


def worker_id_path(credentials_dir: Path, name: str) -> Path:
    """Return the on-disk path recording the minted `worker_id` for `name`.

    The deployment-scoped orchestrator worker self-registers under a
    stable NAME; the minted opaque `worker_id` is persisted here so a
    restart reuses the same identity instead of minting a fresh one.
    """
    return credentials_dir / "control-plane" / f"{_slugify(name)}.worker-id"


def read_token(path: Path) -> str | None:
    """Return the persisted token (or worker-id) at `path`, or None when absent."""
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        return None


# Internal aliases retained for backward compat with existing call sites.
_credential_path = credential_path
_read_token = read_token


def _write_token(path: Path, token: str) -> None:
    """Persist `token` to `path` atomically with mode 0600.

    Mirrors `eden_service_common.auth._write_token`: write to a
    `<path>.<random>.tmp` file then `os.replace` so a crash
    mid-write doesn't leave a half-written file. Random suffix is
    load-bearing under concurrent writers.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = secrets.token_hex(8)
    tmp = path.with_suffix(f"{path.suffix}.{suffix}.tmp")
    try:
        tmp.write_text(token + "\n", encoding="utf-8")
        tmp.chmod(0o600)
        os.replace(tmp, path)
    finally:
        # Best-effort cleanup if the replace never ran (write_text
        # raised, chmod raised). The path may already be gone.
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


@contextmanager
def _bootstrap_lock(credentials_dir: Path, key: str) -> Iterator[None]:
    """Serialize the bootstrap critical section per lock `key`.

    Mirrors `eden_service_common.auth._bootstrap_lock`'s pattern:
    two concurrent startups would otherwise race the persisted-id /
    persisted-token writes and the self-register flow. Since the
    identity rename (#128) the worker_id is not known until AFTER the
    first register, so the lock keys on the deployment worker-NAME slug
    (stable across restarts) rather than the minted id.
    """
    lock_dir = credentials_dir / "control-plane"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{_slugify(key)}.lock"
    with lock_path.open("a+") as fp:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fp.fileno(), fcntl.LOCK_UN)


def bootstrap_control_plane_worker(
    *,
    control_plane_url: str,
    name: str,
    credentials_dir: Path,
    admin_token: str | None,
    timeout: float = 30.0,
    labels: dict[str, str] | None = None,
) -> ControlPlaneCredential:
    """Self-register (or recover) the deployment-scoped orchestrator worker.

    ``name`` is the operator-stable display NAME under which the
    orchestrator self-registers; the opaque ``worker_id`` is MINTED by
    the control plane (#128) and persisted locally so restarts reuse the
    same identity. On restart with a persisted worker_id it verifies the
    token via ``/v0/control/whoami`` and admin-reissues if the token is
    missing or stale (NEVER re-registering — that would mint a different
    id and orphan this one); with no persisted id it admin-registers by
    ``name``, captures the minted id, and persists BOTH the id and the
    token. Raises ``RuntimeError`` when a branch needs the admin token
    but none is available. (Per-branch detail in the inline comments.)
    """
    if admin_token is None:
        admin_token = os.environ.get("EDEN_CONTROL_PLANE_ADMIN_TOKEN") or os.environ.get(
            "EDEN_ADMIN_TOKEN"
        )
    id_path = worker_id_path(credentials_dir, name)

    with _bootstrap_lock(credentials_dir, name):
        persisted_worker_id = _read_token(id_path)

        if persisted_worker_id is not None:
            # Branches 1–3: we already know our minted identity; verify
            # or admin-reissue its credential. NEVER re-register (that
            # would mint a different id and orphan this one).
            token_path = _credential_path(credentials_dir, persisted_worker_id)
            persisted_token = _read_token(token_path)
            if persisted_token is not None:
                bearer = f"{persisted_worker_id}:{persisted_token}"
                with ControlPlaneClient(
                    control_plane_url, bearer=bearer, timeout=timeout
                ) as probe:
                    returned_id: str | None
                    try:
                        returned_id = probe.whoami()
                    except Unauthorized:
                        returned_id = None
                if returned_id == persisted_worker_id:
                    return ControlPlaneCredential(
                        worker_id=persisted_worker_id, token=persisted_token
                    )
            # Token missing or stale → admin-reissue for the known id.
            if admin_token is None:
                msg = (
                    f"persisted control-plane credential for worker_id="
                    f"{persisted_worker_id!r} (name {name!r}) is missing or "
                    f"stale; reissue requires the admin token (set "
                    "--control-plane-admin-token, "
                    "$EDEN_CONTROL_PLANE_ADMIN_TOKEN, or --admin-token)"
                )
                raise RuntimeError(msg)
            with ControlPlaneClient(
                control_plane_url,
                bearer=f"admin:{admin_token}",
                timeout=timeout,
            ) as admin:
                response = admin.reissue_credential(persisted_worker_id)
            new_token = response["registration_token"]
            _write_token(token_path, new_token)
            return ControlPlaneCredential(
                worker_id=persisted_worker_id, token=new_token
            )

        # Branch 4: no persisted worker_id → first self-registration.
        if admin_token is None:
            msg = (
                f"no persisted control-plane worker_id for name {name!r} at "
                f"{id_path}; first registration requires the admin token "
                "(set --control-plane-admin-token, "
                "$EDEN_CONTROL_PLANE_ADMIN_TOKEN, or --admin-token)"
            )
            raise RuntimeError(msg)
        with ControlPlaneClient(
            control_plane_url,
            bearer=f"admin:{admin_token}",
            timeout=timeout,
        ) as admin:
            register_response = admin.register_worker(name, labels=labels)
            worker_id = register_response["worker_id"]
            token = register_response["registration_token"]
            # Persist the minted id FIRST so a crash before the token
            # write still lets the next restart recover via reissue.
            _write_token(id_path, worker_id)
            _write_token(_credential_path(credentials_dir, worker_id), token)
            return ControlPlaneCredential(worker_id=worker_id, token=token)


def ensure_orchestrators_group_membership(
    *,
    control_plane_url: str,
    worker_id: str,
    admin_token: str | None,
    timeout: float = 30.0,
) -> None:
    """Join the deployment-scoped `orchestrators` group.

    Since the identity rename (#128), groups are addressed by their
    minted `grp_*` id; the `orchestrators` authority group is resolved
    by its reserved display NAME. This helper resolves (or admin-mints)
    the group, then adds the minted `worker_id` by id. Idempotent on
    existing membership. Skipped (with a WARN log) when `admin_token`
    is unavailable so the orchestrator can still run in test posture;
    the §15.2 lease ops then 403 and surface to the operator.
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
        group_id = _resolve_or_create_orchestrators(admin)
        try:
            admin.add_to_group(group_id, worker_id)
        except NotFound:
            # Race: group disappeared between resolve + add. Re-resolve
            # (re-creating if needed) and retry once.
            group_id = _resolve_or_create_orchestrators(admin)
            admin.add_to_group(group_id, worker_id)


def _resolve_or_create_orchestrators(admin: ControlPlaneClient) -> str:
    """Return the minted `grp_*` id for the reserved `orchestrators` group.

    Resolves the reserved NAME to its id via `list_groups(name=…)`;
    admin-mints the group when it does not yet exist. The mint races
    with a concurrent creator are benign — `list_groups` is re-read
    after a create attempt so the winning id is returned.
    """
    existing = admin.list_groups(name="orchestrators")
    if existing:
        return existing[0].group_id
    # A concurrent creator may win the race between our list and create.
    # The reserved-name uniqueness guard (§7.5 / §11 §6) surfaces that as
    # ReservedIdentifier ("the name is taken"); a plain duplicate surfaces
    # as AlreadyExists. Either way, re-read to return the winner's id.
    with contextlib.suppress(AlreadyExists, ReservedIdentifier):
        return admin.register_group("orchestrators").group_id
    return admin.list_groups(name="orchestrators")[0].group_id
