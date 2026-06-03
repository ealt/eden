"""Worker-host credential bootstrap (identity rename #128).

After the identity rename, every per-experiment infra worker
(operator / orchestrator / web-ui / ideator-host / executor-host /
evaluator-host) is **minted once by setup-experiment**, which writes
the opaque ``wkr_*`` id into ``.env`` (the service reads it via the
appropriate ``EDEN_*_WORKER_ID`` env var) and the registration token
into ``<credentials_dir>/<worker_id>.token``.

A service therefore NEVER fresh-registers a per-experiment worker: a
mint-always ``register_worker`` would produce a *different* opaque id
than the one setup baked into ``.env``, severing the persisted
identity. Each worker-host service calls
:func:`bootstrap_worker_credential` once at startup with the
setup-minted ``worker_id``; the helper:

1. Looks for a persisted credential at ``<credentials_dir>/<worker_id>.token``.
2. If present, verifies it via ``GET /v0/.../whoami`` (the §6.4
   authenticated probe — now returning a ``WhoamiResult`` whose
   ``.worker_id`` must equal the configured id). On verify success →
   return the credential.
3. If missing or stale, the registry row already exists (setup minted
   it), so recovery is an admin-gated :py:meth:`Store.reissue_credential`
   keyed on the known ``worker_id``. The freshly-issued token is
   persisted and returned.

There is **no fresh-register fallback**: the registry row is the
authority on the worker's identity, and the only documented escape
from a missing/stale credential is the admin-gated reissue.
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
    labels: dict[str, str] | None = None,  # noqa: ARG001 — setup mints with labels; this helper only verifies/reissues
    timeout: float = 30.0,
) -> WorkerCredential:
    """Return a usable per-worker credential for the setup-minted ``worker_id``.

    ``labels`` is retained for caller-signature parity (the host CLIs
    still pass a ``{"role": …}`` map) but is no longer applied here:
    setup-experiment minted the worker WITH its labels, and this helper
    only verifies the persisted token or admin-reissues the credential —
    neither path mutates the registry row's labels.

    Implements the #128 startup recovery flow. ``worker_id`` is the
    opaque ``wkr_*`` id setup-experiment minted and baked into ``.env``;
    this helper never mints a new one (that would diverge from the
    persisted identity):

    1. If a persisted token exists and verifies via ``/whoami``
       (``WhoamiResult.worker_id == worker_id``), use it.
    2. If a persisted token exists but is stale (401, or ``/whoami``
       returns a different id), reissue with ``admin_token`` and
       persist the new one. The registry row already exists.
    3. If no persisted token, the local credential was lost (volume
       wipe, fresh container) but the registry row was minted by setup
       — recover via admin-gated ``reissue_credential(worker_id)`` and
       persist the issued token. There is NO fresh-register fallback.

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
                    # whoami() now returns a WhoamiResult; the opaque
                    # worker_id lives on `.worker_id`.
                    returned_id = probe.whoami().worker_id
                except Unauthorized:
                    returned_id = None
            if returned_id == worker_id:
                return WorkerCredential(worker_id=worker_id, token=persisted)
            # Stale — escalate to reissue. NO fall-through to
            # fresh-register; the registry row (minted by setup) is
            # authoritative on this worker's identity.
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

        # First run under this lock: no persisted token. The worker_id
        # was minted by setup-experiment and already has a registry
        # row; a missing local token means the credential was lost
        # (volume wipe, fresh container). Per #128 the ONLY recovery
        # is the admin-gated reissue — a fresh register would mint a
        # DIFFERENT opaque id and sever the persisted identity.
        if admin_token is None:
            raise RuntimeError(
                f"no persisted credential for worker_id={worker_id!r} at {path}; "
                "the worker is minted by setup-experiment, so recovery requires "
                "the admin token to reissue its credential (set --admin-token "
                "or EDEN_ADMIN_TOKEN)"
            )
        with StoreClient(
            base_url, experiment_id, bearer=f"admin:{admin_token}", timeout=timeout
        ) as admin:
            registration_token = admin.reissue_credential(worker_id)
            _write_token(path, registration_token)
            return WorkerCredential(
                worker_id=worker_id, token=registration_token
            )


def resolve_worker_bearer(
    args: argparse.Namespace,
    *,
    worker_id: str,
    labels: dict[str, str] | None = None,
) -> str | None:
    """Return the bearer the host should use, verifying/reissuing as needed.

    ``worker_id`` is the setup-minted opaque ``wkr_*`` id (read from
    the service's ``EDEN_*_WORKER_ID`` env var); this resolver never
    fresh-registers. Three postures (evaluated in order):

    1. ``args.admin_token`` (or ``$EDEN_ADMIN_TOKEN``) is set → run
       :func:`bootstrap_worker_credential` and return the §13.1
       ``<worker_id>:<token>`` bearer. This is the production
       deployment posture; bootstrap covers persisted-verify +
       admin-gated reissue (NO fresh-register — setup minted the row).
    2. No admin token, but a persisted credential exists at
       ``<credentials_dir>/<worker_id>.token`` → run bootstrap
       without an admin token. Bootstrap's first branch verifies the
       persisted token via ``/whoami`` and returns the bearer
       without needing admin access. This is the production
       restart-with-existing-credential posture (operator removed
       ``EDEN_ADMIN_TOKEN`` from the host's environment after
       initial provisioning; the "no fresh-register" rule remains
       intact because bootstrap raises if the persisted token is
       stale and reissue would be needed).
    3. Neither admin token nor persisted credential → return
       ``None``. Suitable for in-process / TestClient test postures
       where the task-store-server runs without ``--admin-token``;
       the wire's worker_id falls back to the
       :func:`_worker_id_from_request` shim's header read.

    The resolver does not validate ``worker_id`` against the opaque
    ``wkr_*`` grammar; the wire's whoami/reissue endpoints surface a
    ``NotFound`` if the configured id has no registry row.
    """
    # Local import: cli.py imports auth.py for DEFAULT_CREDENTIALS_DIR,
    # so importing cli.py at module level here would create a cycle.
    from .cli import resolve_admin_token, resolve_credentials_dir

    admin_token = resolve_admin_token(args)
    credentials_dir = resolve_credentials_dir(args)
    persisted = _read_token(credential_path(credentials_dir, worker_id))

    if admin_token is None and persisted is None:
        # Neither secret is available — auth-disabled posture.
        return None

    credential = bootstrap_worker_credential(
        base_url=args.task_store_url,
        experiment_id=args.experiment_id,
        worker_id=worker_id,
        credentials_dir=credentials_dir,
        admin_token=admin_token,
        labels=labels,
    )
    return credential.bearer


def credential_secret(bearer: str | None) -> str | None:
    """Extract the secret half of a §13.1 ``<principal>:<secret>`` bearer.

    Used by worker hosts that thread ``EDEN_WORKER_CREDENTIAL`` (the
    secret only — not the principal) into spawned children's
    environment so user code can rebuild the bearer with its own
    ``EDEN_WORKER_ID``. Returns ``None`` when ``bearer`` is ``None``
    or doesn't contain ``:``.
    """
    if bearer is None or ":" not in bearer:
        return None
    return bearer.split(":", 1)[1]
