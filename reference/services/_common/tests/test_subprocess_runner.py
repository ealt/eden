"""Unit tests for the subprocess runner helpers."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from eden_service_common import (
    parse_env_file,
    parse_json_line,
    spawn,
)


def test_parse_env_file_skips_comments_and_blanks(tmp_path: Path) -> None:
    path = tmp_path / "env"
    path.write_text(
        "# comment\nFOO=bar\n\n  BAZ = qux \nINVALID\n",
        encoding="utf-8",
    )
    parsed = parse_env_file(path)
    assert parsed == {"FOO": "bar", "BAZ": " qux"}


def test_parse_env_file_missing_path_returns_empty(tmp_path: Path) -> None:
    assert parse_env_file(tmp_path / "absent") == {}


def test_parse_json_line_well_formed() -> None:
    assert parse_json_line('{"a": 1}') == {"a": 1}


def test_parse_json_line_rejects_non_json() -> None:
    assert parse_json_line("not json") is None


def test_parse_json_line_rejects_non_object() -> None:
    assert parse_json_line("[1, 2]") is None


def test_spawn_round_trip_stdin_stdout(tmp_path: Path) -> None:
    """Echo-style worker round-trips a JSON line."""
    sub = spawn(
        command=(
            "python3 -c \"import sys, json; "
            "line = sys.stdin.readline(); "
            "obj = json.loads(line); "
            "print(json.dumps({'echo': obj['v']}), flush=True); "
            "sys.exit(0)\""
        ),
        cwd=tmp_path,
        env={},
        role="test",
    )
    sub.write_line(json.dumps({"v": 7}))
    deadline = time.monotonic() + 5
    line = sub.read_line(deadline=deadline)
    assert line == '{"echo": 7}'
    sub.popen.wait(timeout=2)


def test_spawn_terminate_signals_process_group(tmp_path: Path) -> None:
    sub = spawn(
        command="python3 -c 'import time; time.sleep(60)'",
        cwd=tmp_path,
        env={},
        role="test",
    )
    assert sub.is_alive()
    rc = sub.terminate(shutdown_deadline=5)
    assert rc != 0  # killed by SIGTERM


def test_read_line_deadline_raises(tmp_path: Path) -> None:
    sub = spawn(
        command="python3 -c 'import time; time.sleep(2)'",
        cwd=tmp_path,
        env={},
        role="test",
    )
    deadline = time.monotonic() + 0.1
    with pytest.raises(TimeoutError):
        sub.read_line(deadline=deadline)
    sub.terminate(shutdown_deadline=2)
