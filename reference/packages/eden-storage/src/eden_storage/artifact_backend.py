"""Blob backends for the reference artifact store (issue #166).

The artifact store is split in two: a **Store**-side metadata row
(``create_artifact`` / ``read_artifact`` — ACL + content-type) and a
**backend** that is a dumb bytes-in / bytes-out blob store. This module
owns the backend half.

The ``ArtifactBackend`` Protocol is deliberately metadata-free so the
S3 / GCS backends (Phase 13d, issue #174) satisfy it trivially — all
attribution, sizing, and ACL state lives in the Store, not here. The
backend is keyed by the server-minted opaque id; the client-facing URI
the deposit endpoint returns is always ``eden://artifacts/<id>``, so the
choice of backend is invisible on the wire. Four reference backends ship:

- :class:`FileArtifactBackend` — the Compose default; writes
  opaque-id-named blobs under a configured root via the exclusive-create
  / atomic-link idiom (no-overwrite per ``08-storage.md`` §5.4) and reads
  them back behind ``O_NOFOLLOW`` + regular-file guards. The opaque id is
  validated against its single-segment hex grammar first, so — unlike the
  retired ``_reference`` artifact route — there is no client-supplied path
  and therefore no traversal surface.
- :class:`InMemoryArtifactBackend` — a dict, for tests and in-process
  deployments.
- :class:`S3Backend` — AWS S3 (and any S3-compatible service, e.g.
  MinIO) via ``boto3`` (the optional ``s3`` extra; lazily imported).
- :class:`GcsBackend` — Google Cloud Storage via
  ``google-cloud-storage`` (the optional ``gcs`` extra; lazily imported).

The two cloud backends keep their SDKs behind lazy imports + optional
extras so plain ``eden-storage`` installs (conformance, the in-memory
test posture, deployments that stay on ``file``) pull in neither.
"""

from __future__ import annotations

import contextlib
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .errors import NotFound

_OPAQUE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
"""Single-segment hex grammar for the server-minted opaque id.

Mirrors ``spec/v0/schemas/artifact-metadata.schema.json`` and
``eden_contracts.artifact.OPAQUE_ID_PATTERN``. Validating against this
before any filesystem call is what lets :class:`FileArtifactBackend`
skip the descriptor-walk traversal defense the client-path-bearing
``_reference`` route needed: a hex-only id can carry no ``/`` or ``..``.
"""

_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC


def _require_valid_opaque_id(opaque_id: str) -> None:
    if not _OPAQUE_ID_RE.fullmatch(opaque_id):
        raise ValueError(f"invalid opaque artifact id {opaque_id!r}")


@runtime_checkable
class ArtifactBackend(Protocol):
    """Bytes-in / bytes-out blob store keyed by opaque id.

    Metadata-free by design (``08-storage.md`` §5.5): the Store owns the
    attribution / size / content-type row. A backend only persists and
    returns the bytes.
    """

    def store(self, opaque_id: str, data: bytes) -> None:
        """Persist ``data`` under ``opaque_id``.

        MUST be exclusive-create: a reuse of an existing ``opaque_id``
        raises ``FileExistsError`` (the ``08-storage.md`` §5.4
        no-overwrite guarantee, independent of caller id-discipline).
        """
        ...

    def load(self, opaque_id: str) -> bytes:
        """Return the bytes previously stored under ``opaque_id``.

        Raises :class:`eden_storage.errors.NotFound` if absent.
        """
        ...


class InMemoryArtifactBackend:
    """Dict-backed blob store for tests / in-process deployments."""

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    def store(self, opaque_id: str, data: bytes) -> None:
        """Persist ``data``; ``FileExistsError`` on a reused id (§5.4)."""
        _require_valid_opaque_id(opaque_id)
        if opaque_id in self._blobs:
            raise FileExistsError(f"artifact {opaque_id!r} already exists")
        self._blobs[opaque_id] = bytes(data)

    def load(self, opaque_id: str) -> bytes:
        """Return the stored bytes; ``NotFound`` if absent."""
        _require_valid_opaque_id(opaque_id)
        try:
            return self._blobs[opaque_id]
        except KeyError as exc:
            raise NotFound(f"artifact {opaque_id!r}") from exc


class FileArtifactBackend:
    """Local-filesystem blob store: one opaque-id-named file under ``root``."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def store(self, opaque_id: str, data: bytes) -> None:
        """Persist ``data`` under ``root/opaque_id``; ``FileExistsError`` on reuse."""
        _require_valid_opaque_id(opaque_id)
        final = self._root / opaque_id
        # A UNIQUE temp (mkstemp) per call, not a fixed `.{id}.tmp` name:
        # if a prior call crashed after os.link but before the unlink, a
        # fixed temp would still be a hard link to the committed inode, and
        # this call's write would truncate it before os.link raised. A
        # fresh unique temp can never alias a committed artifact.
        fd, tmp_name = tempfile.mkstemp(
            dir=self._root, prefix=f".{opaque_id}.", suffix=".tmp"
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                # Flush the bytes to disk BEFORE linking + recording the
                # (durable) metadata row, so a crash can't leave a committed
                # artifacts_uri whose bytes are missing (§5.2 durability).
                handle.flush()
                os.fsync(handle.fileno())
            # os.link is atomic and raises FileExistsError when `final`
            # already exists — the §5.4 no-overwrite guarantee.
            os.link(tmp, final)
            self._fsync_dir()
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)

    def _fsync_dir(self) -> None:
        """Fsync the root directory so the new link's dir entry is durable."""
        dir_fd = os.open(self._root, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def load(self, opaque_id: str) -> bytes:
        """Return the bytes under ``root/opaque_id``; ``NotFound`` if absent."""
        _require_valid_opaque_id(opaque_id)
        try:
            fd = os.open(self._root / opaque_id, _READ_FLAGS)
        except FileNotFoundError as exc:
            raise NotFound(f"artifact {opaque_id!r}") from exc
        try:
            st = os.fstat(fd)
            # Regular-file check on the open fd (not the path) closes the
            # TOCTOU window; O_NOFOLLOW already rejected a symlink at the
            # leaf. Defense-in-depth even though the opaque-id grammar means
            # the server never created anything but a plain file here.
            if not stat.S_ISREG(st.st_mode):
                raise NotFound(f"artifact {opaque_id!r}")
            return os.read(fd, st.st_size)
        finally:
            os.close(fd)


def _missing_extra(backend: str, extra: str, package: str) -> RuntimeError:
    """Build the install-guidance error a cloud backend raises sans its SDK."""
    return RuntimeError(
        f"{backend} requires the optional '{extra}' extra. Install it with "
        f"`pip install eden-storage[{extra}]` (pulls in {package}), or run the "
        f"task-store-server with --blob-backend file."
    )


class S3Backend:
    """S3 (and any S3-compatible service, e.g. MinIO) blob store (issue #174).

    One object per opaque id at ``<prefix>/<opaque_id>`` in ``bucket``.
    Satisfies the metadata-free :class:`ArtifactBackend` Protocol — the
    Store still owns the attribution / size / content-type row; this
    backend only persists and returns the bytes.

    **Auth.** No credentials are passed here: ``boto3`` resolves them via
    its default credential chain. The production posture is IRSA / an EC2
    instance profile (the chart annotates the pod ServiceAccount, the SDK
    picks it up); the fallback is ``AWS_ACCESS_KEY_ID`` /
    ``AWS_SECRET_ACCESS_KEY`` in the pod environment (the chart injects
    them from a Secret). Either way the literal secret never reaches a CLI
    argv — only the bucket / region / prefix / endpoint do.

    **No-overwrite (``08-storage.md`` §5.4).** :meth:`store` HEADs the key
    first and raises ``FileExistsError`` if it already exists. The
    server-minted opaque id is 128 bits of randomness, so the HEAD→PUT
    window is a negligible collision surface — the same non-atomic
    check-then-write posture :class:`InMemoryArtifactBackend` takes.
    """

    def __init__(
        self,
        *,
        bucket: str,
        region: str | None = None,
        prefix: str = "",
        endpoint_url: str | None = None,
        client: object | None = None,
    ) -> None:
        if not bucket:
            raise ValueError("S3Backend requires a non-empty bucket")
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        # boto3 ships no inline types; the injected test double is duck-typed.
        self._client: Any
        if client is not None:
            self._client = client
            return
        try:
            import boto3  # noqa: PLC0415 — lazy: keep boto3 an optional extra
        except ImportError as exc:
            raise _missing_extra("S3Backend", "s3", "boto3") from exc
        kwargs: dict[str, str] = {}
        if region:
            kwargs["region_name"] = region
        elif endpoint_url:
            # The SDK requires *some* region; MinIO ignores the value.
            kwargs["region_name"] = "us-east-1"
        if endpoint_url:
            kwargs["endpoint_url"] = endpoint_url
        self._client = boto3.client("s3", **kwargs)

    def _key(self, opaque_id: str) -> str:
        return f"{self._prefix}/{opaque_id}" if self._prefix else opaque_id

    def store(self, opaque_id: str, data: bytes) -> None:
        """Persist ``data`` at ``<prefix>/<opaque_id>``; ``FileExistsError`` on reuse."""
        _require_valid_opaque_id(opaque_id)
        from botocore.exceptions import ClientError  # noqa: PLC0415

        key = self._key(opaque_id)
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if not _s3_is_absent(exc):
                raise
        else:
            raise FileExistsError(f"artifact {opaque_id!r} already exists")
        self._client.put_object(Bucket=self._bucket, Key=key, Body=data)

    def load(self, opaque_id: str) -> bytes:
        """Return the bytes at ``<prefix>/<opaque_id>``; ``NotFound`` if absent."""
        _require_valid_opaque_id(opaque_id)
        from botocore.exceptions import ClientError  # noqa: PLC0415

        try:
            resp = self._client.get_object(Bucket=self._bucket, Key=self._key(opaque_id))
        except ClientError as exc:
            if _s3_is_absent(exc):
                raise NotFound(f"artifact {opaque_id!r}") from exc
            raise
        return resp["Body"].read()


def _s3_is_absent(exc: object) -> bool:
    """True iff a botocore ``ClientError`` means "object not present" (404)."""
    response = getattr(exc, "response", None) or {}
    error_code = response.get("Error", {}).get("Code", "")
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    # head_object surfaces a bare "404"; get_object surfaces "NoSuchKey".
    return error_code in {"404", "NoSuchKey", "NotFound"} or status == 404


class GcsBackend:
    """Google Cloud Storage blob store (issue #174).

    One object per opaque id at ``<prefix>/<opaque_id>`` in ``bucket``.
    Satisfies the metadata-free :class:`ArtifactBackend` Protocol.

    **Auth.** ``google-cloud-storage`` resolves credentials via its
    default chain: Workload Identity in production (the chart annotates
    the pod ServiceAccount) or a service-account-key JSON pointed at by
    ``GOOGLE_APPLICATION_CREDENTIALS`` (the chart mounts it from a Secret
    and sets the env var). No credential reaches a CLI argv.

    **No-overwrite (``08-storage.md`` §5.4).** :meth:`store` uploads with
    ``if_generation_match=0`` — GCS's native create-only precondition —
    so a reuse raises ``FileExistsError`` atomically server-side.
    """

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "",
        client: object | None = None,
    ) -> None:
        if not bucket:
            raise ValueError("GcsBackend requires a non-empty bucket")
        self._prefix = prefix.strip("/")
        if client is None:
            try:
                from google.cloud import storage  # noqa: PLC0415
            except ImportError as exc:
                raise _missing_extra(
                    "GcsBackend", "gcs", "google-cloud-storage"
                ) from exc
            client = storage.Client()
        # ``client.bucket`` is a cheap local handle (no network round-trip).
        self._bucket = client.bucket(bucket)  # type: ignore[attr-defined]

    def _key(self, opaque_id: str) -> str:
        return f"{self._prefix}/{opaque_id}" if self._prefix else opaque_id

    def store(self, opaque_id: str, data: bytes) -> None:
        """Persist ``data`` at ``<prefix>/<opaque_id>``; ``FileExistsError`` on reuse."""
        _require_valid_opaque_id(opaque_id)
        from google.api_core.exceptions import PreconditionFailed  # noqa: PLC0415

        blob = self._bucket.blob(self._key(opaque_id))
        try:
            blob.upload_from_string(data, if_generation_match=0)
        except PreconditionFailed as exc:
            raise FileExistsError(f"artifact {opaque_id!r} already exists") from exc

    def load(self, opaque_id: str) -> bytes:
        """Return the bytes at ``<prefix>/<opaque_id>``; ``NotFound`` if absent."""
        _require_valid_opaque_id(opaque_id)
        from google.api_core.exceptions import NotFound as GcsNotFound  # noqa: PLC0415

        blob = self._bucket.blob(self._key(opaque_id))
        try:
            return blob.download_as_bytes()
        except GcsNotFound as exc:
            raise NotFound(f"artifact {opaque_id!r}") from exc
