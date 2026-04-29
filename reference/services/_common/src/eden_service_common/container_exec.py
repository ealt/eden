"""Container-isolated execution of user-supplied ``*_command`` scripts.

When a worker host runs in ``--mode subprocess --exec-mode docker``,
the user's command runs in a sibling docker container (DooD) instead
of in-process on the worker host. This module owns the wrap shape,
the cidfile lifecycle, and the orphan-reaping helpers.

See [docs/plans/eden-phase-10d-followup-a-container-isolation.md] for
design rationale.
"""

from __future__ import annotations

import contextlib
import logging
import shlex
import subprocess
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_DOCKER_KILL_TIMEOUT_S = 10.0
"""How long ``docker kill`` / ``docker rm -f`` may block.

The local docker client communicates with the daemon over a unix
socket and the daemon's response is essentially immediate; 10s is a
generous upper bound that still keeps shutdown bounded.
"""


@dataclass(frozen=True)
class VolumeMount:
    """A docker named-volume mount, parsed from ``--exec-volume``."""

    name: str
    target: str
    read_only: bool = False


@dataclass(frozen=True)
class BindMount:
    """A docker bind mount, parsed from ``--exec-bind``."""

    source: str
    target: str
    read_only: bool = False


def parse_volume_spec(spec: str) -> VolumeMount:
    """Parse a ``<name>:<target>[:ro]`` spec.

    Raises :class:`ValueError` on a malformed spec. Targets MUST be
    absolute paths.
    """
    parts = spec.split(":")
    if len(parts) == 2:
        name, target = parts
        ro = False
    elif len(parts) == 3:
        name, target, mode = parts
        if mode not in ("ro", "rw"):
            raise ValueError(
                f"--exec-volume mode must be 'ro' or 'rw'; got {mode!r}"
            )
        ro = mode == "ro"
    else:
        raise ValueError(
            f"--exec-volume must be 'name:target[:ro|rw]'; got {spec!r}"
        )
    if not name:
        raise ValueError(f"--exec-volume missing volume name: {spec!r}")
    if not target.startswith("/"):
        raise ValueError(
            f"--exec-volume target must be absolute path; got {target!r}"
        )
    return VolumeMount(name=name, target=target, read_only=ro)


def parse_bind_spec(spec: str) -> BindMount:
    """Parse a ``<host-path>:<target>[:ro]`` spec.

    Both paths MUST be absolute. Raises :class:`ValueError` on a
    malformed spec.
    """
    parts = spec.split(":")
    if len(parts) == 2:
        source, target = parts
        ro = False
    elif len(parts) == 3:
        source, target, mode = parts
        if mode not in ("ro", "rw"):
            raise ValueError(
                f"--exec-bind mode must be 'ro' or 'rw'; got {mode!r}"
            )
        ro = mode == "ro"
    else:
        raise ValueError(
            f"--exec-bind must be 'host-path:target[:ro|rw]'; got {spec!r}"
        )
    if not source.startswith("/"):
        raise ValueError(
            f"--exec-bind source must be absolute path; got {source!r}"
        )
    if not target.startswith("/"):
        raise ValueError(
            f"--exec-bind target must be absolute path; got {target!r}"
        )
    return BindMount(source=source, target=target, read_only=ro)


def make_cidfile_path(*, cidfile_dir: Path, role: str) -> Path:
    """Generate a unique cidfile path under ``cidfile_dir``.

    Path shape: ``<cidfile_dir>/<role>-<spawn-uuid>.cid``. We use a
    fresh uuid (not task-id-derived) so reclaimed task ids never
    collide with a stale cidfile.
    """
    cidfile_dir.mkdir(parents=True, exist_ok=True)
    return cidfile_dir / f"{role}-{uuid.uuid4().hex}.cid"


def wrap_command(
    *,
    original_command: str,
    image: str,
    cwd_target: str,
    cidfile: Path,
    role: str,
    task_id: str,
    host_id: str,
    volumes: Sequence[VolumeMount],
    binds: Sequence[BindMount],
    env_keys: Iterable[str],
    attach_stdin: bool = True,
) -> str:
    """Build the ``docker run …`` shell command that wraps ``original_command``.

    The returned string is suitable for passing to ``shell=True``
    Popen — it is one ``docker run`` invocation that ends in
    ``bash -lc '<shell-quoted original>'``.

    Validates that ``cwd_target`` falls under at least one mount
    target; raises :class:`ValueError` otherwise. (A cwd outside
    every mount would yield an empty cwd inside the container.)

    ``attach_stdin`` controls whether ``-i`` is added to the docker
    run flags. Set ``True`` (default) when the caller will pipe
    stdin to the spawn (planner protocol). Set ``False`` for
    short-lived non-interactive workloads (implementer / evaluator)
    so docker run doesn't exit early when the worker host's stdin
    is closed (compose containers run with stdin closed by default,
    and `-i` makes docker run treat that EOF as a signal to detach
    before the container has a chance to do useful work).
    """
    targets = [m.target for m in volumes] + [m.target for m in binds]
    if not any(_is_under(cwd_target, t) for t in targets):
        raise ValueError(
            f"cwd_target {cwd_target!r} is not under any mount target; "
            f"the wrapped child container would see an empty cwd. "
            f"Mount targets: {targets!r}"
        )

    # The leading `exec` makes the wrapping `/bin/sh -c …` shell
    # `exec` docker run in place rather than fork-and-wait. Without
    # it, on Linux (where /bin/sh is typically dash) SIGTERM to
    # subprocess.Popen's pgid kills the shell first; the shell exits
    # without giving docker run time to forward the signal, popen
    # exits, terminate's wait returns success — and the SIGKILL
    # escalation that should have run the post_kill_callback never
    # fires. With `exec`, popen.pid IS docker run; SIGTERM goes
    # straight to it.
    parts: list[str] = ["exec", "docker", "run", "--rm", "--init"]
    if attach_stdin:
        parts.append("-i")
    parts += ["--cidfile", str(cidfile)]
    parts += ["--label", f"eden.host={host_id}"]
    parts += ["--label", f"eden.task_id={task_id}"]
    parts += ["--label", f"eden.role={role}"]
    for v in volumes:
        spec = f"type=volume,source={v.name},target={v.target}"
        if v.read_only:
            spec += ",readonly"
        parts += ["--mount", spec]
    for b in binds:
        spec = f"type=bind,source={b.source},target={b.target}"
        if b.read_only:
            spec += ",readonly"
        parts += ["--mount", spec]
    parts += ["-w", cwd_target]
    # Stable ordering for reproducible output (helps tests + log
    # diffing).
    for key in sorted(set(env_keys)):
        parts += ["-e", key]
    parts += [image, "bash", "-lc", original_command]
    return " ".join(shlex.quote(p) for p in parts)


def _is_under(path: str, ancestor: str) -> bool:
    p = path.rstrip("/") + "/"
    a = ancestor.rstrip("/") + "/"
    return p == a or p.startswith(a)


def kill_via_cidfile(cidfile: Path) -> None:
    """Kill (and remove) the container recorded in ``cidfile``.

    Best-effort with structured logging on failure. The cidfile may
    be absent (no-op), empty (no-op), or point at a container that
    is already gone (`docker kill`/`docker rm -f` exit non-zero,
    which we log and ignore). Transport failures (timeout, exec
    failure) are caught and logged at warning so the host's log
    surfaces them — the surrounding ``Subprocess.terminate`` catches
    its callback's exceptions, so SIGKILL cleanup doesn't propagate.
    """
    if not cidfile.is_file():
        return
    cid = cidfile.read_text().strip()
    if not cid:
        return
    for cmd, label in (
        (["docker", "kill", cid], "kill"),
        (["docker", "rm", "-f", cid], "rm"),
    ):
        try:
            result = subprocess.run(
                cmd,
                check=False,
                timeout=_DOCKER_KILL_TIMEOUT_S,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                log.warning(
                    "kill_via_cidfile_nonzero",
                    extra={
                        "step": label,
                        "cid": cid,
                        "returncode": result.returncode,
                        "stderr": result.stderr.strip(),
                    },
                )
        except Exception as exc:  # noqa: BLE001 — best-effort
            log.warning(
                "kill_via_cidfile_failed",
                extra={"step": label, "cid": cid, "error": str(exc)},
            )


def cleanup_cidfile(cidfile: Path) -> None:
    """Unlink ``cidfile`` if it exists. Idempotent."""
    with contextlib.suppress(FileNotFoundError):
        cidfile.unlink()


def reap_orphaned_containers(*, role: str, host: str) -> None:
    """Remove any containers labeled with ``role`` and ``host``.

    Called once at host startup so a worker-host container that
    crashed mid-task last run doesn't leave dangling sibling
    containers. Best-effort; failures are logged at warning and do
    not abort startup.
    """
    try:
        result = subprocess.run(
            [
                "docker",
                "ps",
                "-aq",
                "--filter",
                f"label=eden.role={role}",
                "--filter",
                f"label=eden.host={host}",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=_DOCKER_KILL_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.warning("reap_orphan_list_failed", extra={"error": str(exc)})
        return
    if result.returncode != 0:
        log.warning(
            "reap_orphan_list_nonzero",
            extra={"stderr": result.stderr.strip()},
        )
        return
    cids = [line for line in result.stdout.splitlines() if line.strip()]
    if not cids:
        return
    log.info(
        "reap_orphan_containers",
        extra={"role": role, "host": host, "count": len(cids)},
    )
    for cid in cids:
        with contextlib.suppress(Exception):
            subprocess.run(
                ["docker", "rm", "-f", cid],
                check=False,
                timeout=_DOCKER_KILL_TIMEOUT_S,
                capture_output=True,
            )


def make_cidfile_callbacks(
    cidfile: Path,
) -> tuple[Callable[[], None], Callable[[], None]]:
    """Return ``(post_kill_callback, cleanup_callback)`` for ``cidfile``.

    Both are closures over ``cidfile``. The post-kill callback runs
    `docker kill && docker rm -f` then the cleanup callback unlinks
    the cidfile. The cleanup callback alone is registered on every
    spawn so the cidfile is removed on the happy path; the post-kill
    callback runs only on SIGKILL escalation.
    """

    def _post_kill() -> None:
        kill_via_cidfile(cidfile)

    def _cleanup() -> None:
        cleanup_cidfile(cidfile)

    return _post_kill, _cleanup
