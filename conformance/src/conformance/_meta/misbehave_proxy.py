"""Tiny aiohttp proxy that mutates one specific reference response.

Used by the self-validation meta scenario. Forwards every request
unchanged EXCEPT a wrong-token submit (one that the real IUT would
return 403 `eden://error/wrong-token` for): in that case the proxy
returns 200 OK with a fabricated success body, and increments a
mutation counter.

The mutation counter is exposed via a sidecar file so the meta test
can read it after the subprocess pytest run exits — the proxy itself
runs inside the pytest subprocess.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import threading
from pathlib import Path

from aiohttp import ClientSession, web

_SUBMIT_PATH_RE = re.compile(r"^/v0/experiments/[^/]+/tasks/[^/]+/submit$")


class MisbehaveProxy:
    """Forwards to upstream, mutates one specific 403 → 200."""

    def __init__(self, *, upstream_base_url: str, hit_counter_path: Path) -> None:
        self.upstream_base_url = upstream_base_url.rstrip("/")
        self.hit_counter_path = hit_counter_path
        self.mutation_count = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._port: int | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    @property
    def base_url(self) -> str:
        if self._port is None:
            raise RuntimeError("proxy has not started")
        return f"http://127.0.0.1:{self._port}"

    def start(self) -> None:
        # Pick an ephemeral port on the host, then hand it to aiohttp.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            self._port = s.getsockname()[1]
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=10.0):
            raise RuntimeError("proxy did not start within 10s")

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._serve())
        loop.run_forever()

    async def _serve(self) -> None:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        runner = web.AppRunner(app)
        await runner.setup()
        self._runner = runner
        site = web.TCPSite(runner, "127.0.0.1", self._port)
        await site.start()
        self._site = site
        self._ready.set()

    async def _handle(self, request: web.Request) -> web.Response:
        url = self.upstream_base_url + request.path_qs
        body = await request.read()
        # Strip hop-by-hop headers httpx auto-fills (host, content-length).
        out_headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in ("host", "content-length")
        }
        async with ClientSession() as session, session.request(
            request.method, url, data=body, headers=out_headers, allow_redirects=False
        ) as upstream_resp:
            upstream_body = await upstream_resp.read()
            upstream_status = upstream_resp.status
            upstream_headers = dict(upstream_resp.headers)

        # Mutation check: did the real IUT just return 403 wrong-token
        # for a submit request? If so, lie.
        if (
            request.method == "POST"
            and _SUBMIT_PATH_RE.match(request.path)
            and upstream_status == 403
        ):
            try:
                payload = json.loads(upstream_body)
            except (ValueError, UnicodeDecodeError):
                payload = {}
            if payload.get("type") == "eden://error/wrong-token":
                self.mutation_count += 1
                self._write_hit_counter()
                return web.Response(status=200, body=b"{}", content_type="application/json")
        # Strip transfer-encoding; aiohttp re-emits.
        for h in ("transfer-encoding", "content-encoding", "content-length"):
            upstream_headers.pop(h, None)
            upstream_headers.pop(h.title(), None)
        return web.Response(
            status=upstream_status, body=upstream_body, headers=upstream_headers
        )

    def _write_hit_counter(self) -> None:
        try:
            self.hit_counter_path.write_text(str(self.mutation_count))
        except OSError:
            pass

    def stop(self) -> None:
        if self._loop and self._site and self._runner:
            future = asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)
            try:
                future.result(timeout=5.0)
            except Exception:  # noqa: BLE001
                pass
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    async def _shutdown(self) -> None:
        assert self._site is not None and self._runner is not None
        await self._site.stop()
        await self._runner.cleanup()


_HIT_COUNTER_ENV = "EDEN_CONFORMANCE_MISBEHAVE_HIT_COUNTER"


def hit_counter_path_from_env() -> Path:
    raw = os.environ.get(_HIT_COUNTER_ENV)
    if not raw:
        raise RuntimeError(f"{_HIT_COUNTER_ENV} not set")
    return Path(raw)


def env_var_name() -> str:
    return _HIT_COUNTER_ENV
