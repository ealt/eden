"""Subprocess execution helpers for Phase 10d worker-host modes.

The planner host runs a long-running subprocess and exchanges JSON
lines with it; the implementer / evaluator hosts spawn a short-lived
subprocess per task. This module owns the bits both modes need:

- env-file parsing,
- ``Popen`` setup with a fresh process group so signals can target
  the worker tree without affecting the host,
- a stderr reader thread that forwards each line to a host logger,
- a stdout queue with a deadline-aware ``read_line``,
- a SIGTERM → wait → SIGKILL termination ladder.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import queue
import signal
import subprocess
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

log = logging.getLogger(__name__)


def parse_env_file(path: Path | str) -> dict[str, str]:
    """Parse a ``KEY=VALUE`` env file (one entry per line).

    Lines that are empty or start with ``#`` after stripping are
    skipped. Values are taken verbatim — no shell interpolation, no
    escaping, no quoting rules. Returns the parsed mapping. Missing
    file → empty mapping (callers may pass an absent path
    unconditionally).
    """
    p = Path(path)
    if not p.is_file():
        return {}
    out: dict[str, str] = {}
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            log.warning(
                "env_file_skipping_line_without_eq",
                extra={"path": str(p), "line": raw},
            )
            continue
        out[key.strip()] = value
    return out


@dataclass
class Subprocess:
    """A managed subprocess with stdout queue + stderr forwarding.

    Construct via :func:`spawn` rather than instantiating directly.
    """

    popen: subprocess.Popen[str]
    role: str
    """Free-form role name used in log entries (``planner`` / etc.)."""
    stdout_queue: queue.Queue[str | None]
    """Lines from stdout. Sentinel ``None`` is pushed at EOF."""
    _stderr_thread: threading.Thread
    _task_id_holder: list[str | None]
    """Mutable single-cell holder for the current task_id (long-running
    planner subprocess updates this per dispatch; per-task hosts can
    leave it as the value passed at spawn-time). Read by the stderr
    forwarder so each forwarded line is tagged with whichever task is
    in flight.

    **Best-effort attribution.** stdout and stderr are independent
    pipes. If task A's stderr drains late (after the host has already
    called ``set_current_task(B)``), those lines will be stamped with
    task B's id. Conversely, idle post-task stderr keeps the last
    task's id until the next dispatch. Per-task hosts (implementer /
    evaluator) don't have this issue because their subprocess is
    short-lived and exits before the next task's stderr begins.
    The long-running planner host accepts this as a limitation; users
    who need exact per-task attribution should frame it themselves on
    the structured stdout protocol.
    """

    def set_current_task(self, task_id: str | None) -> None:
        """Update the task_id stamped onto stderr forwarding.

        Best-effort across stderr/stdout boundaries — see the
        ``_task_id_holder`` docstring for the caveat.
        """
        self._task_id_holder[0] = task_id

    def read_line(self, *, deadline: float) -> str | None:
        """Pop one stdout line; return ``None`` on EOF.

        Blocks up to ``deadline`` (absolute monotonic clock). Raises
        :class:`TimeoutError` if the deadline elapses with no line
        available.
        """
        timeout = max(0.0, deadline - time.monotonic())
        try:
            value = self.stdout_queue.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError(
                f"subprocess {self.role!r}: no stdout line within deadline"
            ) from exc
        return value

    def write_line(self, line: str) -> None:
        """Write a single line + newline to the subprocess's stdin."""
        if self.popen.stdin is None:
            raise RuntimeError(f"subprocess {self.role!r} has no stdin")
        self.popen.stdin.write(line + "\n")
        self.popen.stdin.flush()

    def is_alive(self) -> bool:
        """Return ``True`` if the subprocess hasn't exited."""
        return self.popen.poll() is None

    def terminate(self, *, shutdown_deadline: float) -> int:
        """Stop the subprocess: SIGTERM, wait, SIGKILL fallback.

        Sends SIGTERM to the entire process group (``start_new_session=True``
        guarantees there is one), waits up to ``shutdown_deadline``
        seconds for exit, then SIGKILLs the group. Returns the exit
        code.
        """
        if self.popen.poll() is not None:
            return self.popen.returncode
        try:
            os.killpg(self.popen.pid, signal.SIGTERM)
        except ProcessLookupError:
            return self.popen.wait(timeout=1)
        try:
            return self.popen.wait(timeout=shutdown_deadline)
        except subprocess.TimeoutExpired:
            log.warning(
                "subprocess_sigkill",
                extra={"role": self.role, "pid": self.popen.pid},
            )
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.popen.pid, signal.SIGKILL)
            return self.popen.wait(timeout=5)


def spawn(
    *,
    command: str,
    cwd: Path | str,
    env: Mapping[str, str],
    role: str,
    task_id: str | None = None,
    capture_stdout: bool = True,
    capture_stderr: bool = True,
    capture_stdin: bool = True,
) -> Subprocess:
    """Launch ``command`` via ``shell=True`` with a fresh process group.

    The command is interpreted by the user's shell so expressions
    like ``python3 ${EDEN_EXPERIMENT_DIR}/plan.py`` expand against
    the supplied env. Stdout lines flow into a queue; stderr lines
    are forwarded to the host logger at info, tagged with ``role``
    and (if provided) ``task_id`` for downstream filtering.
    """
    full_env = dict(os.environ)
    full_env.update(env)
    popen = subprocess.Popen(
        command,
        shell=True,
        cwd=str(cwd),
        env=full_env,
        stdin=subprocess.PIPE if capture_stdin else None,
        stdout=subprocess.PIPE if capture_stdout else None,
        stderr=subprocess.PIPE if capture_stderr else None,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    stdout_queue: queue.Queue[str | None] = queue.Queue()
    if capture_stdout and popen.stdout is not None:
        stdout_thread = threading.Thread(
            target=_pump_lines_to_queue,
            args=(popen.stdout, stdout_queue),
            daemon=True,
            name=f"subprocess-{role}-stdout",
        )
        stdout_thread.start()
    task_id_holder: list[str | None] = [task_id]
    if capture_stderr and popen.stderr is not None:
        stderr_thread = threading.Thread(
            target=_pump_lines_to_log,
            args=(popen.stderr, role, task_id_holder),
            daemon=True,
            name=f"subprocess-{role}-stderr",
        )
    else:
        stderr_thread = threading.Thread(target=lambda: None, daemon=True)
    stderr_thread.start()
    return Subprocess(
        popen=popen,
        role=role,
        stdout_queue=stdout_queue,
        _stderr_thread=stderr_thread,
        _task_id_holder=task_id_holder,
    )


def _pump_lines_to_queue(stream: IO[str], dest: queue.Queue[str | None]) -> None:
    try:
        for line in iter(stream.readline, ""):
            dest.put(line.rstrip("\n"))
    finally:
        dest.put(None)


def _pump_lines_to_log(
    stream: IO[str], role: str, task_id_holder: list[str | None]
) -> None:
    for line in iter(stream.readline, ""):
        text = line.rstrip("\n")
        if text:
            extra: dict[str, object] = {"role": role, "line": text}
            current = task_id_holder[0]
            if current is not None:
                extra["task_id"] = current
            log.info("subprocess_stderr", extra=extra)


def parse_json_line(line: str) -> dict[str, Any] | None:
    """Parse a single JSON-line message; ``None`` on parse failure."""
    if not line.strip():
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    return obj
