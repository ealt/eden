"""Descriptor-walk artifact-serving primitives (12a-1f §D.2).

These helpers back the reference-only artifact route in
``routers/reference.py``. They were lifted out of
``eden_wire.server`` by F-3 (issue #115) so the security-sensitive
descriptor-relative walk lives in one auditable file, separate from the
app-assembly code.

The core invariant: an artifact request resolves a relative path beneath
an operator-configured root, rejecting symlinks at ANY request component
and path-traversal segments, with the intermediate-component TOCTOU
window closed via ``O_NOFOLLOW`` descriptor-relative walks (the
descriptor-relative equivalent of Linux 5.6+
``openat2(RESOLVE_BENEATH)``).
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from typing import Any

from eden_storage.errors import NotFound

from .errors import ArtifactTooLarge, Forbidden, InvalidPath

MAX_ARTIFACT_BYTES = 1 * 1024 * 1024
"""1 MiB cap on the artifact-serving route (12a-1f §D.2.a).

Mirrors the existing ``_read_inline_artifact`` helper in the web-ui.
Larger files return 413 with no partial body. Pairs with the
fixed-bytes ``Response(content=…)`` delivery model in the reference
router's ``serve_artifact`` handler: ``FileResponse`` would re-open the
path at body-write time and break the descriptor-walk TOCTOU closure.
"""

_REJECT_PATH_COMPONENTS = frozenset({"", ".", ".."})
"""Path components that are NEVER valid in an artifact request.

Caught by the pre-FS-call guard in ``_open_artifact_fd`` so the
descriptor-walk below sees only well-formed segments. ``""`` covers
leading / trailing / doubled slashes; ``.`` and ``..`` cover traversal
attempts. NUL bytes are checked separately.
"""

# Per 12a-1f Decision 6: each path-walking step opens
# ``O_PATH | O_DIRECTORY | O_NOFOLLOW``. Root + all intermediates use
# this; only the terminal switches to ``O_RDONLY`` because we want to
# read its bytes. Deliberately do NOT ``Path.resolve()`` the configured
# root — a symlinked ``artifacts_dir`` would be dereferenced before the
# walk and break the "symlinks at ANY request component → ELOOP"
# invariant.
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
"""Directory-walk flags for the artifact route's component walk.

Python's stdlib does not expose ``O_PATH``; ``O_RDONLY | O_DIRECTORY |
O_NOFOLLOW`` is functionally equivalent for the descriptor-relative walk
(we only use the fd as ``dir_fd=`` for the next ``os.open`` call; we
never read from it). The ``O_CLOEXEC`` flag prevents the dirfd from
leaking to a forked child if the server later spawns subprocesses
(defensive — the wire server does not fork today).
"""

_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
"""Terminal-file open flags for the artifact route."""


class _SymlinkRejected(OSError):
    """The walk hit a symlink at some component.

    Raised in place of the OS-specific errno (Linux: ELOOP; macOS can
    return ENOTDIR for a symlink-to-dir opened with
    ``O_DIRECTORY|O_NOFOLLOW``). The route handler catches this distinct
    exception and maps to 403; treating it as a normal ``OSError`` would
    conflate the symlink case with "intermediate is a regular file"
    (ENOTDIR).
    """


def _open_artifact_fd(root: Path, rel_path: str) -> int:
    """Open ``rel_path`` beneath ``root`` and return the fd.

    Descriptor-relative walk: every component (root, intermediates,
    terminal) is opened with ``O_NOFOLLOW``, anchored by the prior
    step's fd via ``dir_fd=``. To make symlink rejection OS-portable
    (macOS returns ENOTDIR rather than ELOOP for a symlink-to-dir opened
    with ``O_DIRECTORY|O_NOFOLLOW``), each step first calls
    ``os.lstat(component, dir_fd=parent_fd)`` and rejects symlinks via
    :class:`_SymlinkRejected`. ``O_NOFOLLOW`` on the subsequent
    ``os.open`` is the TOCTOU backstop — if an attacker swaps the real
    inode for a symlink between the lstat and the open, the open fails
    with ELOOP and we still get the rejection. Malformed components
    (``..``, empty segment, NUL byte) raise ``ValueError`` BEFORE any
    filesystem call (caller maps to 400).

    This is the descriptor-relative equivalent of Linux 5.6+
    ``openat2(RESOLVE_BENEATH)`` and closes the intermediate-component
    TOCTOU window: a concurrent renamer cannot swap an intermediate dir
    to a symlink while we walk because each step is anchored by the
    prior step's fd, not by a re-resolved path string.

    The operator-configured root is treated as TRUSTED: only the root's
    trailing basename participates in the ``O_NOFOLLOW`` guarantee at the
    initial ``os.open(root, …)`` step. Ancestor components of the root
    may legitimately be symlinks (e.g. ``/var/lib/eden →
    /mnt/eden-state``).
    """
    parts = rel_path.split("/")
    if any(p in _REJECT_PATH_COMPONENTS for p in parts):
        raise ValueError(f"invalid path component in {rel_path!r}")
    if any("\0" in p for p in parts):
        raise ValueError(f"NUL byte in path {rel_path!r}")

    root_fd = os.open(root, _DIR_FLAGS)
    try:
        current_fd = root_fd
        for intermediate in parts[:-1]:
            _check_not_symlink(intermediate, dir_fd=current_fd)
            try:
                next_fd = os.open(intermediate, _DIR_FLAGS, dir_fd=current_fd)
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    # ELOOP on Linux for a swapped-in symlink at this
                    # component (the lstat above didn't see the symlink
                    # — TOCTOU race).
                    raise _SymlinkRejected(
                        exc.errno,
                        f"symlink hit at intermediate component "
                        f"{intermediate!r} during open",
                    ) from exc
                # ENOTDIR can mean (a) the component is a symlink to a
                # non-directory (macOS's `O_DIRECTORY|O_NOFOLLOW` shape —
                # Codex round 0 finding) or (b) the component is a plain
                # regular file (legitimate "this isn't a directory").
                # Distinguish via a follow-up lstat: if it's a symlink,
                # raise _SymlinkRejected (→ 403); otherwise re-raise the
                # OSError (outer handler → 404).
                if exc.errno == errno.ENOTDIR and _is_symlink(
                    intermediate, dir_fd=current_fd
                ):
                    raise _SymlinkRejected(
                        exc.errno,
                        f"symlink hit at intermediate component "
                        f"{intermediate!r} during open",
                    ) from exc
                raise
            finally:
                if current_fd != root_fd:
                    os.close(current_fd)
            current_fd = next_fd
        terminal = parts[-1]
        _check_not_symlink(terminal, dir_fd=current_fd)
        try:
            terminal_fd = os.open(terminal, _FILE_FLAGS, dir_fd=current_fd)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise _SymlinkRejected(
                    exc.errno,
                    f"symlink hit at terminal component {terminal!r} during open",
                ) from exc
            raise
        finally:
            if current_fd != root_fd:
                os.close(current_fd)
        return terminal_fd
    finally:
        os.close(root_fd)


def _check_not_symlink(component: str, *, dir_fd: int) -> None:
    """Pre-open lstat check rejecting symlink components.

    Raises :class:`_SymlinkRejected` if ``component`` is a symbolic link
    in ``dir_fd``'s directory. Does NOT raise on non-existent paths — the
    subsequent ``os.open`` handles those uniformly.
    """
    try:
        st = os.lstat(component, dir_fd=dir_fd)
    except FileNotFoundError:
        # The component doesn't exist; let the open call raise the
        # canonical FileNotFoundError so the handler maps to 404.
        return
    except OSError:
        # Other lstat errors (EACCES, etc.) — let the subsequent open
        # surface them uniformly.
        return
    if stat.S_ISLNK(st.st_mode):
        raise _SymlinkRejected(
            errno.ELOOP,
            f"symlink not allowed at component {component!r}",
        )


def _build_content_disposition(raw_name: str) -> str:
    """Build an RFC-6266 ``Content-Disposition: attachment`` header.

    Emits both a sanitized quoted-string ``filename="..."`` (legacy
    user-agents) and a percent-encoded ``filename*=UTF-8''...`` (modern
    user-agents per RFC 6266 §4.1 / RFC 5987). Strips control characters
    (including CR/LF — header-injection defense) and escapes the few
    characters that have meaning in HTTP quoted-strings.
    """
    from urllib.parse import quote

    # Strip control chars + path separators so the basename can't carry
    # CR/LF or sneak `..` past the user-agent's UI. Empty → generic
    # default.
    safe_ascii_chars: list[str] = []
    for ch in raw_name:
        if ord(ch) < 32 or ord(ch) == 0x7F:  # control chars + DEL
            continue
        if ch in ('"', "\\"):
            safe_ascii_chars.append("\\" + ch)  # RFC 7230 quoted-pair
        elif ch == "/" or ch == "\x00":
            continue
        elif ord(ch) > 127:
            # Non-ASCII: keep only in the filename*= form; substitute `_`
            # in the legacy quoted-string so the header stays ASCII-clean
            # per the original RFC 2616 grammar.
            safe_ascii_chars.append("_")
        else:
            safe_ascii_chars.append(ch)
    safe_ascii = "".join(safe_ascii_chars) or "artifact"

    # Percent-encode for filename*= (RFC 5987 percent-encoded UTF-8
    # value). `safe=""` so even ASCII reserved-for-attr-char chars like
    # `;`, `*`, `=` get encoded.
    encoded = quote(raw_name.replace("\x00", ""), safe="")
    return (
        f'attachment; filename="{safe_ascii}"; '
        f"filename*=UTF-8''{encoded}"
    )


def _is_symlink(component: str, *, dir_fd: int) -> bool:
    """Return True iff ``component`` is a symbolic link in ``dir_fd``.

    Helper for the ENOTDIR disambiguation in ``_open_artifact_fd``: on
    macOS, opening an intermediate-component symlink-to-non-dir with
    ``O_DIRECTORY|O_NOFOLLOW`` returns ENOTDIR (not ELOOP), so we lstat
    to distinguish "swapped-in symlink → 403" from "legitimate regular
    file as intermediate → 404".
    """
    try:
        st = os.lstat(component, dir_fd=dir_fd)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode)


def open_and_read_artifact(artifact_root: Any, path: str) -> bytes:
    """Open ``artifact_root`` + ``path`` and read at most ``MAX_ARTIFACT_BYTES``.

    Closes the intermediate-component TOCTOU window via descriptor-
    relative walks (``_open_artifact_fd``): an attacker who can swap an
    intermediate dir to a symlink between resolution and open cannot
    sneak the walk out of the root, because each step is anchored by the
    prior step's fd, not a path.
    """
    try:
        fd = _open_artifact_fd(artifact_root, path)
    except ValueError as exc:
        raise InvalidPath(f"invalid path: {exc}") from exc
    except _SymlinkRejected as exc:
        # Symlink hit (terminal or intermediate). Pre-open lstat check +
        # post-open ELOOP raised this; either way we never follow the
        # link.
        raise Forbidden("symlink not allowed") from exc
    except FileNotFoundError as exc:
        # Use eden_storage.NotFound so the existing
        # @app.exception_handler(StorageError) maps to problem+json under
        # eden://error/not-found (NOT FastAPI's default {"detail": ...}
        # shape, which would bypass the wire-binding error vocabulary).
        raise NotFound("artifact not found") from exc
    except OSError as exc:
        # Codex round-3: only ENOENT / ENOTDIR / ENAMETOOLONG are
        # legitimate "path doesn't resolve to a file" cases and collapse
        # to 404. Other OSErrors (EIO, EACCES, EMFILE, ENOSPC, etc.) are
        # operational server faults — propagate so they surface as 5xx
        # rather than masquerading as missing.
        if exc.errno in (errno.ENOENT, errno.ENOTDIR, errno.ENAMETOOLONG):
            raise NotFound("artifact not found") from exc
        raise

    try:
        st = os.fstat(fd)
        # Regular-file check on the OPEN fd (not on the path — re-stating
        # the path would re-open the TOCTOU window).
        if not stat.S_ISREG(st.st_mode):
            raise NotFound("artifact not found")
        if st.st_size > MAX_ARTIFACT_BYTES:
            raise ArtifactTooLarge(
                f"artifact exceeds {MAX_ARTIFACT_BYTES}-byte cap "
                f"(size={st.st_size})"
            )
        # The 1 MiB cap bounds per-request memory.
        data = os.read(fd, MAX_ARTIFACT_BYTES + 1)
        if len(data) > MAX_ARTIFACT_BYTES:
            # Race: file grew between fstat and read.
            raise ArtifactTooLarge("artifact size grew during read")
    finally:
        os.close(fd)
    return data


def artifact_response_headers(path: str) -> dict[str, str]:
    """Build safe-delivery headers for an artifact response.

    ``Content-Disposition: attachment`` forces the user-agent to treat
    the response as a download (defeats stored-XSS via .html / .svg
    artifacts); ``X-Content-Type-Options: nosniff`` prevents MIME
    sniffing; ``Content-Type: application/octet-stream`` is generic — the
    agent decodes via the artifacts_uri domain knowledge it already has.

    Codex round-2: ``_build_content_disposition`` emits both
    ``filename="<ascii-safe>"`` and ``filename*=UTF-8''<percent>`` per
    RFC 6266 §4.1 so attacker-controlled artifact names with quotes,
    backslashes, or CR/LF can't break header syntax.
    """
    raw_name = path.rsplit("/", 1)[-1] or "artifact"
    return {
        "Content-Disposition": _build_content_disposition(raw_name),
        "X-Content-Type-Options": "nosniff",
    }
