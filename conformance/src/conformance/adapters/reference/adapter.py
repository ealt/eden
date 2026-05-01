"""Reference IUT adapter — spawns the EDEN reference task-store-server.

The adapter is per-scenario: each test gets a fresh subprocess. Bonds
to in-memory SQLite storage for speed; conformance does not test
durability (eden-storage's own tests do).
"""

from __future__ import annotations

import queue
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

from conformance.harness.adapter import IutAdapter, IutHandle


class ReferenceAdapter(IutAdapter):
    """Spawns ``python -m eden_task_store_server`` in a subprocess."""

    _PORT_ANNOUNCE_TIMEOUT = 15.0
    _STOP_TIMEOUT = 5.0
    _SUBSCRIBE_TIMEOUT_S = 2.0  # see plan §D rationale

    def __init__(self) -> None:
        self._proc: subprocess.Popen[str] | None = None
        self._stderr_lines: list[str] = []
        self._stderr_thread: threading.Thread | None = None

    def start(
        self,
        *,
        experiment_config_path: Path,
        experiment_id: str,
    ) -> IutHandle:
        # Use token_hex (only [0-9a-f]) — token_urlsafe can start with
        # `-`, which argparse interprets as a new flag and the server
        # exits with code 2 before announcing its port.
        token = secrets.token_hex(24)
        self._proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "eden_task_store_server",
                "--store-url",
                ":memory:",
                "--experiment-id",
                experiment_id,
                "--experiment-config",
                str(experiment_config_path),
                "--shared-token",
                token,
                "--subscribe-timeout",
                str(self._SUBSCRIBE_TIMEOUT_S),
                "--port",
                "0",
                "--host",
                "127.0.0.1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        # Defensive: if the port-announce read or stderr-drain start
        # raises, the spawned subprocess and its drain thread would leak
        # because the fixture never reached `yield` — `stop()` would not
        # be called. Catch, clean up, then re-raise.
        try:
            port = self._read_port_announcement()
            self._start_stderr_drain()
        except BaseException:
            self.stop()
            raise
        return IutHandle(
            base_url=f"http://127.0.0.1:{port}",
            experiment_id=experiment_id,
            extra_headers={"Authorization": f"Bearer {token}"},
        )

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

    # ----- internals -----

    def _read_port_announcement(self) -> int:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        # readline() blocks indefinitely until a newline OR EOF, so a
        # hung server that never prints anything would wedge here for
        # the lifetime of the test process. Drain stdout from a
        # daemon thread into a queue so the timeout is real.
        q: queue.Queue[str] = queue.Queue()

        def _reader() -> None:
            assert proc.stdout is not None
            try:
                for line in iter(proc.stdout.readline, ""):
                    q.put(line)
            finally:
                q.put("")  # EOF sentinel

        reader = threading.Thread(target=_reader, daemon=True)
        reader.start()
        deadline = time.monotonic() + self._PORT_ANNOUNCE_TIMEOUT
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    "task-store-server did not announce port within "
                    f"{self._PORT_ANNOUNCE_TIMEOUT}s; stderr="
                    f"{self.stderr_log!r}"
                )
            try:
                line = q.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                # Reader still alive, no line yet — loop and re-check.
                if proc.poll() is not None:
                    raise RuntimeError(
                        f"task-store-server exited early with code "
                        f"{proc.returncode}; stderr="
                        f"{self._read_remaining_stderr()!r}"
                    ) from None
                continue
            if line == "":
                # EOF: the subprocess closed stdout, so it likely exited.
                raise RuntimeError(
                    f"task-store-server stdout closed before announcing "
                    f"port; rc={proc.poll()!r}; stderr="
                    f"{self._read_remaining_stderr()!r}"
                )
            if line.startswith("EDEN_TASK_STORE_LISTENING"):
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
