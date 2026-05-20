"""Reference adapter for the EDEN control-plane server.

Spawns ``python -m eden_control_plane_server`` as a per-test
subprocess so v1+multi-experiment scenarios can drive the chapter
11 surface without depending on Compose.

Mirrors `ReferenceAdapter`'s port-announcement protocol, stderr
drain, and bounded-shutdown patterns so a control-plane subprocess
crash surfaces with the same diagnostics as a task-store-server
crash.
"""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass


@dataclass
class ControlPlaneHandle:
    """Connection details for a spawned control-plane subprocess."""

    base_url: str


class ControlPlaneSubprocess:
    """One control-plane-server subprocess.

    The conformance suite is single-tenant per test, but the
    control plane is conceptually deployment-wide; a fresh
    `InMemoryControlPlaneStore` per subprocess keeps tests isolated
    without spinning up Postgres.
    """

    _PORT_ANNOUNCE_TIMEOUT = 15.0
    _STOP_TIMEOUT = 5.0

    def __init__(self) -> None:
        self._proc: subprocess.Popen[str] | None = None
        self._stderr_lines: list[str] = []
        self._stderr_thread: threading.Thread | None = None

    def start(self) -> ControlPlaneHandle:
        """Spawn the control-plane subprocess; return its base URL."""
        self._proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "eden_control_plane_server",
                "--store-url",
                ":memory:",
                "--port",
                "0",
                "--host",
                "127.0.0.1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            port = self._read_port_announcement()
            self._start_stderr_drain()
        except BaseException:
            self.stop()
            raise
        return ControlPlaneHandle(base_url=f"http://127.0.0.1:{port}")

    def stop(self) -> None:
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=self._STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2.0)
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=1.0)
        self._proc = None

    @property
    def stderr_log(self) -> str:
        return "".join(self._stderr_lines)

    def _read_port_announcement(self) -> int:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        q: queue.Queue[str] = queue.Queue()

        def _reader() -> None:
            assert proc.stdout is not None
            try:
                for line in iter(proc.stdout.readline, ""):
                    q.put(line)
            finally:
                q.put("")

        reader = threading.Thread(target=_reader, daemon=True)
        reader.start()
        deadline = time.monotonic() + self._PORT_ANNOUNCE_TIMEOUT
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    "control-plane-server did not announce port within "
                    f"{self._PORT_ANNOUNCE_TIMEOUT}s; stderr="
                    f"{self.stderr_log!r}"
                )
            try:
                line = q.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                if proc.poll() is not None:
                    raise RuntimeError(
                        f"control-plane-server exited early with code "
                        f"{proc.returncode}; stderr={self._read_remaining_stderr()!r}"
                    ) from None
                continue
            if line == "":
                raise RuntimeError(
                    f"control-plane-server stdout closed before announcing "
                    f"port; rc={proc.poll()!r}; stderr={self._read_remaining_stderr()!r}"
                )
            if line.startswith("EDEN_CONTROL_PLANE_LISTENING"):
                parts = dict(p.split("=", 1) for p in line.strip().split()[1:])
                return int(parts["port"])

    def _read_remaining_stderr(self) -> str:
        proc = self._proc
        assert proc is not None
        leftover = ""
        if proc.stderr is not None:
            try:
                leftover = proc.stderr.read() or ""
            except (OSError, ValueError):
                pass
        return self.stderr_log + leftover

    def _start_stderr_drain(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stderr is not None

        def _drain() -> None:
            assert proc.stderr is not None
            for line in proc.stderr:
                self._stderr_lines.append(line)

        thread = threading.Thread(target=_drain, daemon=True)
        thread.start()
        self._stderr_thread = thread
