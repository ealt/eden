"""Unit tests for the per-experiment store factory (issue #145 §3.3)."""

from __future__ import annotations

import httpx
import pytest
from eden_storage import InMemoryStore
from eden_web_ui.store_factory import (
    BearerCache,
    StaticStoreFactory,
    StoreFactory,
)


def _store() -> InMemoryStore:
    return InMemoryStore(experiment_id="exp-1", evaluation_schema={"type": "object"})


# ---------------------------------------------------------------------------
# StaticStoreFactory
# ---------------------------------------------------------------------------


def test_static_factory_returns_worker_store_for_any_id() -> None:
    store = _store()
    factory = StaticStoreFactory(experiment_id="exp-1", store=store)
    assert factory.for_experiment("exp-1") is store
    # Single-experiment posture: the id argument is ignored.
    assert factory.for_experiment("exp-other") is store


def test_static_factory_admin_role() -> None:
    store, admin = _store(), _store()
    factory = StaticStoreFactory(experiment_id="exp-1", store=store, admin_store=admin)
    assert factory.admin_enabled is True
    assert factory.for_experiment("exp-1", role="admin") is admin


def test_static_factory_admin_disabled_returns_none() -> None:
    factory = StaticStoreFactory(experiment_id="exp-1", store=_store())
    assert factory.admin_enabled is False
    assert factory.for_experiment("exp-1", role="admin") is None


def test_static_factory_close_is_noop() -> None:
    factory = StaticStoreFactory(experiment_id="exp-1", store=_store())
    factory.close()  # does not raise; does not own the store lifecycle


# ---------------------------------------------------------------------------
# Live StoreFactory (construction + caching; no network needed)
# ---------------------------------------------------------------------------


class _FakeBearerCache:
    """A BearerCache stand-in that records calls and returns a fixed bearer."""

    def __init__(self, bearer: str | None) -> None:
        self._bearer = bearer
        self.calls: list[str] = []

    def bearer_for(self, experiment_id: str) -> str | None:
        self.calls.append(experiment_id)
        return self._bearer

    def clear(self) -> None:
        self.calls.clear()


def _live_factory(
    *, bearer_cache: object, admin_token: str | None = "secret"
) -> tuple[StoreFactory, httpx.Client]:
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200)))
    factory = StoreFactory(
        base_url="http://task-store",
        bearer_cache=bearer_cache,  # type: ignore[arg-type]
        admin_token=admin_token,
        shared_client=client,
    )
    return factory, client


def test_live_factory_caches_worker_view() -> None:
    cache = _FakeBearerCache("w-1:tok")
    factory, _ = _live_factory(bearer_cache=cache)
    first = factory.for_experiment("exp-A", role="worker")
    second = factory.for_experiment("exp-A", role="worker")
    assert first is second
    # bearer_for is consulted only on the cache miss.
    assert cache.calls == ["exp-A"]


def test_live_factory_worker_and_admin_are_distinct_instances() -> None:
    factory, _ = _live_factory(bearer_cache=_FakeBearerCache("w-1:tok"))
    worker = factory.for_experiment("exp-A", role="worker")
    admin = factory.for_experiment("exp-A", role="admin")
    assert worker is not admin
    assert admin is not None


def test_live_factory_admin_none_without_token() -> None:
    factory, _ = _live_factory(bearer_cache=_FakeBearerCache(None), admin_token=None)
    assert factory.admin_enabled is False
    assert factory.for_experiment("exp-A", role="admin") is None


def test_live_factory_shares_one_httpx_client() -> None:
    factory, client = _live_factory(bearer_cache=_FakeBearerCache("w-1:tok"))
    view_a = factory.for_experiment("exp-A", role="worker")
    view_b = factory.for_experiment("exp-B", role="worker")
    # Vended clients ride on the shared transport and do not own it.
    assert view_a is not None
    assert view_b is not None
    assert view_a._client is client
    assert view_b._client is client


def test_live_factory_close_closes_shared_client() -> None:
    factory, client = _live_factory(bearer_cache=_FakeBearerCache("w-1:tok"))
    factory.close()
    assert client.is_closed


# ---------------------------------------------------------------------------
# BearerCache auth-disabled posture (no network)
# ---------------------------------------------------------------------------


def test_bearer_cache_auth_disabled_returns_none(tmp_path: object) -> None:
    cache = BearerCache(
        base_url="http://task-store",
        worker_id="w-1",
        credential_dir=tmp_path,  # type: ignore[arg-type]
        admin_token=None,
    )
    # No admin token AND no persisted credential → None (header-shim posture).
    assert cache.bearer_for("exp-A") is None
    # Cached: a second call does not re-probe disk.
    assert cache.bearer_for("exp-A") is None


@pytest.mark.parametrize("role", ["worker", "admin"])
def test_live_factory_role_param_accepted(role: str) -> None:
    factory, _ = _live_factory(bearer_cache=_FakeBearerCache("w-1:tok"))
    factory.for_experiment("exp-A", role=role)  # type: ignore[arg-type]
