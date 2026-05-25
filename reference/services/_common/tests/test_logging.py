"""Tests for the shared JSON-line logger.

Issue #109 wired ``EDEN_LOG_DIR`` into ``configure_logging`` so each
service also writes its logs to a per-service ``.jsonl`` file. These
tests pin the contract that:

* with ``EDEN_LOG_DIR`` unset, stdout-only behavior is preserved
  (no regression);
* with ``EDEN_LOG_DIR`` set, a ``<service>.jsonl`` file is created,
  receives JSON-line records identical to stdout, appends across
  re-init, and rotates on byte threshold;
* invalid env-var values fall back to the defaults rather than
  crashing logger setup (a service that can't initialize logging
  can't surface the misconfiguration).
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Iterator
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import cast

import pytest
from eden_service_common.logging import (
    _DEFAULT_LOG_BACKUP_COUNT,
    _DEFAULT_LOG_MAX_BYTES,
    configure_logging,
    get_logger,
)


def _reset_root_logger() -> None:
    """Strip every handler and restore default level before/after each test.

    ``configure_logging`` only removes its own handlers (those marked
    ``_eden_service_handler``); tests that exercise the bare logger
    must not leak handlers across the suite. Restoring the level to
    WARNING matters too: a leaked INFO level enables debug/info calls
    in unrelated tests downstream (e.g. ideator tests that call
    ``log.info("...", url=...)`` against a plain ``logging.Logger``
    — those rely on ``isEnabledFor(INFO)`` being False so the
    unknown-kwarg branch never hits ``_log``).
    """
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        with contextlib.suppress(Exception):
            h.close()
    root.setLevel(logging.WARNING)


@pytest.fixture(autouse=True)
def _isolate_logging(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    _reset_root_logger()
    for name in ("EDEN_LOG_DIR", "EDEN_LOG_MAX_BYTES", "EDEN_LOG_BACKUP_COUNT"):
        monkeypatch.delenv(name, raising=False)
    yield
    _reset_root_logger()


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_no_log_dir_env_means_stdout_only() -> None:
    """Default behavior — no env var, no file handler installed."""
    configure_logging(service="orchestrator", experiment_id="exp-1")
    handlers = [
        h
        for h in logging.getLogger().handlers
        if getattr(h, "_eden_service_handler", False)
    ]
    assert len(handlers) == 1
    assert isinstance(handlers[0], logging.StreamHandler)
    assert not isinstance(handlers[0], RotatingFileHandler)


def test_log_dir_creates_per_service_jsonl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``EDEN_LOG_DIR`` is set, a ``<service>.jsonl`` file is created
    and receives well-formed JSON records identical to stdout."""
    monkeypatch.setenv("EDEN_LOG_DIR", str(tmp_path))
    configure_logging(service="orchestrator", experiment_id="exp-1")
    log = get_logger("test")
    log.info("hello", iteration=42)
    for h in logging.getLogger().handlers:
        h.flush()

    target = tmp_path / "orchestrator.jsonl"
    assert target.exists()
    records = _read_jsonl(target)
    assert len(records) == 1
    rec = records[0]
    assert rec["message"] == "hello"
    assert rec["service"] == "orchestrator"
    assert rec["experiment_id"] == "exp-1"
    assert rec["iteration"] == 42
    assert rec["level"] == "info"
    # Per the format contract: ts is ISO-8601 UTC.
    assert cast(str, rec["ts"]).endswith("Z")


def test_log_dir_is_created_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The handler creates ``EDEN_LOG_DIR`` (and parents) if absent."""
    log_dir = tmp_path / "deep" / "nest" / "logs"
    assert not log_dir.exists()
    monkeypatch.setenv("EDEN_LOG_DIR", str(log_dir))
    configure_logging(service="ideator-host", experiment_id="exp-1")
    log = get_logger("test")
    log.info("created")
    for h in logging.getLogger().handlers:
        h.flush()
    assert (log_dir / "ideator-host.jsonl").exists()


def test_log_dir_append_mode_preserves_prior_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-running configure_logging against the same dir appends, never
    truncates — crash forensics depend on prior-run records surviving
    a restart of the service."""
    monkeypatch.setenv("EDEN_LOG_DIR", str(tmp_path))

    configure_logging(service="executor-host", experiment_id="exp-1")
    get_logger("test").info("first-run")
    for h in logging.getLogger().handlers:
        h.flush()

    # Simulate a process restart: drop handlers, reinitialize.
    _reset_root_logger()
    configure_logging(service="executor-host", experiment_id="exp-1")
    get_logger("test").info("second-run")
    for h in logging.getLogger().handlers:
        h.flush()

    records = _read_jsonl(tmp_path / "executor-host.jsonl")
    messages = [r["message"] for r in records]
    assert messages == ["first-run", "second-run"]


def test_idempotent_reconfigure_does_not_stack_handlers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Calling configure_logging twice with the file handler enabled
    keeps exactly one stdout + one file handler on the root logger
    (no duplicate writes)."""
    monkeypatch.setenv("EDEN_LOG_DIR", str(tmp_path))
    configure_logging(service="web-ui", experiment_id="exp-1")
    configure_logging(service="web-ui", experiment_id="exp-1")
    eden_handlers = [
        h
        for h in logging.getLogger().handlers
        if getattr(h, "_eden_service_handler", False)
    ]
    assert len(eden_handlers) == 2
    n_stream_only = sum(
        1
        for h in eden_handlers
        if isinstance(h, logging.StreamHandler)
        and not isinstance(h, RotatingFileHandler)
    )
    n_file = sum(
        1
        for h in eden_handlers
        if isinstance(h, RotatingFileHandler)
    )
    assert n_stream_only == 1
    assert n_file == 1

    # And only one record per emit, despite the reconfigure.
    get_logger("test").info("single")
    for h in logging.getLogger().handlers:
        h.flush()
    records = _read_jsonl(tmp_path / "web-ui.jsonl")
    assert len(records) == 1


def test_rotation_kicks_in_at_max_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A small ``EDEN_LOG_MAX_BYTES`` triggers ``RotatingFileHandler``
    to produce ``.jsonl.1`` backups. Verifies the rotation knob is
    wired, not the exact rollover boundary (that's stdlib-defined)."""
    monkeypatch.setenv("EDEN_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("EDEN_LOG_MAX_BYTES", "200")
    monkeypatch.setenv("EDEN_LOG_BACKUP_COUNT", "2")
    configure_logging(service="evaluator-host", experiment_id="exp-1")
    log = get_logger("test")
    # Each record is well over 200 bytes once the ts / service /
    # experiment_id / level / message overhead is included — three
    # emits force at least one rollover.
    for i in range(10):
        log.info(f"message-{i}", iteration=i)
    for h in logging.getLogger().handlers:
        h.flush()

    assert (tmp_path / "evaluator-host.jsonl").exists()
    backups = sorted(tmp_path.glob("evaluator-host.jsonl.*"))
    assert backups, "expected at least one rotated backup file"
    # backupCount caps the number of backups.
    assert len(backups) <= 2


def test_invalid_max_bytes_falls_back_to_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Garbage env-var values MUST NOT crash logger init; they fall
    back to the documented defaults."""
    monkeypatch.setenv("EDEN_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("EDEN_LOG_MAX_BYTES", "not-a-number")
    monkeypatch.setenv("EDEN_LOG_BACKUP_COUNT", "0")  # rejected; below min
    configure_logging(service="task-store-server", experiment_id="exp-1")
    file_handlers: list[RotatingFileHandler] = [
        h
        for h in logging.getLogger().handlers
        if isinstance(h, RotatingFileHandler)
    ]
    assert len(file_handlers) == 1
    assert file_handlers[0].maxBytes == _DEFAULT_LOG_MAX_BYTES
    assert file_handlers[0].backupCount == _DEFAULT_LOG_BACKUP_COUNT


def test_unwritable_log_dir_does_not_break_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ``EDEN_LOG_DIR`` value that points at a non-creatable path
    (a regular file, not a directory) falls through to stdout-only
    — the service must still come up."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir")
    monkeypatch.setenv("EDEN_LOG_DIR", str(blocker / "logs"))
    configure_logging(service="orchestrator", experiment_id="exp-1")
    eden_handlers = [
        h
        for h in logging.getLogger().handlers
        if getattr(h, "_eden_service_handler", False)
    ]
    # Stdout handler installed; file handler skipped.
    assert any(
        isinstance(h, logging.StreamHandler)
        and not isinstance(h, RotatingFileHandler)
        for h in eden_handlers
    )
    assert not any(
        isinstance(h, RotatingFileHandler)
        for h in eden_handlers
    )


def test_empty_log_dir_env_is_treated_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``EDEN_LOG_DIR=`` (empty / whitespace) MUST NOT install a file
    handler — operators who clear the env var expect stdout-only."""
    monkeypatch.setenv("EDEN_LOG_DIR", "   ")
    configure_logging(service="orchestrator", experiment_id="exp-1")
    assert not any(
        isinstance(h, RotatingFileHandler)
        for h in logging.getLogger().handlers
    )
