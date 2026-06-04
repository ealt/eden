"""Automatic-checkpoint scheduler (issue #131).

Owns the cadence timer, the retention ring, the terminal-once flag, and
the atomic export write for the orchestrator's auto-checkpointing
feature. It is deliberately *pure enough to unit-test without the loop*:
all wall I/O goes through an injected ``export_fn(stream) -> None``
callable (which closes over an admin-authed export client — the
checkpoint export endpoint is admin-gated per ``07-wire-protocol.md``
§14, so a worker-bearer export would 403), and timing goes through an
injectable monotonic ``now_fn``.

Design (plan §3.2):

- **Periodic.** ``maybe_checkpoint_periodic`` fires when the monotonic
  clock reaches ``next_at`` (initialized one full interval after
  construction — startup state is just the seed, low value, and this
  avoids a t=0 churn checkpoint). The timer is measured **from the
  completion of the last attempt**, not a fixed slot, so a slow export
  or a delayed loop tick can never leave ``next_at`` in the past and
  produce back-to-back catch-up checkpoints.
- **Terminal.** ``maybe_checkpoint_terminal`` fires at most once per
  process when ``on_terminate`` is set; it is additionally restart-
  deduped by checking the destination for an existing
  ``<safe_exp>-terminated-*.tar`` (an in-memory flag alone would re-fire
  on a restart against an already-``terminated`` experiment).
- **Best-effort (plan D5).** Every export/prune is wrapped: on any
  exception the scheduler logs a structured warning, cleans up the
  partial temp file, and — for the periodic path — **still advances the
  timer by one interval** so a persistently-failing export retries at
  the next cadence boundary, not every poll tick (no retry storm).
- **Surgical pruning (plan §8 risk 3).** ``_prune`` only ever manages
  files matching the *periodic* pattern for *this* experiment id; it
  never touches ``-terminated-`` archives or operator-dropped files. A
  too-broad glob is a data-loss vector.

Disabled config (``enabled=false`` or absent block) yields a scheduler
whose methods are no-ops, so the loop always holds exactly one and never
branches on ``if scheduler is not None``.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

from eden_contracts import AutoCheckpointConfig
from eden_service_common import get_logger

log = get_logger(__name__)

# Reference defaults for the auto_checkpoint block. Applied here (not in
# the JSON Schema or the Pydantic model) so an absent config field
# round-trips as absent for schema parity — same posture as the
# orchestrator's other implementation-defined knobs.
DEFAULT_ENABLED = False
DEFAULT_INTERVAL_SECONDS = 3600.0
DEFAULT_RETENTION_COUNT = 6
DEFAULT_ON_TERMINATE = True

# Filename timestamp: UTC, second-granularity, sortable lexically.
_WALL_TS_FORMAT = "%Y%m%dT%H%M%SZ"
_WALL_TS_RE = r"\d{8}T\d{6}Z"

# Type of the admin-authed export callable. It writes the archive bytes
# into the supplied binary stream; its return value (if any) is ignored.
ExportFn = Callable[[IO[bytes]], object]


def _safe_experiment_prefix(experiment_id: str) -> str:
    """Filesystem-safe, collision-resistant filename prefix for an id.

    ``experiment_id`` is an arbitrary ≤64-char string per
    ``02-data-model.md`` §1.3 — not guaranteed filesystem-safe. A bare
    sanitizer would alias distinct ids (``a/b`` and ``a_b`` both →
    ``a_b``); since pruning matches on the filename prefix, that could
    delete a *different* experiment's archives in a shared destination.
    Appending a hash of the **raw** id makes the prefix practically
    collision-resistant (8 hex = 32 bits) without relying on the
    reference deployment's per-experiment destination isolation.
    """
    sanitized = re.sub(r"[^A-Za-z0-9._-]", "_", experiment_id)
    digest = hashlib.sha256(experiment_id.encode("utf-8")).hexdigest()[:8]
    return f"{sanitized}-{digest}"


class CheckpointScheduler:
    """Cadence + terminal auto-checkpoint orchestration (plan §3.2)."""

    def __init__(
        self,
        *,
        experiment_id: str,
        destination: Path | None,
        export_fn: ExportFn | None,
        enabled: bool,
        interval_seconds: float,
        retention_count: int,
        on_terminate: bool,
        now_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._experiment_id = experiment_id
        self._safe_prefix = _safe_experiment_prefix(experiment_id)
        self._destination = destination
        self._export_fn = export_fn
        self._enabled = enabled
        self._interval_seconds = interval_seconds
        self._retention_count = retention_count
        self._on_terminate = on_terminate
        self._now_fn = now_fn
        self._terminal_done = False
        # First periodic checkpoint is one full interval after
        # construction (plan §3.2): startup state is just the seed.
        self._next_at = now_fn() + interval_seconds
        # Precompiled matchers scoped to THIS experiment's prefix.
        esc = re.escape(self._safe_prefix)
        self._periodic_re = re.compile(rf"^{esc}-(?P<ts>{_WALL_TS_RE})\.tar$")
        self._terminal_re = re.compile(rf"^{esc}-terminated-{_WALL_TS_RE}\.tar$")

    @classmethod
    def from_config(
        cls,
        config: AutoCheckpointConfig | None,
        *,
        experiment_id: str,
        destination: Path | None,
        export_fn: ExportFn | None,
        now_fn: Callable[[], float] = time.monotonic,
    ) -> CheckpointScheduler:
        """Build a scheduler from an ``auto_checkpoint`` config block.

        Applies the reference defaults for any absent field. An absent
        block (``config is None``) or ``enabled=false`` yields a no-op
        scheduler (both ``maybe_*`` methods return immediately).
        """
        if config is None:
            enabled = DEFAULT_ENABLED
            interval_seconds = DEFAULT_INTERVAL_SECONDS
            retention_count = DEFAULT_RETENTION_COUNT
            on_terminate = DEFAULT_ON_TERMINATE
        else:
            enabled = (
                config.enabled if config.enabled is not None else DEFAULT_ENABLED
            )
            interval_seconds = (
                config.interval_seconds
                if config.interval_seconds is not None
                else DEFAULT_INTERVAL_SECONDS
            )
            retention_count = (
                config.retention_count
                if config.retention_count is not None
                else DEFAULT_RETENTION_COUNT
            )
            on_terminate = (
                config.on_terminate
                if config.on_terminate is not None
                else DEFAULT_ON_TERMINATE
            )
        return cls(
            experiment_id=experiment_id,
            destination=destination,
            export_fn=export_fn,
            enabled=enabled,
            interval_seconds=interval_seconds,
            retention_count=retention_count,
            on_terminate=on_terminate,
            now_fn=now_fn,
        )

    @property
    def enabled(self) -> bool:
        """Whether auto-checkpointing is active (false ⇒ both methods no-op)."""
        return self._enabled

    @property
    def interval_seconds(self) -> float:
        """Resolved cadence interval (seconds)."""
        return self._interval_seconds

    @property
    def retention_count(self) -> int:
        """Resolved periodic-checkpoint ring depth."""
        return self._retention_count

    @property
    def on_terminate(self) -> bool:
        """Whether a terminal checkpoint is taken on observed termination."""
        return self._on_terminate

    @property
    def should_check_terminal(self) -> bool:
        """Whether the loop still needs to read state for the terminal trigger.

        Lets the loop skip the per-iteration ``read_experiment_state``
        wire roundtrip entirely when auto-checkpointing is disabled,
        ``on_terminate`` is off, or a terminal checkpoint has already
        fired this process.
        """
        return self._enabled and self._on_terminate and not self._terminal_done

    def maybe_checkpoint_periodic(self, *, wall_now: datetime) -> None:
        """Export a periodic checkpoint if the cadence interval has elapsed.

        ``wall_now`` supplies the human-readable UTC timestamp embedded
        in the filename; the *timing* decision uses the injected
        monotonic ``now_fn`` so a wall-clock step never reorders the
        cadence. Best-effort (plan D5): on any failure the timer still
        advances one interval (no retry storm).
        """
        if not self._enabled:
            return
        if self._now_fn() < self._next_at:
            return
        wall_ts = wall_now.strftime(_WALL_TS_FORMAT)
        filename = f"{self._safe_prefix}-{wall_ts}.tar"
        try:
            self._export_to(filename)
            self._prune()
        except Exception:  # noqa: BLE001 — best-effort safety net (plan D5)
            log.exception(
                "auto_checkpoint_periodic_failed",
                experiment_id=self._experiment_id,
                filename=filename,
            )
        finally:
            # Advance from completion of THIS attempt (success or
            # failure) — never leaves next_at in the past, never storms.
            self._next_at = self._now_fn() + self._interval_seconds

    def maybe_checkpoint_terminal(self) -> None:
        """Export a one-shot terminal checkpoint (plan D6).

        Fires only when ``on_terminate`` is set, no terminal checkpoint
        has fired this process, and no ``<safe_exp>-terminated-*.tar``
        already exists in the destination (restart dedup). The caller is
        responsible for gating this on an *observed* ``terminated``
        state. Best-effort: a failure is logged and the done-flag is NOT
        set, so a subsequent observed-terminated iteration retries.
        """
        if not self._enabled or not self._on_terminate or self._terminal_done:
            return
        try:
            if self._terminal_exists():
                # A prior process already wrote one — adopt it as done
                # so we don't re-scan the dir every iteration.
                self._terminal_done = True
                return
            wall_ts = datetime.now(UTC).strftime(_WALL_TS_FORMAT)
            filename = f"{self._safe_prefix}-terminated-{wall_ts}.tar"
            self._export_to(filename)
            self._terminal_done = True
            log.info(
                "auto_checkpoint_terminal_written",
                experiment_id=self._experiment_id,
                filename=filename,
            )
        except Exception:  # noqa: BLE001 — best-effort safety net (plan D5)
            log.exception(
                "auto_checkpoint_terminal_failed",
                experiment_id=self._experiment_id,
            )

    # -- internals -----------------------------------------------------

    def _export_to(self, filename: str) -> None:
        """Atomically write an export to ``<dest>/<filename>``.

        The export streams into a temp file **created inside the
        destination dir** so the final ``os.replace`` is a same-
        filesystem atomic rename — a partially-written ``.tar`` is never
        visible to the operator and the rename can't cross a device
        boundary (mirrors the credentials-file atomic-write discipline
        in ``checkpoints.py``). The temp file is unlinked on failure.
        """
        if self._destination is None or self._export_fn is None:
            raise RuntimeError(
                "auto_checkpoint export attempted with no destination/export_fn"
            )
        dest = self._destination
        final_path = dest / filename
        fd, tmp_name = tempfile.mkstemp(dir=dest, prefix=".tmp-", suffix=".tar")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as stream:
                self._export_fn(stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp_path, final_path)
        except BaseException:
            with contextlib.suppress(OSError):
                tmp_path.unlink()
            raise

    def _prune(self) -> None:
        """Unlink periodic archives beyond ``retention_count`` (oldest first).

        Surgical: matches ONLY this experiment's periodic pattern; never
        terminal archives, never foreign files. The embedded timestamp
        sorts lexically in chronological order.
        """
        if self._destination is None:
            return
        matches: list[tuple[str, Path]] = []
        for entry in self._destination.iterdir():
            if not entry.is_file():
                continue
            m = self._periodic_re.match(entry.name)
            if m is not None:
                matches.append((m.group("ts"), entry))
        if len(matches) <= self._retention_count:
            return
        matches.sort(key=lambda pair: pair[0])
        for _ts, path in matches[: len(matches) - self._retention_count]:
            with contextlib.suppress(OSError):
                path.unlink()
            log.info(
                "auto_checkpoint_pruned",
                experiment_id=self._experiment_id,
                filename=path.name,
            )

    def _terminal_exists(self) -> bool:
        """Whether a terminal archive for this experiment already exists."""
        if self._destination is None or not self._destination.is_dir():
            return False
        for entry in self._destination.iterdir():
            if entry.is_file() and self._terminal_re.match(entry.name):
                return True
        return False
