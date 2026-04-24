"""SIGTERM/SIGINT handler that flips a ``stopping`` flag.

Services poll ``StopFlag.is_set()`` between iterations. When the flag
is set, the loop exits cleanly. This avoids signal handlers that
interrupt in-flight work mid-request.
"""

from __future__ import annotations

import contextlib
import signal
import threading
from types import FrameType


class StopFlag:
    """Thread-safe stop signal for service loops."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def set(self) -> None:
        """Flip the flag on; subsequent ``is_set`` calls return ``True``."""
        self._event.set()

    def is_set(self) -> bool:
        """Return ``True`` once any caller has invoked :meth:`set`."""
        return self._event.is_set()

    def wait(self, timeout: float) -> bool:
        """Block up to ``timeout`` seconds or until set. Returns True if set."""
        return self._event.wait(timeout)


def install_stop_handlers(flag: StopFlag) -> None:
    """Install SIGTERM + SIGINT handlers that set ``flag``.

    No-op on platforms that lack one of the signals (e.g. Windows).
    """

    def _handler(signum: int, _frame: FrameType | None) -> None:  # noqa: ARG001
        flag.set()

    for sig_name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        # Not in main thread, or platform doesn't support it — ignore.
        with contextlib.suppress(ValueError, OSError):
            signal.signal(sig, _handler)
