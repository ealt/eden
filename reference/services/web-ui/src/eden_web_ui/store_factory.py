"""Per-experiment ``Store`` vending for the multi-experiment web-ui.

Issue #145 closes the 12c per-route store-swap deferral. 12c shipped
the experiment switcher (``Session.selected_experiment_id`` +
``POST /admin/experiments/{E}/select``) but every per-experiment route
still read the startup-bound ``app.state.store``. This module is the
load-bearing piece that lets a route operate against whichever
experiment the operator selected.

Two implementations share one interface (``for_experiment`` + ``close``
+ ``admin_enabled``):

- :class:`StoreFactory` — the production, wire-backed factory. Vends
  per-``(experiment_id, role)`` :class:`~eden_wire.StoreClient` views
  against one task-store-server URL (12c Decision 11: one URL
  deployment-wide; only the ``experiment_id`` path segment varies). A
  single shared ``httpx.Client`` is threaded through every vended
  client so connection-pooling is preserved. Worker-role views are
  JIT-credentialed via :class:`BearerCache` on first access; admin-role
  views reuse the one deployment admin token.
- :class:`StaticStoreFactory` — wraps a pre-built ``Store`` (and
  optional admin ``Store``) for a single experiment. Used by
  ``make_app``'s single-experiment construction path and by the test
  suite (the in-memory store is not a wire client, so the live factory
  cannot vend it).

The bearer plumbing (§3.2 of the plan) reuses
:func:`eden_service_common.auth.bootstrap_worker_credential` verbatim —
that function already implements the per-``worker_id`` exclusive lock,
the idempotent-register-then-reissue branch, and persisted-token
verification via ``/whoami``. This module never reimplements those
disciplines; it only caches the resulting bearer per experiment.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import httpx
from eden_service_common.auth import (
    _read_token as read_persisted_token,
)
from eden_service_common.auth import (
    bootstrap_worker_credential,
    credential_path,
)
from eden_storage import Store
from eden_storage.errors import NotFound
from eden_wire import StoreClient, Unauthorized

Role = Literal["worker", "admin"]


class StoreFactoryError(RuntimeError):
    """Base class for credential-bootstrap / store-vending failures."""


class MissingAdminToken(StoreFactoryError):
    """A worker credential must be bootstrapped but no admin token is available.

    Raised when ``for_experiment`` needs to JIT-register (or reissue) a
    per-experiment worker credential, there is no persisted credential
    on disk, and neither ``--admin-token`` nor ``$EDEN_ADMIN_TOKEN`` is
    set. The resolve helper routes this to a
    ``?error=cannot-bootstrap-credential`` dashboard redirect.
    """

    def __init__(self, experiment_id: str) -> None:
        self.experiment_id = experiment_id
        super().__init__(
            f"cannot bootstrap a worker credential for experiment "
            f"{experiment_id!r}: no persisted credential and no admin token"
        )


class AdminTokenRejected(StoreFactoryError):
    """The admin token was rejected while bootstrapping a worker credential."""

    def __init__(self, experiment_id: str) -> None:
        self.experiment_id = experiment_id
        super().__init__(
            f"admin token rejected while bootstrapping a credential for "
            f"experiment {experiment_id!r}"
        )


class TaskStoreUnreachable(StoreFactoryError):
    """A transport error reached the task-store-server during bootstrap."""

    def __init__(self, experiment_id: str, cause: BaseException) -> None:
        self.experiment_id = experiment_id
        self.cause = cause
        super().__init__(
            f"task-store-server unreachable while bootstrapping a credential "
            f"for experiment {experiment_id!r}: {cause.__class__.__name__}"
        )


class BearerCache:
    """Caches per-experiment worker bearers, JIT-bootstrapping on miss.

    Delegates to :func:`bootstrap_worker_credential` — the same helper
    every worker host uses at startup — passing a per-experiment
    ``credentials_dir`` (``<credential-dir>/<experiment_id>``) so each
    experiment's persisted ``<worker_id>.token`` lives in its own
    subtree. The credential's ``bearer`` string is cached in-process
    for the lifetime of the factory; the on-disk persistence handles
    cross-process reuse and restart survival.
    """

    def __init__(
        self,
        *,
        base_url: str,
        worker_id: str,
        credential_dir: Path,
        admin_token: str | None,
    ) -> None:
        self._base_url = base_url
        self._worker_id = worker_id
        self._credential_dir = credential_dir
        self._admin_token = admin_token
        self._cache: dict[str, str | None] = {}

    def bearer_for(self, experiment_id: str) -> str | None:
        """Return the worker bearer for ``experiment_id``, bootstrapping if needed.

        Returns ``None`` in the auth-disabled posture (no admin token AND
        no persisted credential) — mirroring
        ``resolve_worker_bearer``'s posture 3, where the task-store-server
        runs without ``--admin-token`` and the wire falls back to the
        worker-id header shim. Otherwise classifies the four documented
        bootstrap failure branches (plan §3.2) into the module's
        exception taxonomy so the resolve helper can route each to a
        distinct operator-facing banner.
        """
        if experiment_id in self._cache:
            return self._cache[experiment_id]
        cred_dir = self._credential_dir / experiment_id
        persisted = read_persisted_token(credential_path(cred_dir, self._worker_id))
        if self._admin_token is None and persisted is None:
            self._cache[experiment_id] = None
            return None
        try:
            credential = bootstrap_worker_credential(
                base_url=self._base_url,
                experiment_id=experiment_id,
                worker_id=self._worker_id,
                credentials_dir=cred_dir,
                admin_token=self._admin_token,
                labels={"role": "web-ui"},
            )
        except NotFound:
            # The experiment does not exist on the task-store-server.
            # The caller (resolve helper) distinguishes registered-but-
            # unseeded (control plane knows it) from truly-gone.
            raise
        except Unauthorized as exc:
            raise AdminTokenRejected(experiment_id) from exc
        except RuntimeError as exc:
            # bootstrap_worker_credential raises bare RuntimeError when
            # it needs the admin token (register / reissue) but none is
            # available. Narrow it to MissingAdminToken so the caller
            # does not swallow genuine programming errors.
            raise MissingAdminToken(experiment_id) from exc
        except httpx.TransportError as exc:
            raise TaskStoreUnreachable(experiment_id, exc) from exc
        self._cache[experiment_id] = credential.bearer
        return credential.bearer

    def evict(self, experiment_id: str) -> None:
        """Drop the cached bearer for ``experiment_id`` so it re-bootstraps."""
        self._cache.pop(experiment_id, None)

    def clear(self) -> None:
        """Drop all cached bearers (the on-disk credentials persist)."""
        self._cache.clear()


class StoreFactory:
    """Vends per-experiment ``StoreClient`` views against one task-store URL.

    Caches by ``(experiment_id, role)``. Connection-pools by sharing one
    ``httpx.Client`` across every vended client (passed via the
    ``client=`` kwarg ``StoreClient.__init__`` already accepts), so a
    vended client's own ``close()`` is a no-op — only ``close()`` on the
    factory tears down the shared transport.
    """

    def __init__(
        self,
        *,
        base_url: str,
        bearer_cache: BearerCache,
        admin_token: str | None,
        shared_client: httpx.Client,
    ) -> None:
        self._base_url = base_url
        self._bearer_cache = bearer_cache
        self._admin_token = admin_token
        self._shared_client = shared_client
        self._cache: dict[tuple[str, Role], StoreClient] = {}

    @property
    def admin_enabled(self) -> bool:
        """True when a deployment admin token is configured."""
        return self._admin_token is not None

    def for_experiment(
        self, experiment_id: str, *, role: Role = "worker"
    ) -> StoreClient | None:
        """Return a ``StoreClient`` view of ``experiment_id`` for ``role``.

        ``role="admin"`` returns ``None`` when no deployment admin token
        is configured (the admin-disabled posture, mirroring 12c's
        ``admin_store is None``). ``role="worker"`` always returns a
        client or raises one of the bootstrap exceptions.
        """
        if role == "admin" and self._admin_token is None:
            return None
        cached = self._cache.get((experiment_id, role))
        if cached is not None:
            return cached
        if role == "admin":
            bearer = f"admin:{self._admin_token}"
        else:
            bearer = self._bearer_cache.bearer_for(experiment_id)
        client = StoreClient(
            base_url=self._base_url,
            experiment_id=experiment_id,
            bearer=bearer,
            client=self._shared_client,
        )
        self._cache[(experiment_id, role)] = client
        return client

    def evict(self, experiment_id: str) -> None:
        """Drop cached views + bearer for ``experiment_id`` (stale-401 recovery).

        The next ``for_experiment`` re-bootstraps the worker credential.
        Vended clients ride on the shared transport, so dropping them from
        the cache without closing leaks nothing.
        """
        self._cache.pop((experiment_id, "worker"), None)
        self._cache.pop((experiment_id, "admin"), None)
        self._bearer_cache.evict(experiment_id)

    def close(self) -> None:
        """Close the shared ``httpx.Client`` and clear caches."""
        self._cache.clear()
        self._bearer_cache.clear()
        self._shared_client.close()


class StaticStoreFactory:
    """A factory that vends one pre-built ``Store`` for a single experiment.

    Backs ``make_app``'s single-experiment construction and the test
    suite. ``for_experiment`` ignores the ``experiment_id`` argument
    (the single-experiment / no-control-plane posture always resolves to
    the deployment default) and returns the worker store for
    ``role="worker"`` or the admin store for ``role="admin"``.
    """

    def __init__(
        self,
        *,
        experiment_id: str,
        store: Store,
        admin_store: Store | None = None,
    ) -> None:
        self._experiment_id = experiment_id
        self._store = store
        self._admin_store = admin_store

    @property
    def admin_enabled(self) -> bool:
        """True when an admin store is configured (admin controls render)."""
        return self._admin_store is not None

    def for_experiment(
        self, experiment_id: str, *, role: Role = "worker"  # noqa: ARG002
    ) -> Store | None:
        """Return the single store (worker) or admin store for any id."""
        if role == "admin":
            return self._admin_store
        return self._store

    def evict(self, experiment_id: str) -> None:  # noqa: ARG002
        """No-op: the static factory holds one fixed store, nothing to evict."""
        return None

    def close(self) -> None:
        """No-op: the static factory does not own the stores' lifecycles.

        The CLI / test harness that constructed the stores owns closing
        them.
        """
        return None
