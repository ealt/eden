"""Unit tests for ``eden_service_common.container_exec``.

These tests exercise the wrap-command builder + cidfile helpers in
isolation. The docker-backed integration tests live in
``test_container_exec_integration.py`` and are gated on a reachable
docker daemon.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest
from eden_service_common import (
    BindMount,
    VolumeMount,
    cleanup_cidfile,
    kill_via_cidfile,
    make_cidfile_callbacks,
    make_cidfile_path,
    parse_bind_spec,
    parse_volume_spec,
    wrap_command,
)


def _common_wrap_kwargs(
    cidfile: Path,
    *,
    cwd_target: str = "/var/lib/eden/worktrees/host-a/task-1",
    role: str = "implementer",
    task_id: str = "task-1",
    host_id: str = "host-a",
    extra_volumes: list[VolumeMount] | None = None,
    extra_binds: list[BindMount] | None = None,
    env_keys: list[str] | None = None,
) -> dict:
    volumes = [
        VolumeMount(name="eden-bare-repo", target="/var/lib/eden/repo"),
        VolumeMount(name="eden-worktrees", target="/var/lib/eden/worktrees"),
    ]
    if extra_volumes:
        volumes.extend(extra_volumes)
    binds = [
        BindMount(
            source="/host/path/to/exp",
            target="/etc/eden/experiment-dir",
            read_only=True,
        )
    ]
    if extra_binds:
        binds.extend(extra_binds)
    return dict(
        original_command="python3 /etc/eden/experiment-dir/implement.py",
        image="eden-runtime:dev",
        cwd_target=cwd_target,
        cidfile=cidfile,
        role=role,
        task_id=task_id,
        host_id=host_id,
        volumes=volumes,
        binds=binds,
        env_keys=env_keys
        or [
            "EDEN_TASK_JSON",
            "EDEN_OUTPUT",
            "EDEN_WORKTREE",
            "EDEN_EXPERIMENT_DIR",
        ],
    )


def test_wrap_command_basic_shape(tmp_path: Path) -> None:
    cidfile = tmp_path / "cid"
    out = wrap_command(**_common_wrap_kwargs(cidfile))
    parts = shlex.split(out)
    assert parts[0] == "docker"
    assert parts[1] == "run"
    assert "--rm" in parts
    assert "-i" in parts
    assert "--init" in parts
    # cidfile flag pair, in order.
    cf_idx = parts.index("--cidfile")
    assert parts[cf_idx + 1] == str(cidfile)


def test_wrap_command_labels(tmp_path: Path) -> None:
    out = wrap_command(**_common_wrap_kwargs(tmp_path / "cid"))
    parts = shlex.split(out)
    labels = [parts[i + 1] for i, p in enumerate(parts) if p == "--label"]
    assert "eden.host=host-a" in labels
    assert "eden.task_id=task-1" in labels
    assert "eden.role=implementer" in labels


def test_wrap_command_volume_and_bind_mounts(tmp_path: Path) -> None:
    out = wrap_command(**_common_wrap_kwargs(tmp_path / "cid"))
    parts = shlex.split(out)
    mounts = [parts[i + 1] for i, p in enumerate(parts) if p == "--mount"]
    assert any(
        m.startswith("type=volume,source=eden-bare-repo,target=/var/lib/eden/repo")
        for m in mounts
    )
    assert any(
        m.startswith("type=volume,source=eden-worktrees,target=/var/lib/eden/worktrees")
        for m in mounts
    )
    assert any(
        m.startswith(
            "type=bind,source=/host/path/to/exp,target=/etc/eden/experiment-dir"
        )
        and ",readonly" in m
        for m in mounts
    )


def test_wrap_command_envs_sorted_and_unique(tmp_path: Path) -> None:
    out = wrap_command(
        **_common_wrap_kwargs(
            tmp_path / "cid",
            env_keys=["B", "A", "B", "C"],
        )
    )
    parts = shlex.split(out)
    e_values = [parts[i + 1] for i, p in enumerate(parts) if p == "-e"]
    assert e_values == ["A", "B", "C"]


def test_wrap_command_cwd_outside_mounts_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not under any mount target"):
        wrap_command(
            **_common_wrap_kwargs(
                tmp_path / "cid",
                cwd_target="/elsewhere",
            )
        )


def test_wrap_command_quotes_user_command(tmp_path: Path) -> None:
    """A *_command containing single-quotes and dollars round-trips intact."""
    kwargs = _common_wrap_kwargs(tmp_path / "cid")
    tricky = "echo 'hello' && echo $FOO"
    kwargs["original_command"] = tricky
    out = wrap_command(**kwargs)
    parts = shlex.split(out)
    # The trailing element after `bash -lc` should be the original
    # command, recovered exactly.
    assert parts[-3] == "bash"
    assert parts[-2] == "-lc"
    assert parts[-1] == tricky


def test_wrap_command_planner_only_bind(tmp_path: Path) -> None:
    """Planner has no volume mounts (cwd = experiment-dir bind)."""
    cidfile = tmp_path / "cid"
    out = wrap_command(
        original_command="python3 plan.py",
        image="eden-runtime:dev",
        cwd_target="/etc/eden/experiment-dir",
        cidfile=cidfile,
        role="planner",
        task_id="planner-host",
        host_id="planner-host",
        volumes=[],
        binds=[
            BindMount(
                source="/abs/exp",
                target="/etc/eden/experiment-dir",
                read_only=True,
            )
        ],
        env_keys=["EDEN_EXPERIMENT_DIR"],
    )
    parts = shlex.split(out)
    assert parts[parts.index("-w") + 1] == "/etc/eden/experiment-dir"


def test_parse_volume_spec_variants() -> None:
    assert parse_volume_spec("name:/target") == VolumeMount(
        name="name", target="/target", read_only=False
    )
    assert parse_volume_spec("name:/target:rw") == VolumeMount(
        name="name", target="/target", read_only=False
    )
    assert parse_volume_spec("name:/target:ro") == VolumeMount(
        name="name", target="/target", read_only=True
    )


@pytest.mark.parametrize(
    "spec",
    [
        "",
        "no-target",
        "name:relative",
        ":/abs",
        "name:/abs:bogus",
        "a:b:c:d",
    ],
)
def test_parse_volume_spec_rejects_malformed(spec: str) -> None:
    with pytest.raises(ValueError, match=r".+"):
        parse_volume_spec(spec)


def test_parse_bind_spec_variants() -> None:
    assert parse_bind_spec("/h:/t") == BindMount(
        source="/h", target="/t", read_only=False
    )
    assert parse_bind_spec("/h:/t:ro") == BindMount(
        source="/h", target="/t", read_only=True
    )


@pytest.mark.parametrize(
    "spec",
    ["", "host:/abs", "/abs:relative", "/h:/t:bogus"],
)
def test_parse_bind_spec_rejects_malformed(spec: str) -> None:
    with pytest.raises(ValueError, match=r".+"):
        parse_bind_spec(spec)


def test_make_cidfile_path_unique(tmp_path: Path) -> None:
    a = make_cidfile_path(cidfile_dir=tmp_path, role="planner")
    b = make_cidfile_path(cidfile_dir=tmp_path, role="planner")
    assert a != b
    assert a.parent == tmp_path
    assert a.name.startswith("planner-")
    assert a.suffix == ".cid"


def test_kill_via_cidfile_noop_when_absent(tmp_path: Path) -> None:
    # No file at this path — should not raise, should not invoke
    # docker. We don't monkeypatch subprocess.run because the early-
    # return branch means it's never called.
    kill_via_cidfile(tmp_path / "missing.cid")


def test_kill_via_cidfile_noop_when_empty(tmp_path: Path) -> None:
    cidfile = tmp_path / "empty.cid"
    cidfile.write_text("")
    kill_via_cidfile(cidfile)  # must not raise


def test_cleanup_cidfile_idempotent(tmp_path: Path) -> None:
    cidfile = tmp_path / "x.cid"
    cidfile.write_text("abc")
    cleanup_cidfile(cidfile)
    assert not cidfile.exists()
    cleanup_cidfile(cidfile)  # second call must not raise


def test_make_cidfile_callbacks_returns_pair(tmp_path: Path) -> None:
    cidfile = tmp_path / "y.cid"
    post_kill, cleanup = make_cidfile_callbacks(cidfile)
    # Only the cleanup half should touch the file when the cidfile
    # is empty/absent (post_kill no-ops via kill_via_cidfile).
    cidfile.write_text("not-a-real-cid")
    cleanup()
    assert not cidfile.exists()
    # Calling post_kill against a no-longer-existing cidfile must
    # also not raise.
    post_kill()
