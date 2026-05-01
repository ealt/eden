"""ReferenceAdapter wrapper that fronts the IUT with the misbehaving proxy.

Used by the self-validation meta scenario via --iut-adapter. This is
NOT a normal conformance adapter; it deliberately violates spec to
prove the harness has teeth.
"""

from __future__ import annotations

from pathlib import Path

from conformance.adapters.reference.adapter import ReferenceAdapter
from conformance.harness.adapter import IutHandle

from .misbehave_proxy import MisbehaveProxy, hit_counter_path_from_env


class MisbehavingAdapter(ReferenceAdapter):
    """Wraps the reference adapter with a proxy that mutates one wrong-token response."""

    def __init__(self) -> None:
        super().__init__()
        self._proxy: MisbehaveProxy | None = None

    def start(
        self,
        *,
        experiment_config_path: Path,
        experiment_id: str,
    ) -> IutHandle:
        real = super().start(
            experiment_config_path=experiment_config_path,
            experiment_id=experiment_id,
        )
        # Defensive: if proxy creation/start raises after the upstream
        # subprocess has been spawned (or after the proxy itself
        # partially started threads/sockets), tear down BOTH the proxy
        # and the upstream so we don't leak the eden-task-store-server
        # process or the proxy thread/listener.
        proxy: MisbehaveProxy | None = None
        try:
            proxy = MisbehaveProxy(
                upstream_base_url=real.base_url,
                hit_counter_path=hit_counter_path_from_env(),
            )
            proxy.start()
        except BaseException:
            if proxy is not None:
                try:
                    proxy.stop()
                except Exception:  # noqa: BLE001
                    pass
            super().stop()
            raise
        self._proxy = proxy
        return IutHandle(
            base_url=proxy.base_url,
            experiment_id=real.experiment_id,
            extra_headers=real.extra_headers,
        )

    def stop(self) -> None:
        if self._proxy is not None:
            try:
                self._proxy.stop()
            except Exception:  # noqa: BLE001
                pass
            self._proxy = None
        super().stop()
