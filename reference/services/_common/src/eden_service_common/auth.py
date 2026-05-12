"""Worker-host registration + credential bootstrap (12a-1 wave 4).

Implements the §D.1 startup recovery flow from
[`docs/plans/eden-phase-12a-1-worker-identity.md`](../../../../../docs/plans/eden-phase-12a-1-worker-identity.md).
Each worker-host service calls :func:`bootstrap_worker_credential`
once at startup; the helper:

1. Looks for a persisted credential at ``<credentials_dir>/<worker_id>.token``.
2. If present, verifies it via ``GET /v0/.../whoami`` (the §6.4
   authenticated probe). On verify success → return the credential.
3. If missing or stale, uses the admin token (passed in or read from
   ``EDEN_ADMIN_TOKEN``) to either :py:meth:`Store.register_worker`
   (no persisted token) or :py:meth:`Store.reissue_credential` (stale
   token). Persists the new credential and returns it.

Per plan §8.2: there is **no fall-through to fresh register** on
credential failure — the existing registry row is the authority on
the worker's identity, and the only documented escape from a stale
credential is the explicit admin-gated reissue.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import os
import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from eden_wire import StoreClient, Unauthorized

DEFAULT_CREDENTIALS_DIR = Path("/var/lib/eden/credentials")
"""Reference deployment's per-host credentials directory.

Each worker host persists ``<worker_id>.token`` here. The directory
is per-host (a single Compose service container has one worker_id
and one token); volume layout is wave-6's concern.
"""


@dataclass(frozen=True)
class WorkerCredential:
    """The per-worker bearer assembled at startup.

    ``bearer`` is the §13.1 ``<worker_id>:<token>`` string used
    directly as the Authorization value (without the ``Bearer ``
    prefix; ``StoreClient`` adds that). ``token`` is retained as a
    convenience for callers (e.g. ``container_exec``) that thread
    just the secret half into spawned children's environment.
    """

    worker_id: str
    token: str

    @property
    def bearer(self) -> str:
        """The §13.1 ``<worker_id>:<token>`` bearer string."""
        return f"{self.worker_id}:{self.token}"


def credential_path(credentials_dir: Path, worker_id: str) -> Path:
    """Return the on-disk path for ``worker_id``'s persisted credential."""
    return credentials_dir / f"{worker_id}.token"


def _read_token(path: Path) -> str | None:
    if not path.exists():
        return None
    text = path.read_text().strip()
    return text or None


def _write_token(path: Path, token: str) -> None:
    """Persist ``token`` to ``path`` with mode 0600.

    Atomicity: write to ``<path>.<random-suffix>.tmp`` then
    ``os.replace`` so a crash mid-write doesn't leave a half-written
    file. The random suffix is load-bearing: two concurrent writers
    targeting the same ``path`` MUST NOT share a tmp filename, or
    one process's ``write_text`` would clobber the other's in-flight
    bytes and ``os.replace`` could lose the second write entirely.
    Permissions are locked to owner-rw to mirror the §13.5
    "token storage hygiene" requirement.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = secrets.token_hex(8)
    tmp = path.with_suffix(f"{path.suffix}.{suffix}.tmp")
    try:
        tmp.write_text(token)
        tmp.chmod(0o600)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


@contextlib.contextmanager
def _bootstrap_lock(credentials_dir: Path, worker_id: str) -> Iterator[None]:
    """Per-worker_id exclusive lock for the bootstrap critical section.

    Two startup processes that race to bootstrap the same worker_id
    would otherwise corrupt the persisted credential file and
    invalidate each other's freshly-issued tokens via the idempotent-
    register → reissue branch. The lock serializes the whole
    register/verify/reissue sequence on a per-worker_id basis.

    ``fcntl.flock`` is advisory but cooperatively respected by every
    caller of this helper — both processes go through
    :func:`bootstrap_worker_credential`. The lock file is left behind
    after release (cheap; one byte per registered worker_id).
    """
    credentials_dir.mkdir(parents=True, exist_ok=True)
    lock_path = credentials_dir / f"{worker_id}.token.lock"
    # ``a`` mode preserves the file across restarts; the lock is
    # released when the fd closes. No content is written.
    with open(lock_path, "a") as fp:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fp.fileno(), fcntl.LOCK_UN)


def bootstrap_worker_credential(
    *,
    base_url: str,
    experiment_id: str,
    worker_id: str,
    credentials_dir: Path,
    admin_token: str | None,
    labels: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> WorkerCredential:
    """Return a usable per-worker credential, registering if needed.

    Implements the §D.1 / §8.2 startup recovery flow:

    1. If a persisted token exists and verifies via ``/whoami``, use it.
    2. If a persisted token exists but is stale (401), reissue with
       ``admin_token`` and persist the new one.
    3. If no persisted token, register with ``admin_token`` and
       persist the issued one.

    Raises ``RuntimeError`` if step 2 or 3 needs the admin token but
    none is available (no ``--admin-token`` flag, no
    ``EDEN_ADMIN_TOKEN`` env var). The caller surfaces this to
    operator-readable startup failure.
    """
    if admin_token is None:
        admin_token = os.environ.get("EDEN_ADMIN_TOKEN")
    path = credential_path(credentials_dir, worker_id)

    # Serialize the bootstrap critical section per worker_id. Without
    # this, two concurrent startups for the same worker would (a) race
    # the persisted-token write and (b) take the idempotent-register
    # branch in opposite order, with the second arrival firing
    # ``reissue_credential`` and invalidating the first arrival's
    # freshly-issued token. The lock makes register/verify/reissue
    # observably linear per worker_id.
    with _bootstrap_lock(credentials_dir, worker_id):
        persisted = _read_token(path)

        if persisted is not None:
            bearer = f"{worker_id}:{persisted}"
            with StoreClient(
                base_url, experiment_id, bearer=bearer, timeout=timeout
            ) as probe:
                try:
                    returned_id = probe.whoami()
                except Unauthorized:
                    returned_id = None
            if returned_id == worker_id:
                return WorkerCredential(worker_id=worker_id, token=persisted)
            # Stale — escalate to reissue. Per §8.2: NO fall-through to
            # fresh-register; the registry row is authoritative.
            if admin_token is None:
                raise RuntimeError(
                    f"persisted credential for worker_id={worker_id!r} is stale "
                    f"(whoami returned {returned_id!r}); reissue requires the "
                    "admin token (set --admin-token or EDEN_ADMIN_TOKEN)"
                )
            with StoreClient(
                base_url,
                experiment_id,
                bearer=f"admin:{admin_token}",
                timeout=timeout,
            ) as admin:
                new_token = admin.reissue_credential(worker_id)
            _write_token(path, new_token)
            return WorkerCredential(worker_id=worker_id, token=new_token)

        # First run under this lock: no persisted token. Register
        # against the admin bearer.
        if admin_token is None:
            raise RuntimeError(
                f"no persisted credential for worker_id={worker_id!r} at {path}; "
                "registration requires the admin token (set --admin-token or "
                "EDEN_ADMIN_TOKEN)"
            )
        with StoreClient(
            base_url, experiment_id, bearer=f"admin:{admin_token}", timeout=timeout
        ) as admin:
            worker, registration_token = admin.register_worker(
                worker_id, labels=labels
            )
            if registration_token is None:
                # Idempotent re-register hit an existing row. Since we
                # hold the bootstrap lock and saw no persisted token
                # at the start of this critical section, the row was
                # written by an EARLIER startup whose credential file
                # was lost (operator wipe, fresh container volume,
                # etc.). Per §8.2 the only recovery is reissue. This
                # is safe under the lock — no concurrent bootstrap
                # for this worker_id is in flight to be invalidated.
                registration_token = admin.reissue_credential(worker_id)
            _write_token(path, registration_token)
            return WorkerCredential(
                worker_id=worker.worker_id, token=registration_token
            )


def resolve_worker_bearer(
    args: argparse.Namespace,
    *,
    worker_id: str,
    labels: dict[str, str] | None = None,
) -> str | None:
    """Return the bearer the host should use, registering as needed.

    Two postures:

    1. If ``args.admin_token`` (or ``$EDEN_ADMIN_TOKEN``) is set →
       run :func:`bootstrap_worker_credential` and return the §13.1
       ``<worker_id>:<token>`` bearer. This is the production
       deployment posture.
    2. Else → return ``None`` (auth disabled at the wire). Suitable
       for in-process / TestClient test postures where the
       task-store-server runs without ``--admin-token``.

    The resolver does not validate ``worker_id`` against the §6.1
    grammar; ``StoreClient.register_worker`` (and the admin register
    endpoint) does that and surfaces ``BadRequest`` /
    ``ReservedIdentifier`` errors as needed.
    """
    # Local import: cli.py imports auth.py for DEFAULT_CREDENTIALS_DIR,
    # so importing cli.py at module level here would create a cycle.
    from .cli import resolve_admin_token, resolve_credentials_dir

    admin_token = resolve_admin_token(args)
    if admin_token is None:
        return None
    credentials_dir = resolve_credentials_dir(args)
    credential = bootstrap_worker_credential(
        base_url=args.task_store_url,
        experiment_id=args.experiment_id,
        worker_id=worker_id,
        credentials_dir=credentials_dir,
        admin_token=admin_token,
        labels=labels,
    )
    return credential.bearer
