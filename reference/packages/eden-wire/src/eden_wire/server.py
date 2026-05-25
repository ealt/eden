# slop-allow-file: F-3 eden-wire/server.py APIRouter regroup deferred to issue #115

"""FastAPI server that exposes a ``Store`` over the EDEN wire protocol.

:func:`make_app` takes a single ``Store`` and returns a fresh
``FastAPI`` instance that routes every ``/v0/experiments/{E}/...``
endpoint specified in ``spec/v0/07-wire-protocol.md`` to the
corresponding ``Store`` method.

Error handling:

- Any ``StorageError`` raised by the store maps to the matching
  ``eden://error/<name>`` problem+json body via
  :func:`eden_wire.errors.envelope_for_error`.
- ``BadRequest`` covers schema-validation failures; FastAPI's
  ``RequestValidationError`` is caught and rewritten.
- ``ExperimentIdMismatch`` guards the header-vs-path invariant (§1.3).

The server does **not** contain business logic: every endpoint is a
thin adapter that validates the request, calls the store, and
serializes the result.
"""

from __future__ import annotations

import asyncio
import errno
import io
import os
import re
import secrets
import stat
import tempfile
import time
from pathlib import Path
from typing import Any

from eden_checkpoint import (
    CHECKPOINT_MEDIA_TYPE,
    CheckpointError,
    CheckpointInvalid,
)
from eden_checkpoint import (
    ExperimentIdMismatch as CheckpointExperimentIdMismatch,
)
from eden_contracts import Idea, TaskAdapter, Variant
from eden_storage import Store
from eden_storage.errors import NotFound, StorageError
from eden_storage.submissions import (
    Submission,
    submission_from_payload,
    submission_to_payload,
)
from fastapi import Body, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from .auth import (
    authenticate,
    install_auth_middleware,
    require_admin,
    require_worker,
)
from .errors import (
    ArtifactServingDisabled,
    ArtifactTooLarge,
    BadRequest,
    ExperimentIdMismatch,
    Forbidden,
    InvalidPath,
    Unauthorized,
    WireReferenceError,
    envelope_for_error,
    envelope_for_reference_error,
)
from .models import (
    AddGroupMemberRequest,
    ClaimRequest,
    DispatchModeResponse,
    DispatchModeUpdateRequest,
    EventsResponse,
    ExperimentStateResponse,
    IntegrateRequest,
    PolicyErrorRequest,
    ReassignRequest,
    ReclaimRequest,
    RegisterGroupRequest,
    RegisterWorkerRequest,
    RejectRequest,
    SubmitRequest,
    TerminateRequest,
    ValidateEvaluationRequest,
    ValidateTerminalResponse,
)

PROBLEM_JSON = "application/problem+json"

MAX_ARTIFACT_BYTES = 1 * 1024 * 1024
"""1 MiB cap on the artifact-serving route (12a-1f §D.2.a).

Mirrors the existing ``_read_inline_artifact`` helper in the
web-ui. Larger files return 413 with no partial body. Pairs with
the fixed-bytes ``Response(content=…)`` delivery model in
``_serve_artifact`` (see §8.2 of the plan): ``FileResponse`` would
re-open the path at body-write time and break the descriptor-walk
TOCTOU closure.
"""

_REJECT_PATH_COMPONENTS = frozenset({"", ".", ".."})
"""Path components that are NEVER valid in an artifact request.

Caught by the pre-FS-call guard in ``_open_artifact_fd`` so the
descriptor-walk below sees only well-formed segments. ``""`` covers
leading / trailing / doubled slashes; ``.`` and ``..`` cover
traversal attempts. NUL bytes are checked separately.
"""

# Per 12a-1f Decision 6: each path-walking step opens
# ``O_PATH | O_DIRECTORY | O_NOFOLLOW``. Root + all intermediates
# use this; only the terminal switches to ``O_RDONLY`` because we
# want to read its bytes. Deliberately do NOT ``Path.resolve()``
# the configured root — a symlinked ``artifacts_dir`` would be
# dereferenced before the walk and break the
# "symlinks at ANY request component → ELOOP" invariant.
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
"""Directory-walk flags for the artifact route's component walk.

Python's stdlib does not expose ``O_PATH``; ``O_RDONLY |
O_DIRECTORY | O_NOFOLLOW`` is functionally equivalent for the
descriptor-relative walk (we only use the fd as ``dir_fd=`` for
the next ``os.open`` call; we never read from it). The
``O_CLOEXEC`` flag prevents the dirfd from leaking to a forked
child if the server later spawns subprocesses (defensive — the
wire server does not fork today).
"""

_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
"""Terminal-file open flags for the artifact route."""


class _SymlinkRejected(OSError):
    """The walk hit a symlink at some component.

    Raised in place of the OS-specific errno (Linux: ELOOP; macOS
    can return ENOTDIR for a symlink-to-dir opened with
    ``O_DIRECTORY|O_NOFOLLOW``). The route handler catches this
    distinct exception and maps to 403; treating it as a normal
    ``OSError`` would conflate the symlink case with "intermediate
    is a regular file" (ENOTDIR).
    """


def _open_artifact_fd(root: Path, rel_path: str) -> int:
    """Open ``rel_path`` beneath ``root`` and return the fd.

    Descriptor-relative walk: every component (root, intermediates,
    terminal) is opened with ``O_NOFOLLOW``, anchored by the prior
    step's fd via ``dir_fd=``. To make symlink rejection OS-portable
    (macOS returns ENOTDIR rather than ELOOP for a symlink-to-dir
    opened with ``O_DIRECTORY|O_NOFOLLOW``), each step first calls
    ``os.lstat(component, dir_fd=parent_fd)`` and rejects symlinks
    via :class:`_SymlinkRejected`. ``O_NOFOLLOW`` on the subsequent
    ``os.open`` is the TOCTOU backstop — if an attacker swaps the
    real inode for a symlink between the lstat and the open, the
    open fails with ELOOP and we still get the rejection. Malformed
    components (``..``, empty segment, NUL byte) raise
    ``ValueError`` BEFORE any filesystem call (caller maps to 400).

    This is the descriptor-relative equivalent of Linux 5.6+
    ``openat2(RESOLVE_BENEATH)`` and closes the
    intermediate-component TOCTOU window: a concurrent renamer
    cannot swap an intermediate dir to a symlink while we walk
    because each step is anchored by the prior step's fd, not by
    a re-resolved path string.

    The operator-configured root is treated as TRUSTED: only the
    root's trailing basename participates in the ``O_NOFOLLOW``
    guarantee at the initial ``os.open(root, …)`` step. Ancestor
    components of the root may legitimately be symlinks (e.g.
    ``/var/lib/eden → /mnt/eden-state``).
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
                    # ELOOP on Linux for a swapped-in symlink at
                    # this component (the lstat above didn't see
                    # the symlink — TOCTOU race).
                    raise _SymlinkRejected(
                        exc.errno,
                        f"symlink hit at intermediate component "
                        f"{intermediate!r} during open",
                    ) from exc
                # ENOTDIR can mean (a) the component is a symlink
                # to a non-directory (macOS's `O_DIRECTORY|
                # O_NOFOLLOW` shape — Codex round 0 finding) or
                # (b) the component is a plain regular file
                # (legitimate "this isn't a directory").
                # Distinguish via a follow-up lstat: if it's a
                # symlink, raise _SymlinkRejected (→ 403);
                # otherwise re-raise the OSError (outer handler →
                # 404).
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

    Raises :class:`_SymlinkRejected` if ``component`` is a symbolic
    link in ``dir_fd``'s directory. Does NOT raise on non-existent
    paths — the subsequent ``os.open`` handles those uniformly.
    """
    try:
        st = os.lstat(component, dir_fd=dir_fd)
    except FileNotFoundError:
        # The component doesn't exist; let the open call raise the
        # canonical FileNotFoundError so the handler maps to 404.
        return
    except OSError:
        # Other lstat errors (EACCES, etc.) — let the subsequent
        # open surface them uniformly.
        return
    if stat.S_ISLNK(st.st_mode):
        raise _SymlinkRejected(
            errno.ELOOP,
            f"symlink not allowed at component {component!r}",
        )


def _build_content_disposition(raw_name: str) -> str:
    """Build an RFC-6266 ``Content-Disposition: attachment`` header.

    Emits both a sanitized quoted-string ``filename="..."`` (legacy
    user-agents) and a percent-encoded ``filename*=UTF-8''...``
    (modern user-agents per RFC 6266 §4.1 / RFC 5987). Strips
    control characters (including CR/LF — header-injection
    defense) and escapes the few characters that have meaning in
    HTTP quoted-strings.
    """
    from urllib.parse import quote

    # Strip control chars + path separators so the basename can't
    # carry CR/LF or sneak `..` past the user-agent's UI. Empty →
    # generic default.
    safe_ascii_chars: list[str] = []
    for ch in raw_name:
        if ord(ch) < 32 or ord(ch) == 0x7F:  # control chars + DEL
            continue
        if ch in ('"', "\\"):
            safe_ascii_chars.append("\\" + ch)  # RFC 7230 quoted-pair
        elif ch == "/" or ch == "\x00":
            continue
        elif ord(ch) > 127:
            # Non-ASCII: keep only in the filename*= form; substitute
            # `_` in the legacy quoted-string so the header stays
            # ASCII-clean per the original RFC 2616 grammar.
            safe_ascii_chars.append("_")
        else:
            safe_ascii_chars.append(ch)
    safe_ascii = "".join(safe_ascii_chars) or "artifact"

    # Percent-encode for filename*= (RFC 5987 percent-encoded UTF-8
    # value). `safe=""` so even ASCII reserved-for-attr-char chars
    # like `;`, `*`, `=` get encoded.
    encoded = quote(raw_name.replace("\x00", ""), safe="")
    return (
        f'attachment; filename="{safe_ascii}"; '
        f"filename*=UTF-8''{encoded}"
    )


# Mirror the spec/v0 §6.1 worker_id grammar from eden_storage.
# `_persist_reissued_credentials` re-validates worker_id against this
# pattern as a defense-in-depth guard: a malformed worker_id would
# otherwise interpolate directly into a filesystem path. The upstream
# `_validate_worker` already enforces this via Pydantic at import-parse
# time, but the helper checks again so a future refactor that bypasses
# the model layer (test fixtures, synthetic ImportResult) cannot trip a
# path-traversal write.
_WORKER_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _probe_credentials_dir_writable(credentials_dir: Path) -> None:
    """Pre-flight check that ``credentials_dir`` is writable.

    Codex review caught a partial-failure non-atomicity: the store-side
    import commits BEFORE the persistence loop runs, so a mid-loop
    failure (ENOSPC, EROFS, EACCES, etc.) would leave the receiver with
    fresh credential hashes committed and only a subset of the
    plaintexts persisted — the unpersisted tokens then die with the
    in-memory ``ImportResult.reissued_credentials`` mapping and become
    unrecoverable. This probe surfaces the failure BEFORE the store
    commit so the import aborts cleanly. The probe creates the
    directory and writes/removes a one-shot ``.eden-import-probe`` file
    with the same atomic-create + 0o600 semantics the real loop uses.
    """
    credentials_dir.mkdir(parents=True, exist_ok=True)
    probe = credentials_dir / ".eden-import-probe"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_TRUNC
    try:
        fd = os.open(probe, flags, 0o600)
    except FileExistsError:
        # A prior probe didn't clean up (process crash mid-import).
        # Remove + retry so we don't perma-fail.
        probe.unlink(missing_ok=True)
        fd = os.open(probe, flags, 0o600)
    try:
        os.write(fd, b"probe")
    finally:
        os.close(fd)
    probe.unlink(missing_ok=True)


def _persist_reissued_credentials(
    credentials_dir: Path, reissued: dict[str, str]
) -> list[Path]:
    """Write the post-import per-worker tokens to ``credentials_dir``.

    Per ``10-checkpoints.md`` §8 step 4 the importer mints fresh
    credentials for every imported worker atomically with the commit;
    this helper is the receiver-side implementation-defined side
    channel for the reference deployment. The file layout matches
    :func:`eden_service_common.auth.bootstrap_worker_credential`'s
    ``<credentials_dir>/<worker_id>.token`` convention so a worker
    host whose credentials volume is bind-mounted from
    ``credentials_dir`` picks up the freshly-issued bearer at
    startup with no `reissue_credential` round-trip.

    Each file holds the raw token plaintext (NOT the
    ``<worker_id>:<token>`` bearer; the host's bootstrap helper
    assembles the bearer). Files are created via
    ``os.open(O_WRONLY|O_CREAT|O_EXCL, mode=0o600)`` so the mode is
    locked at file-creation time (no chmod TOCTOU window where the
    plaintext is briefly world-readable under a permissive umask);
    the resulting fd is written and the path is then ``os.replace``-d
    onto the canonical ``<worker_id>.token`` location. The
    randomly-suffixed tmp filename is load-bearing under concurrency
    — two import handlers targeting the same worker_id MUST NOT
    share a tmp filename or one process's in-flight write could
    clobber the other's. Chapter 7 §13.5 token-storage hygiene.

    The caller is expected to have run
    :func:`_probe_credentials_dir_writable` BEFORE the store commit so
    the directory exists + is writable; this helper still calls
    ``mkdir(parents=True, exist_ok=True)`` as a belt-and-suspenders.
    """
    credentials_dir.mkdir(parents=True, exist_ok=True)
    persisted: list[Path] = []
    for worker_id in sorted(reissued):
        if not _WORKER_ID_PATTERN.match(worker_id):
            # Defense-in-depth: the upstream `_validate_worker` already
            # rejects malformed worker_ids during import parsing, but
            # filename construction below would silently escape the
            # creds dir if a future refactor bypassed that check.
            raise ValueError(
                f"refusing to persist credential for malformed worker_id "
                f"{worker_id!r}; pattern is {_WORKER_ID_PATTERN.pattern}"
            )
        token = reissued[worker_id]
        path = credentials_dir / f"{worker_id}.token"
        suffix = secrets.token_hex(8)
        tmp = path.with_suffix(f"{path.suffix}.{suffix}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_TRUNC
        try:
            fd = os.open(tmp, flags, 0o600)
            try:
                os.write(fd, token.encode("ascii"))
            finally:
                os.close(fd)
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        persisted.append(path)
    return persisted


def _build_post_import_credentials_warning(
    reissued: dict[str, str], credentials_dir_root: Path | None
) -> str | None:
    """Persist reissued credentials (when a dir is configured) and
    build the operator-facing warning string. Returns ``None`` when no
    workers were imported (no warning needed).

    Factored out of ``_import_checkpoint`` to keep the route handler
    under the complexity gate while preserving the codex-review
    invariants: ``_persist_reissued_credentials`` runs only after the
    pre-flight probe (the route handler's responsibility), the
    persisted-branch warning omits per-file absolute paths, and the
    unpersisted-branch warning enumerates worker_ids so operators
    can drive the manual ``reissue_credential`` flow.
    """
    if not reissued:
        return None
    if credentials_dir_root is not None:
        _persist_reissued_credentials(credentials_dir_root, reissued)
        return (
            f"credentials reissued and persisted for "
            f"{len(reissued)} worker(s) under {credentials_dir_root}"
        )
    return (
        "credentials reissued for "
        f"{len(reissued)} worker(s) "
        f"({', '.join(sorted(reissued))}) but no "
        "checkpoint_import_credentials_dir is configured; "
        "tokens were NOT persisted — operators must reissue "
        "via the admin endpoint before the workers can claim"
    )


def _is_symlink(component: str, *, dir_fd: int) -> bool:
    """Return True iff ``component`` is a symbolic link in ``dir_fd``.

    Helper for the ENOTDIR disambiguation in ``_open_artifact_fd``:
    on macOS, opening an intermediate-component symlink-to-non-dir
    with ``O_DIRECTORY|O_NOFOLLOW`` returns ENOTDIR (not ELOOP), so
    we lstat to distinguish "swapped-in symlink → 403" from
    "legitimate regular file as intermediate → 404".
    """
    try:
        st = os.lstat(component, dir_fd=dir_fd)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode)


# slop-allow: L-D resolved by F-3 — deferred to issue #115
def make_app(
    store: Store,
    *,
    subscribe_timeout: float = 30.0,
    subscribe_poll_interval: float = 0.1,
    admin_token: str | None = None,
    artifacts_dir: Path | str | None = None,
    checkpoint_experiment_config: str | None = None,
    checkpoint_repo_path: Path | str | None = None,
    checkpoint_import_credentials_dir: Path | str | None = None,
) -> FastAPI:
    """Build a FastAPI app that exposes ``store`` over the wire binding.

    The app is stateless beyond the injected ``store``; multiple apps
    for different experiments can coexist in one process, each with
    their own ``Store`` instance.

    ``subscribe_timeout`` is the long-poll window per
    ``07-wire-protocol.md`` §8.2 (default 30s). Tests typically pass
    a short value. ``subscribe_poll_interval`` is how often the
    server re-checks the event log for new entries; finer values
    reduce latency at the cost of CPU.

    ``admin_token``, when non-``None``, installs the §13 normative
    authentication middleware: every ``/v0/`` request MUST carry a
    valid ``Authorization: Bearer <principal>:<secret>`` header
    where the principal is either ``admin`` (matched against
    ``admin_token`` constant-time) or a registered ``worker_id``
    (verified against the Store's ``verify_worker_credential``).
    ``None`` (test / in-process default) disables auth — convenient
    for unit tests but NOT spec-conformant for a deployed server.

    ``artifacts_dir``, when non-``None``, enables the 12a-1f
    reference-only artifact-serving route at
    ``/_reference/experiments/{experiment_id}/artifacts/{path:path}``.
    The route is ALWAYS mounted regardless; when ``artifacts_dir``
    is ``None`` every request to it returns 503
    ``eden://reference-error/artifact-serving-disabled``. See
    ``spec/v0/reference-bindings/worker-host-subprocess.md`` §9 for
    the substrate-access posture this route supports.
    """
    app = FastAPI(
        title=f"EDEN task store — {store.experiment_id}",
        version="0",
    )

    artifact_root: Path | None = (
        Path(artifacts_dir) if artifacts_dir is not None else None
    )
    # 12b: optional checkpoint substrate. When the operator supplies
    # both, every export carries real bytes; when either is None the
    # route emits a zero-byte placeholder (the wave-4 in-process
    # posture; receiver-side resume requires both to be present).
    checkpoint_repo_root: Path | None = (
        Path(checkpoint_repo_path) if checkpoint_repo_path is not None else None
    )
    checkpoint_config_text: str = checkpoint_experiment_config or ""
    # Per `10-checkpoints.md` §8 the importer mints fresh credentials
    # for every imported worker atomically with the import; the
    # implementation-defined side channel here is "write each
    # `<worker_id>.token` file to this directory". The directory is
    # bind-mounted into the worker hosts' per-host credentials volumes
    # in the reference Compose deployment so the new bearers are
    # already in place by the time the workers start. Setting this
    # ``None`` (the unit-test default) skips the on-disk persistence;
    # tokens are still minted (§8 step 4 is normative) but the only
    # surfacing is the in-process Store state — operators must reissue
    # manually via the admin endpoint.
    credentials_dir_root: Path | None = (
        Path(checkpoint_import_credentials_dir)
        if checkpoint_import_credentials_dir is not None
        else None
    )

    if admin_token is not None:
        install_auth_middleware(app, admin_token=admin_token, store=store)

    def _enforce_worker(request: Request) -> None:
        """Worker-gated route guard (§13.3).

        When auth is disabled (``admin_token is None``), the middleware
        hasn't installed a principal and no enforcement runs — that's
        the in-process / TestClient posture. When auth is enabled, any
        admin bearer hitting a worker-gated route MUST 403 per the
        chapter-7 §13.3 dispatcher contract.
        """
        if admin_token is None:
            return
        require_worker(request)

    def _enforce_in_any_group(
        request: Request, group_ids: tuple[str, ...]
    ) -> str:
        """Worker-gated route guard plus group-membership check (§3.7).

        Requires the request to carry a worker bearer (admin bearers
        are rejected — these endpoints exist for operator workflows
        that the deployment surfaces through registered workers in
        ``admins`` / ``orchestrators``; the literal ``admin``
        principal is a bootstrap-only identity for registry mgmt per
        12a-1 §D.5). Then checks the worker's transitive membership
        in any of ``group_ids`` via ``Store.resolve_worker_in_group``;
        membership in ANY listed group passes (OR semantics).

        Returns the authenticated worker_id on success so the caller
        can stamp attribution fields (``reassigned_by`` / ``updated_by``).
        Returns the literal ``"anonymous"`` when auth is disabled (test
        posture) — equivalent to the existing ``X-Eden-Worker-Id``
        fallback in ``_worker_id_from_request``.

        Raises :class:`Forbidden` (403 ``eden://error/forbidden``) on
        membership miss.
        """
        if admin_token is None:
            return request.headers.get("X-Eden-Worker-Id", "anonymous")
        principal = require_worker(request)
        assert principal.worker_id is not None
        for gid in group_ids:
            if store.resolve_worker_in_group(principal.worker_id, gid):
                return principal.worker_id
        groups_str = " or ".join(repr(g) for g in group_ids)
        raise Forbidden(
            f"endpoint requires membership in {groups_str}; worker "
            f"{principal.worker_id!r} is not a transitive member"
        )

    def _stamp_created_by(
        request: Request, body: dict[str, Any], field: str = "created_by"
    ) -> dict[str, Any]:
        """Stamp ``created_by`` on a create-* request body from the auth principal.

        Per chapter 02 §3.1 / §5.1, ``created_by`` records the actor
        identifier of the caller that produced the artifact. To prevent
        a client from spoofing the attribution, the binding overrides
        the field with the authenticated principal's identity:

        - Worker bearer → ``created_by = principal.worker_id``.
        - Admin bearer → ``created_by = "admin"`` (the §13.1 admin
          principal name; carried through chapter 02 §3.1 / §5.1).

        If the body supplied a different ``created_by`` value, the
        binding rejects with `BadRequest`. When auth is disabled (test
        / in-process posture, ``admin_token is None``), the body is
        passed through unchanged so existing test fixtures keep
        working.
        """
        if admin_token is None:
            return body
        principal = getattr(request.state, "principal", None)
        if principal is None or not hasattr(principal, "is_worker"):
            return body
        if principal.is_worker():
            assert principal.worker_id is not None
            stamp = principal.worker_id
        else:
            stamp = "admin"
        supplied = body.get(field)
        if supplied is not None and supplied != stamp:
            raise BadRequest(
                f"{field}={supplied!r} disagrees with authenticated "
                f"principal {stamp!r}; the binding overrides this "
                f"field from the bearer's identity per chapter 02 §3.1"
            )
        return {**body, field: stamp}

    def _problem(status: int, type_: str, title: str, detail: str, instance: str) -> JSONResponse:
        return JSONResponse(
            status_code=status,
            media_type=PROBLEM_JSON,
            content={
                "type": type_,
                "title": title,
                "status": status,
                "detail": detail,
                "instance": instance,
            },
        )

    def _check_experiment(path_exp: str, header_exp: str | None, url: str) -> None:
        if header_exp is None:
            raise ExperimentIdMismatch(
                f"missing X-Eden-Experiment-Id header (expected {path_exp!r})"
            )
        if header_exp != path_exp:
            raise ExperimentIdMismatch(
                f"X-Eden-Experiment-Id header {header_exp!r} does not match "
                f"URL segment {path_exp!r}"
            )
        if path_exp != store.experiment_id:
            raise ExperimentIdMismatch(
                f"URL segment {path_exp!r} does not match server's experiment "
                f"{store.experiment_id!r}"
            )

    @app.exception_handler(StorageError)
    async def _storage_error_handler(request: Request, exc: StorageError) -> JSONResponse:
        envelope = envelope_for_error(exc, instance=str(request.url))
        return JSONResponse(
            status_code=envelope.status,
            media_type=PROBLEM_JSON,
            content=envelope.to_dict(),
        )

    @app.exception_handler(BadRequest)
    async def _bad_request_handler(request: Request, exc: BadRequest) -> JSONResponse:
        envelope = envelope_for_error(exc, instance=str(request.url))
        return JSONResponse(
            status_code=envelope.status,
            media_type=PROBLEM_JSON,
            content=envelope.to_dict(),
        )

    @app.exception_handler(ExperimentIdMismatch)
    async def _exp_mismatch_handler(
        request: Request, exc: ExperimentIdMismatch
    ) -> JSONResponse:
        envelope = envelope_for_error(exc, instance=str(request.url))
        return JSONResponse(
            status_code=envelope.status,
            media_type=PROBLEM_JSON,
            content=envelope.to_dict(),
        )

    @app.exception_handler(Unauthorized)
    async def _unauthorized_handler(
        request: Request, exc: Unauthorized
    ) -> JSONResponse:
        envelope = envelope_for_error(exc, instance=str(request.url))
        return JSONResponse(
            status_code=envelope.status,
            media_type=PROBLEM_JSON,
            content=envelope.to_dict(),
        )

    @app.exception_handler(Forbidden)
    async def _forbidden_handler(
        request: Request, exc: Forbidden
    ) -> JSONResponse:
        envelope = envelope_for_error(exc, instance=str(request.url))
        return JSONResponse(
            status_code=envelope.status,
            media_type=PROBLEM_JSON,
            content=envelope.to_dict(),
        )

    @app.exception_handler(WireReferenceError)
    async def _wire_reference_error_handler(
        request: Request, exc: WireReferenceError
    ) -> JSONResponse:
        # 12a-1f: the existing exception_handlers above cover only
        # normative chapter-7 errors. The new artifact route raises
        # reference-only WireReferenceError subclasses
        # (InvalidPath / ArtifactTooLarge / ArtifactServingDisabled);
        # without this handler they'd fall through to FastAPI's
        # default 500. The handler delegates to the existing
        # envelope_for_reference_error helper which already knows
        # the eden://reference-error/... URI mappings.
        envelope = envelope_for_reference_error(exc, instance=str(request.url))
        return JSONResponse(
            status_code=envelope.status,
            media_type=PROBLEM_JSON,
            content=envelope.to_dict(),
        )

    @app.exception_handler(RequestValidationError)
    async def _request_validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _problem(
            400,
            "eden://error/bad-request",
            "Bad Request",
            "; ".join(str(e) for e in exc.errors()),
            str(request.url),
        )

    @app.exception_handler(ValidationError)
    async def _pydantic_validation_handler(
        request: Request, exc: ValidationError
    ) -> JSONResponse:
        return _problem(
            400,
            "eden://error/bad-request",
            "Bad Request",
            exc.errors()[0].get("msg", "validation error") if exc.errors() else "validation error",
            str(request.url),
        )

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    base = "/v0/experiments/{experiment_id}"

    @app.post(f"{base}/tasks")
    async def _create_task(
        request: Request,
        experiment_id: str,
        body: dict[str, Any] = Body(...),
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/tasks",
        )
        # §2.1 per-kind authority. Peek at `body["kind"]` BEFORE the
        # full validate so the authority check fires on schema-valid
        # AND schema-invalid bodies alike. If the kind is missing /
        # unrecognized the downstream TaskAdapter.validate_python
        # produces the canonical bad-request envelope; we fall through
        # to that path without claiming authority either way.
        #
        # 12a-3 broadened `kind=execution` from orchestrators-only to
        # admins OR orchestrators: the new ``idea.intended_executor``
        # field gives operators a non-fungible routing seed, so the
        # pre-12a-3 deferral that the operator path needed first no
        # longer applies (`03-roles.md` §6.5, `07-wire-protocol.md`
        # §2.1).
        kind = body.get("kind") if isinstance(body, dict) else None
        if kind in ("ideation", "execution", "evaluation"):
            _enforce_in_any_group(request, ("admins", "orchestrators"))
        else:
            # Unrecognized kind — let the schema validator decide. We
            # still require a worker bearer so an admin bearer never
            # reaches the route handler.
            _enforce_worker(request)
        body = _stamp_created_by(request, body)
        try:
            task = TaskAdapter.validate_python(body)
        except ValidationError as exc:
            raise BadRequest(str(exc)) from exc
        created = store.create_task(task)
        return created.model_dump(mode="json", exclude_none=True)

    @app.get(f"{base}/tasks")
    async def _list_tasks(
        experiment_id: str,
        kind: str | None = Query(None),
        state: str | None = Query(None),
        x_eden_experiment_id: str | None = Header(None),
    ) -> list[dict[str, Any]]:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/tasks",
        )
        tasks = store.list_tasks(kind=kind, state=state)
        return [t.model_dump(mode="json", exclude_none=True) for t in tasks]

    @app.get(f"{base}/tasks/{{task_id}}")
    async def _read_task(
        experiment_id: str,
        task_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_experiment(
            experiment_id, x_eden_experiment_id, f"/v0/experiments/{experiment_id}/tasks/{task_id}"
        )
        task = store.read_task(task_id)
        return task.model_dump(mode="json", exclude_none=True)

    @app.get(f"{base}/tasks/{{task_id}}/submission")
    async def _read_submission(
        experiment_id: str,
        task_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> Response:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/tasks/{task_id}/submission",
        )
        submission = store.read_submission(task_id)
        if submission is None:
            return Response(status_code=204)
        return JSONResponse(content=_submission_to_wire(submission))

    @app.post(f"{base}/tasks/{{task_id}}/claim")
    async def _claim(
        request: Request,
        experiment_id: str,
        task_id: str,
        body: ClaimRequest,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/tasks/{task_id}/claim",
        )
        _enforce_worker(request)
        # §2.3 + §13: claimant worker_id comes from the authenticated
        # bearer, not the request body. _worker_id_from_request reads
        # request.state.principal when auth is enabled and falls back
        # to a sentinel only when auth was not installed (test-only).
        worker_id = _worker_id_from_request(request)
        claim = store.claim(task_id, worker_id, expires_at=body.expires_at)
        resp: dict[str, Any] = {
            "worker_id": claim.worker_id,
            "claimed_at": claim.claimed_at,
        }
        if claim.expires_at is not None:
            resp["expires_at"] = claim.expires_at
        return resp

    @app.post(f"{base}/tasks/{{task_id}}/submit")
    async def _submit(
        request: Request,
        experiment_id: str,
        task_id: str,
        body: SubmitRequest,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/tasks/{task_id}/submit",
        )
        _enforce_worker(request)
        # §2.4 + §13: forward the authenticated worker_id to
        # Store.submit; the Store performs the §4.1 atomic claim-match
        # (WrongClaimant / NotClaimed). No pre-flight `read_task →
        # compare` here — that would introduce a TOCTOU window
        # against reclaim.
        worker_id = _worker_id_from_request(request)
        task = store.read_task(task_id)
        submission = _submission_from_wire(task.kind, body.payload)
        store.submit(task_id, worker_id, submission)
        return {}

    @app.post(f"{base}/tasks/{{task_id}}/accept")
    async def _accept(
        request: Request,
        experiment_id: str,
        task_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> Response:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/tasks/{task_id}/accept",
        )
        # §2.5: accept is the orchestrator role's responsibility.
        _enforce_in_any_group(request, ("orchestrators",))
        store.accept(task_id)
        return Response(status_code=204)

    @app.post(f"{base}/tasks/{{task_id}}/reject")
    async def _reject(
        request: Request,
        experiment_id: str,
        task_id: str,
        body: RejectRequest,
        x_eden_experiment_id: str | None = Header(None),
    ) -> Response:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/tasks/{task_id}/reject",
        )
        # §2.5: reject is the orchestrator role's responsibility.
        _enforce_in_any_group(request, ("orchestrators",))
        store.reject(task_id, body.reason)  # type: ignore[arg-type]
        return Response(status_code=204)

    @app.post(f"{base}/tasks/{{task_id}}/reclaim")
    async def _reclaim(
        request: Request,
        experiment_id: str,
        task_id: str,
        body: ReclaimRequest,
        x_eden_experiment_id: str | None = Header(None),
    ) -> Response:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/tasks/{task_id}/reclaim",
        )
        _enforce_worker(request)
        store.reclaim(task_id, body.cause)  # type: ignore[arg-type]
        return Response(status_code=204)

    @app.post(f"{base}/tasks/{{task_id}}/reassign")
    async def _reassign_task(
        request: Request,
        experiment_id: str,
        task_id: str,
        body: ReassignRequest,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        """§2.7: admin-group-gated reassignment of `task.target`.

        Stamps `reassigned_by` from the authenticated principal; the
        request body MUST NOT carry the field (the `ReassignRequest`
        model forbids it via `extra="forbid"`).
        """
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/tasks/{task_id}/reassign",
        )
        reassigned_by = _enforce_in_any_group(request, ("admins",))
        updated = store.reassign_task(
            task_id,
            body.new_target,
            reason=body.reason,
            reassigned_by=reassigned_by,
        )
        return updated.model_dump(mode="json", exclude_none=True)

    # ------------------------------------------------------------------
    # Ideas
    # ------------------------------------------------------------------

    @app.post(f"{base}/ideas")
    async def _create_idea(
        request: Request,
        experiment_id: str,
        body: dict[str, Any] = Body(...),
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_experiment(
            experiment_id, x_eden_experiment_id, f"/v0/experiments/{experiment_id}/ideas"
        )
        _enforce_worker(request)
        body = _stamp_created_by(request, body)
        try:
            idea = Idea.model_validate(body)
        except ValidationError as exc:
            raise BadRequest(str(exc)) from exc
        store.create_idea(idea)
        # §3: response body matches idea.schema.json; return the
        # stored idea so the caller sees what landed.
        return store.read_idea(idea.idea_id).model_dump(
            mode="json", exclude_none=True
        )

    @app.get(f"{base}/ideas")
    async def _list_ideas(
        experiment_id: str,
        state: str | None = Query(None),
        x_eden_experiment_id: str | None = Header(None),
    ) -> list[dict[str, Any]]:
        _check_experiment(
            experiment_id, x_eden_experiment_id, f"/v0/experiments/{experiment_id}/ideas"
        )
        ideas = store.list_ideas(state=state)
        return [p.model_dump(mode="json", exclude_none=True) for p in ideas]

    @app.get(f"{base}/ideas/{{idea_id}}")
    async def _read_idea(
        experiment_id: str,
        idea_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/ideas/{idea_id}",
        )
        return store.read_idea(idea_id).model_dump(mode="json", exclude_none=True)

    @app.post(f"{base}/ideas/{{idea_id}}/mark-ready")
    async def _mark_idea_ready(
        request: Request,
        experiment_id: str,
        idea_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> Response:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/ideas/{idea_id}/mark-ready",
        )
        _enforce_worker(request)
        store.mark_idea_ready(idea_id)
        return Response(status_code=204)

    # ------------------------------------------------------------------
    # Variants
    # ------------------------------------------------------------------

    @app.post(f"{base}/variants")
    async def _create_variant(
        request: Request,
        experiment_id: str,
        body: dict[str, Any] = Body(...),
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_experiment(
            experiment_id, x_eden_experiment_id, f"/v0/experiments/{experiment_id}/variants"
        )
        _enforce_worker(request)
        try:
            variant = Variant.model_validate(body)
        except ValidationError as exc:
            raise BadRequest(str(exc)) from exc
        store.create_variant(variant)
        # §4: response body matches variant.schema.json.
        return store.read_variant(variant.variant_id).model_dump(
            mode="json", exclude_none=True
        )

    @app.get(f"{base}/variants")
    async def _list_variants(
        experiment_id: str,
        status: str | None = Query(None),
        x_eden_experiment_id: str | None = Header(None),
    ) -> list[dict[str, Any]]:
        _check_experiment(
            experiment_id, x_eden_experiment_id, f"/v0/experiments/{experiment_id}/variants"
        )
        return [
            t.model_dump(mode="json", exclude_none=True)
            for t in store.list_variants(status=status)
        ]

    @app.get(f"{base}/variants/{{variant_id}}")
    async def _read_variant(
        experiment_id: str,
        variant_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/variants/{variant_id}",
        )
        return store.read_variant(variant_id).model_dump(mode="json", exclude_none=True)

    @app.post(f"{base}/variants/{{variant_id}}/declare-evaluation-error")
    async def _declare_variant_eval_error(
        request: Request,
        experiment_id: str,
        variant_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> Response:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/variants/{variant_id}/declare-evaluation-error",
        )
        _enforce_worker(request)
        store.declare_variant_evaluation_error(variant_id)
        return Response(status_code=204)

    @app.post(f"{base}/variants/{{variant_id}}/integrate")
    async def _integrate_variant(
        request: Request,
        experiment_id: str,
        variant_id: str,
        body: IntegrateRequest,
        x_eden_experiment_id: str | None = Header(None),
    ) -> Response:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/variants/{variant_id}/integrate",
        )
        # §4 / §5: integration is the orchestrator role's job; the
        # 12a-2 authority table pins the caller to `orchestrators`.
        _enforce_in_any_group(request, ("orchestrators",))
        # §5: 200 + empty body on success and same-value idempotent
        # retries; 409 invalid-precondition on different-SHA divergence
        # (raised by Store.integrate_variant).
        store.integrate_variant(variant_id, body.variant_commit_sha)
        return Response(status_code=200)

    # ------------------------------------------------------------------
    # Dispatch mode (12a-2 §2.8)
    # ------------------------------------------------------------------

    @app.get(f"{base}/dispatch_mode")
    async def _read_dispatch_mode(
        request: Request,
        experiment_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        """§2.8 companion read endpoint (MAY-level per spec).

        Wave-3 exposes the read because the StoreClient's read-back
        ladder for PATCH transport-indeterminate failures needs it.
        Either-auth (admin OR worker) — same posture as
        ``GET /events`` and the other read endpoints.
        """
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/dispatch_mode",
        )
        if admin_token is not None:
            _ = request.state.principal  # ensure auth was run
        mode = store.read_dispatch_mode()
        return mode.model_dump(mode="json", exclude_none=True)

    @app.patch(f"{base}/dispatch_mode")
    async def _update_dispatch_mode(
        request: Request,
        experiment_id: str,
        body: DispatchModeUpdateRequest,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        """§2.8 admin-group-gated partial-merge update.

        Stamps `updated_by` from the authenticated principal; the
        request body MUST NOT carry the field (the model's
        ``extra="allow"`` lets unknown dispatch_mode keys round-trip
        per §2.5, but the server itself sources `updated_by` from
        auth).
        """
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/dispatch_mode",
        )
        updated_by = _enforce_in_any_group(request, ("admins",))
        # Value-grammar validation lives at the wire layer so a bad
        # value (including on an unknown extra="allow" key) becomes a
        # 400 BadRequest per chapter 04 §7.1 / chapter 07 §2.8, not a
        # 409 invalid-precondition (the store-side check exists as
        # defense-in-depth but is reachable only via direct Store
        # callers). The closed value-set is `auto` / `manual`.
        #
        # Walk the FULL body — known declared fields plus
        # `model_extra` (the `extra="allow"` round-trip slot) —
        # BEFORE `exclude_none=True` collapses null values away. A
        # payload like `{"future_key": null}` would otherwise dump
        # to `{}` and slip through as a vacuous 200 OK.
        known_fields = {
            "termination",
            "ideation_creation",
            "execution_dispatch",
            "evaluation_dispatch",
            "integration",
        }
        all_keys: dict[str, Any] = {}
        for fname in known_fields:
            v = getattr(body, fname, None)
            if v is not None:
                all_keys[fname] = v
        if body.model_extra:
            all_keys.update(body.model_extra)
        for key, value in all_keys.items():
            if value not in ("auto", "manual"):
                raise BadRequest(
                    f"dispatch_mode.{key} value {value!r} is not 'auto' or 'manual'"
                )
        # The known-field subset (sans the unknown extras the wire
        # tolerates but doesn't persist) is what flows to the Store.
        updates = body.model_dump(mode="json", exclude_none=True)
        result = store.update_dispatch_mode(updates, updated_by=updated_by)
        return DispatchModeResponse.model_validate(
            result.model_dump(mode="json", exclude_none=True)
        ).model_dump(mode="json", exclude_none=True)

    # ------------------------------------------------------------------
    # Experiment lifecycle (12a-3) — chapter 7 §2.9
    # ------------------------------------------------------------------

    @app.post(f"{base}/terminate")
    async def _terminate_experiment(
        request: Request,
        experiment_id: str,
        body: TerminateRequest,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        """§2.9 admin-group-gated lifecycle transition.

        Stamps ``terminated_by`` from the authenticated principal; the
        request body MUST NOT carry it (the model's ``extra="forbid"``
        rejects unknown keys). Idempotent on the terminated state
        (`04-task-protocol.md` §8.1) — a second call returns 200 with
        the existing experiment and emits no second event; the
        winning caller's ``reason`` is the one recorded.
        """
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/terminate",
        )
        terminated_by = _enforce_in_any_group(request, ("admins",))
        experiment = store.terminate_experiment(
            reason=body.reason, terminated_by=terminated_by
        )
        return experiment.model_dump(mode="json", exclude_none=True)

    @app.post(f"{base}/policy-errors")
    async def _emit_policy_error(
        request: Request,
        experiment_id: str,
        body: PolicyErrorRequest,
        x_eden_experiment_id: str | None = Header(None),
    ) -> Response:
        """12a-3 wave-7 follow-up: emit ``experiment.policy_error``.

        Per [`03-roles.md`](../../../../spec/v0/03-roles.md) §6.2
        decision-type 0 fault-tolerance, when a termination policy
        raises the orchestrator MUST emit a registered
        ``experiment.policy_error`` event so operators see the
        failure in the event log. The orchestrator service runs
        against ``StoreClient`` (wire-bound), so the event needs a
        wire endpoint to land in the per-experiment log.

        Authority: ``orchestrators`` — the orchestrator instance is
        the only caller that produces these events. The endpoint is
        NOT exposed to ``admins`` to keep the event surface from
        becoming a manual log-spam vector.

        The event is exempt from the
        [`05-event-protocol.md`](../../../../spec/v0/05-event-protocol.md)
        §2 transactional invariant: no protocol-owned state mutation
        pairs with it. The route delegates to
        ``Store.emit_policy_error`` for the actual single-event
        append; 204 on success.
        """
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/policy-errors",
        )
        _enforce_in_any_group(request, ("orchestrators",))
        store.emit_policy_error(
            policy_kind=body.policy_kind,
            error_type=body.error_type,
            error_message=body.error_message,
        )
        return Response(status_code=204)

    @app.get(f"{base}/state")
    async def _read_experiment_state(
        request: Request,
        experiment_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        """§2.9 companion read endpoint.

        Either-auth — any registered worker MAY read the state.
        Mirrors the `GET /dispatch_mode` posture (both reads support
        the corresponding StoreClient's read-back ladders).
        """
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/state",
        )
        if admin_token is not None:
            _ = request.state.principal  # ensure auth was run
        state = store.read_experiment_state()
        return ExperimentStateResponse(state=state).model_dump(
            mode="json", exclude_none=True
        )

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    @app.get(f"{base}/events")
    async def _read_range(
        experiment_id: str,
        cursor: int = Query(0, ge=0),
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_experiment(
            experiment_id, x_eden_experiment_id, f"/v0/experiments/{experiment_id}/events"
        )
        events = store.read_range(cursor=cursor if cursor > 0 else None)
        resp = EventsResponse(events=events, cursor=cursor + len(events))
        return resp.model_dump(mode="json", exclude_none=True)

    @app.get(f"{base}/events/subscribe")
    async def _subscribe(
        experiment_id: str,
        cursor: int = Query(0, ge=0),
        timeout: float | None = Query(None, ge=0),
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        # §6.2 long-poll: hold the connection open until at least one
        # event is available after `cursor` or ``timeout`` (default
        # ``subscribe_timeout``) elapses. The underlying ``Store`` is
        # a synchronous in-process object, so we poll ``read_range``
        # in a loop with a short interval. An asyncio.sleep yields to
        # the event loop, so other requests (e.g. the write that
        # unblocks us) progress concurrently.
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/events/subscribe",
        )
        effective_timeout = timeout if timeout is not None else subscribe_timeout
        deadline = time.monotonic() + effective_timeout
        events = store.read_range(cursor=cursor if cursor > 0 else None)
        while not events and time.monotonic() < deadline:
            await asyncio.sleep(subscribe_poll_interval)
            events = store.read_range(cursor=cursor if cursor > 0 else None)
        resp = EventsResponse(events=events, cursor=cursor + len(events))
        return resp.model_dump(mode="json", exclude_none=True)

    # ------------------------------------------------------------------
    # Worker registry (chapter 7 §6)
    # ------------------------------------------------------------------

    @app.post(f"{base}/workers")
    async def _register_worker(
        request: Request,
        experiment_id: str,
        body: RegisterWorkerRequest,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/workers",
        )
        # Admin-gated. require_admin raises Forbidden (with the
        # "endpoint requires authentication" path) when auth is off,
        # so test harnesses can still drive the route by passing the
        # admin bearer.
        principal = require_admin(request) if admin_token is not None else None
        worker, registration_token = store.register_worker(
            body.worker_id,
            labels=body.labels,
            registered_by=principal.kind if principal is not None else None,
        )
        resp = worker.model_dump(mode="json", exclude_none=True)
        if registration_token is not None:
            resp["registration_token"] = registration_token
        return resp

    @app.get(f"{base}/workers")
    async def _list_workers(
        request: Request,
        experiment_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/workers",
        )
        # Either-gated (admin OR worker). Auth was already verified by
        # the middleware; we don't classify further.
        if admin_token is not None:
            _ = request.state.principal  # auth was run; principal is set
        workers = store.list_workers()
        return {
            "workers": [w.model_dump(mode="json", exclude_none=True) for w in workers]
        }

    @app.get(f"{base}/workers/{{worker_id}}")
    async def _read_worker(
        experiment_id: str,
        worker_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/workers/{worker_id}",
        )
        worker = store.read_worker(worker_id)
        return worker.model_dump(mode="json", exclude_none=True)

    @app.post(f"{base}/workers/{{worker_id}}/reissue-credential")
    async def _reissue_credential(
        request: Request,
        experiment_id: str,
        worker_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/workers/{worker_id}/reissue-credential",
        )
        if admin_token is not None:
            require_admin(request)
        token = store.reissue_credential(worker_id)
        worker = store.read_worker(worker_id)
        resp = worker.model_dump(mode="json", exclude_none=True)
        resp["registration_token"] = token
        return resp

    @app.get(f"{base}/whoami")
    async def _whoami(
        request: Request,
        experiment_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, str]:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/whoami",
        )
        # Worker-gated per §6.4: the endpoint exists to confirm the
        # caller's worker_id; an admin bearer cannot speak as a
        # worker, so this MUST 403 for admins.
        if admin_token is not None:
            principal = require_worker(request)
            assert principal.worker_id is not None
            return {"worker_id": principal.worker_id}
        # Auth disabled: return a sentinel so tests still get a 200.
        return {"worker_id": "anonymous"}

    # ------------------------------------------------------------------
    # Group registry (chapter 7 §7)
    # ------------------------------------------------------------------

    @app.post(f"{base}/groups")
    async def _register_group(
        request: Request,
        experiment_id: str,
        body: RegisterGroupRequest,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/groups",
        )
        principal = require_admin(request) if admin_token is not None else None
        group = store.register_group(
            body.group_id,
            members=body.members,
            created_by=principal.kind if principal is not None else None,
        )
        return group.model_dump(mode="json", exclude_none=True)

    @app.get(f"{base}/groups")
    async def _list_groups(
        experiment_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/groups",
        )
        groups = store.list_groups()
        return {
            "groups": [g.model_dump(mode="json", exclude_none=True) for g in groups]
        }

    @app.get(f"{base}/groups/{{group_id}}")
    async def _read_group(
        experiment_id: str,
        group_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/groups/{group_id}",
        )
        group = store.read_group(group_id)
        return group.model_dump(mode="json", exclude_none=True)

    @app.post(f"{base}/groups/{{group_id}}/members")
    async def _add_to_group(
        request: Request,
        experiment_id: str,
        group_id: str,
        body: AddGroupMemberRequest,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/groups/{group_id}/members",
        )
        if admin_token is not None:
            require_admin(request)
        group = store.add_to_group(group_id, body.member_id)
        return group.model_dump(mode="json", exclude_none=True)

    @app.delete(f"{base}/groups/{{group_id}}/members/{{member_id}}")
    async def _remove_from_group(
        request: Request,
        experiment_id: str,
        group_id: str,
        member_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/groups/{group_id}/members/{member_id}",
        )
        if admin_token is not None:
            require_admin(request)
        group = store.remove_from_group(group_id, member_id)
        return group.model_dump(mode="json", exclude_none=True)

    @app.delete(f"{base}/groups/{{group_id}}")
    async def _delete_group(
        request: Request,
        experiment_id: str,
        group_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> Response:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/groups/{group_id}",
        )
        if admin_token is not None:
            require_admin(request)
        store.delete_group(group_id)
        return Response(status_code=204)

    # ------------------------------------------------------------------
    # Portable checkpoints (chapter 7 §14, chapter 10)
    # ------------------------------------------------------------------

    @app.get(f"{base}")
    async def _read_experiment(
        request: Request,
        experiment_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        """Chapter 7 §14.3: read the full experiment runtime object.

        Either-auth (any registered worker MAY read, parallel to the
        §2.9 ``GET /state`` companion read). Returns ``state`` +
        ``created_at`` + ``imported_from`` per
        ``spec/v0/schemas/experiment.schema.json``; the
        ``imported_from`` field is the recovery-probe anchor for the
        lost-import-response case in chapter 10 §10. The orchestrator's
        per-iteration ``ExperimentStateView.experiment_created_at`` is
        the other consumer; restricting this surface to admin-only
        would 403 the orchestrator's worker bearer and break the
        dispatch loop (caught by the wave-5 smoke regression). See
        §14 intro for the bootstrap-class boundary rationale.
        """
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}",
        )
        # Auth middleware (when admin_token is set) has already
        # authenticated the principal; either-auth means we accept any
        # registered principal class. No additional gate here.
        return store.read_experiment().model_dump(mode="json", exclude_none=False)

    @app.post(f"{base}/checkpoint")
    async def _export_checkpoint(
        request: Request,
        experiment_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> Response:
        """Chapter 7 §14.1: stream a portable-checkpoint archive.

        Admin-gated (literal ``admin`` principal per §13.1). Returns the tar bytes with
        ``Content-Type: application/x-eden-checkpoint+tar``. The wave-4
        binding materializes the archive to an in-memory buffer; future
        revisions MAY switch to a streaming temp-file model for very
        large experiments (chapter 10 §6 leaves the materialization
        strategy implementation-defined).

        Caller-supplied substrate-external pieces (``experiment_config``
        text and ``repo_bundle`` bytes) are NOT carried on this wave-4
        endpoint — wave 4 surfaces only the Store-managed JSONL data
        plus zero-byte placeholders for those fields. Wave 5 wires the
        substrate-external integration.
        """
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/v0/experiments/{experiment_id}/checkpoint",
        )
        if admin_token is not None:
            require_admin(request)
        # Compose substrate-external pieces. The bundle is generated
        # per-request inside a temp file (git bundle is a write-then-read
        # flow; can't stream directly to the export buffer). When the
        # repo path is unset (e.g. test fixtures) the bundle stays
        # empty — the resulting archive is structurally valid but
        # receiver-side resume requires both substrate pieces.
        bundle_bytes = b""
        if checkpoint_repo_root is not None:
            from eden_checkpoint.repo_bundle import create_bundle

            with tempfile.TemporaryDirectory(prefix="eden-checkpoint-bundle-") as td:
                bundle_path = Path(td) / "repo.bundle"
                try:
                    create_bundle(checkpoint_repo_root, bundle_path)
                    bundle_bytes = bundle_path.read_bytes()
                except CheckpointInvalid:
                    # Empty repo / unreachable bundle: emit a zero-byte
                    # placeholder rather than 5xx-ing. The receiver's
                    # chapter-10 §12 cross-reference validation will
                    # surface any inconsistency at import time.
                    bundle_bytes = b""
        buffer = io.BytesIO()
        store.export_checkpoint(
            buffer,
            experiment_config=checkpoint_config_text,
            repo_bundle=bundle_bytes,
        )
        return Response(
            content=buffer.getvalue(),
            media_type=CHECKPOINT_MEDIA_TYPE,
        )

    @app.post("/v0/checkpoints/import")
    async def _import_checkpoint(
        request: Request,
        as_experiment_id: str | None = Query(None),
        x_eden_experiment_id: str | None = Header(None),
    ) -> Response:
        """Chapter 7 §14.2: import a portable-checkpoint archive.

        Admin-gated (literal ``admin`` principal per §13.1;
        bootstrap-class because a fresh receiver has no ``admins``-
        group member). The §1.3 experiment-scoping carve-out applies:
        the ``X-Eden-Experiment-Id`` header is OPTIONAL on this
        endpoint, but if present MUST equal the post-rewrite
        experiment_id (the manifest's id, or ``as_experiment_id`` if
        supplied). The wire layer's ``ExperimentIdMismatch`` covers
        that surface; the eden-checkpoint ``ExperimentIdMismatch``
        covers the store-target-vs-manifest mismatch and is re-raised
        through the same wire type per the spec error-vocabulary
        uniformity rule.

        The body MUST be the raw tar archive bytes; this wave does not
        accept multipart/form-data — operators using the script
        wrapper or the StoreClient send the bytes directly.
        """
        if admin_token is not None:
            require_admin(request)
        archive_bytes = await request.body()
        if not archive_bytes:
            raise BadRequest("empty request body; expected tar archive")
        # Pre-route ExperimentIdMismatch: when the optional header is
        # supplied, fail fast against the post-rewrite id BEFORE
        # extracting the archive (avoids creating a tempdir for a
        # request we'll reject anyway).
        target_id = as_experiment_id or store.experiment_id
        if (
            x_eden_experiment_id is not None
            and x_eden_experiment_id != target_id
        ):
            raise ExperimentIdMismatch(
                f"X-Eden-Experiment-Id header {x_eden_experiment_id!r} does "
                f"not match the post-rewrite experiment_id {target_id!r}"
            )
        # Pre-flight the credentials-dir probe BEFORE the store commit.
        # If persistence would fail (read-only mount, ENOSPC, permission
        # error) the import must abort cleanly — otherwise we'd leave
        # the store with fresh credential hashes and the plaintexts
        # would die in the in-memory `ImportResult.reissued_credentials`
        # mapping with no recovery path (the operator cannot tell which
        # workers got persisted). The probe is best-effort: a race
        # where the dir becomes unwritable between probe and persist
        # could still strand tokens, but the common operator-config
        # errors (typo'd path, RO mount, missing perms) are caught
        # cleanly here. See codex review.
        if credentials_dir_root is not None:
            try:
                _probe_credentials_dir_writable(credentials_dir_root)
            except OSError as exc:
                raise BadRequest(
                    f"checkpoint_import_credentials_dir is not writable: "
                    f"{credentials_dir_root} ({exc.strerror or exc})"
                ) from exc
        with tempfile.TemporaryDirectory(prefix="eden-checkpoint-wire-") as td:
            extract_dir = Path(td)
            try:
                result = store.import_checkpoint(
                    io.BytesIO(archive_bytes),
                    as_experiment_id=as_experiment_id,
                    extract_dir=extract_dir,
                )
            except CheckpointExperimentIdMismatch as exc:
                # Surface the chapter-10 §11 mismatch through the same
                # wire vocabulary as the §1.3 header check.
                raise ExperimentIdMismatch(str(exc)) from exc
        # Per `10-checkpoints.md` §8 step 4 the import already minted
        # fresh credentials for every imported worker atomically with
        # the rest of the commit; the new tokens are on
        # ``result.reissued_credentials``. The wire binding's
        # implementation-defined side channel (§8 last paragraph) is to
        # persist each ``<worker_id>:<token>`` bearer to the
        # operator-configured credentials directory so the worker hosts
        # pick it up at startup (no manual `reissue_credential` from
        # the operator is needed for the steady-state import → resume
        # flow). When no directory is configured, the tokens stay
        # ephemeral and a warning calls that out.
        warnings: list[str] = list(result.warnings)
        credential_warning = _build_post_import_credentials_warning(
            dict(result.reissued_credentials), credentials_dir_root
        )
        if credential_warning is not None:
            warnings.append(credential_warning)
        # Chapter 7 §14.2 mandates 201 Created on a successful import
        # (a new experiment row is materialized). FastAPI's default
        # would be 200; an explicit JSONResponse sets the spec-pinned
        # status without losing the problem+json envelope wiring above.
        return JSONResponse(
            status_code=201,
            content={
                "experiment_id": result.experiment_id,
                "warnings": warnings,
            },
        )

    @app.exception_handler(CheckpointError)
    async def _checkpoint_error_handler(
        request: Request, exc: CheckpointError
    ) -> JSONResponse:
        # Most CheckpointError subclasses (CheckpointInvalid,
        # ExperimentIdConflict, SpecVersionMismatch,
        # UnsupportedCheckpointVersion) have direct entries in
        # _TYPE_BY_EXC. CheckpointExperimentIdMismatch is converted in
        # the import handler above, so by the time we get here only
        # the registered subclasses arrive.
        try:
            envelope = envelope_for_error(exc, instance=str(request.url))
        except ValueError:
            # Defense in depth: an un-mapped CheckpointError surfaces
            # as a generic 400 rather than a 500.
            return _problem(
                400,
                "eden://error/checkpoint-invalid",
                "Checkpoint Invalid",
                str(exc) or type(exc).__name__,
                str(request.url),
            )
        return JSONResponse(
            status_code=envelope.status,
            media_type=PROBLEM_JSON,
            content=envelope.to_dict(),
        )

    # ------------------------------------------------------------------
    # Reference-only helpers (non-normative)
    # ------------------------------------------------------------------

    ref_base = "/_reference/experiments/{experiment_id}"

    @app.get(f"{ref_base}/tasks/{{task_id}}/validate-terminal")
    async def _validate_terminal(
        experiment_id: str,
        task_id: str,
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/_reference/experiments/{experiment_id}/tasks/{task_id}/validate-terminal",
        )
        decision, reason = store.validate_terminal(task_id)
        return ValidateTerminalResponse(
            decision=decision, reason=reason
        ).model_dump(mode="json", exclude_none=True)

    @app.post(f"{ref_base}/validate/evaluation")
    async def _validate_evaluation(
        experiment_id: str,
        body: ValidateEvaluationRequest,
        x_eden_experiment_id: str | None = Header(None),
    ) -> Response:
        _check_experiment(
            experiment_id,
            x_eden_experiment_id,
            f"/_reference/experiments/{experiment_id}/validate/evaluation",
        )
        store.validate_evaluation(body.evaluation)
        return Response(status_code=204)

    # ------------------------------------------------------------------
    # Reference-only artifact-serving route (12a-1f)
    # ------------------------------------------------------------------
    #
    # See spec/v0/reference-bindings/worker-host-subprocess.md §9 for
    # the substrate-access posture this route supports. The route is
    # mounted unconditionally; when artifacts_dir is None it returns
    # 503 with a closed-vocabulary reference-error type. The auth
    # middleware skips /_reference/ paths by default (auth.py
    # short-circuits before the route handler), so the handler does
    # its own bearer-auth check.

    @app.get(f"{ref_base}/artifacts/{{path:path}}")
    async def _serve_artifact(
        experiment_id: str,
        path: str,
        request: Request,
    ) -> Response:
        # 1. Auth-first. NEVER touch the filesystem before auth so
        #    timing / response-code differences on unauth requests
        #    can't leak existence-of-files. When admin_token is None
        #    (test / in-process posture), auth is disabled — same
        #    posture the rest of the wire takes.
        if admin_token is not None:
            authenticate(
                request.headers.get("authorization"),
                admin_token=admin_token,
                store=store,
            )
        # 2. Experiment-id mismatch guard (chapter-7 §1.3 parity).
        if experiment_id != store.experiment_id:
            raise ExperimentIdMismatch(
                f"URL segment {experiment_id!r} does not match server's "
                f"experiment {store.experiment_id!r}"
            )
        # 3. Disabled-deployment guard.
        if artifact_root is None:
            raise ArtifactServingDisabled(
                "task-store-server started without --artifacts-dir"
            )
        # 4-7. Open + read with all path-traversal / symlink /
        # size-cap guards. See `_open_and_read_artifact`.
        data = _open_and_read_artifact(artifact_root, path)
        # 8. Return with safe-delivery headers. See
        #    `_artifact_response_headers` for the
        #    Content-Disposition + nosniff posture.
        return Response(
            content=data,
            media_type="application/octet-stream",
            headers=_artifact_response_headers(path),
        )

    return app


def _open_and_read_artifact(artifact_root: Any, path: str) -> bytes:
    """Open ``artifact_root`` + ``path`` and read at most ``MAX_ARTIFACT_BYTES``.

    Closes the intermediate-component TOCTOU window via descriptor-
    relative walks (``_open_artifact_fd``): an attacker who can swap
    an intermediate dir to a symlink between resolution and open
    cannot sneak the walk out of the root, because each step is
    anchored by the prior step's fd, not a path.
    """
    try:
        fd = _open_artifact_fd(artifact_root, path)
    except ValueError as exc:
        raise InvalidPath(f"invalid path: {exc}") from exc
    except _SymlinkRejected as exc:
        # Symlink hit (terminal or intermediate). Pre-open lstat
        # check + post-open ELOOP raised this; either way we never
        # follow the link.
        raise Forbidden("symlink not allowed") from exc
    except FileNotFoundError as exc:
        # Use eden_storage.NotFound so the existing
        # @app.exception_handler(StorageError) maps to problem+json
        # under eden://error/not-found (NOT FastAPI's default
        # {"detail": ...} shape, which would bypass the wire-binding
        # error vocabulary).
        raise NotFound("artifact not found") from exc
    except OSError as exc:
        # Codex round-3: only ENOENT / ENOTDIR / ENAMETOOLONG are
        # legitimate "path doesn't resolve to a file" cases and
        # collapse to 404. Other OSErrors (EIO, EACCES, EMFILE,
        # ENOSPC, etc.) are operational server faults — propagate so
        # they surface as 5xx rather than masquerading as missing.
        if exc.errno in (errno.ENOENT, errno.ENOTDIR, errno.ENAMETOOLONG):
            raise NotFound("artifact not found") from exc
        raise

    try:
        st = os.fstat(fd)
        # Regular-file check on the OPEN fd (not on the path —
        # re-stating the path would re-open the TOCTOU window).
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


def _artifact_response_headers(path: str) -> dict[str, str]:
    """Build safe-delivery headers for an artifact response.

    ``Content-Disposition: attachment`` forces the user-agent to
    treat the response as a download (defeats stored-XSS via .html /
    .svg artifacts); ``X-Content-Type-Options: nosniff`` prevents
    MIME sniffing; ``Content-Type: application/octet-stream`` is
    generic — the agent decodes via the artifacts_uri domain
    knowledge it already has.

    Codex round-2: ``_build_content_disposition`` emits both
    ``filename="<ascii-safe>"`` and ``filename*=UTF-8''<percent>``
    per RFC 6266 §4.1 so attacker-controlled artifact names with
    quotes, backslashes, or CR/LF can't break header syntax.
    """
    raw_name = path.rsplit("/", 1)[-1] or "artifact"
    return {
        "Content-Disposition": _build_content_disposition(raw_name),
        "X-Content-Type-Options": "nosniff",
    }


def _submission_to_wire(submission: Submission) -> dict[str, Any]:
    kind, payload = submission_to_payload(submission)
    return {"kind": kind, **payload}


def _submission_from_wire(kind: str, payload: dict[str, Any]) -> Submission:
    try:
        return submission_from_payload(kind, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _worker_id_from_request(request: Request) -> str:
    """Extract the authenticated ``worker_id`` from a request.

    When auth is enabled (``admin_token`` was passed to :func:`make_app`),
    the §13 middleware sets ``request.state.principal``; this helper
    returns the principal's ``worker_id`` and rejects admin bearers
    on worker-gated routes (§13.3) with :class:`Forbidden`.

    When auth is disabled (test / in-process default), there is no
    principal on ``request.state``; this helper returns the
    ``X-Eden-Worker-Id`` header value if present, otherwise the
    sentinel ``"anonymous"``. The sentinel exists so tests that don't
    care about identity can still drive claim / submit, while tests
    that DO care can opt in by setting the header explicitly.
    """
    principal = getattr(request.state, "principal", None)
    if principal is not None:
        if not principal.is_worker():
            raise Forbidden(
                "endpoint is worker-gated; admin bearers MUST NOT access it (§13.3)"
            )
        assert principal.worker_id is not None
        return principal.worker_id
    # Auth disabled — read the test-only override header, otherwise sentinel.
    return request.headers.get("X-Eden-Worker-Id", "anonymous")


# The pre-12a-1 reference shared-token middleware has been removed in
# favor of the normative §13 per-worker + admin auth implemented in
# :mod:`eden_wire.auth` (`install_auth_middleware`). Callers that
# previously passed ``shared_token=...`` to :func:`make_app` now pass
# ``admin_token=...``; the bearer format and error vocabulary have
# moved to the normative ``eden://error/unauthorized`` /
# ``eden://error/forbidden`` types.
